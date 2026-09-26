import hmac
import ipaddress
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from rfa_mas.application.observations import safe_error_code
from rfa_mas.bootstrap import Container, build_container
from rfa_mas.contracts import (
    SCHEMA_VERSION,
    DirectWorkRequest,
    DomainId,
    KnowledgeDelete,
    KnowledgeExport,
    KnowledgeImportResult,
    KnowledgeRevision,
    KnowledgeWrite,
    ResumeRequest,
    RunRecord,
    RunResult,
    SessionCreate,
    SessionDetail,
    SessionRecord,
    StructuredError,
    TrustedPrincipal,
    WorkRequest,
    new_id,
)
from rfa_mas.errors import RfaError
from rfa_mas.settings import Settings


async def resolve_principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> TrustedPrincipal:
    """Single-installation auth boundary; replace here with verified runtime identity.

    No body/header user ID, forwarding header, fixture role, or trace ID is an
    authority. Keyless mode is explicitly local development, not multi-user auth.
    """
    container = request.app.state.container
    configured = container.settings.app_api_key
    if configured is not None:
        expected = f"Bearer {configured.get_secret_value()}".encode()
        if authorization is None or not hmac.compare_digest(authorization.encode(), expected):
            raise RfaError("authentication_required", "유효한 API 인증이 필요합니다.")
    else:
        # A proxy may rewrite the ASGI peer address. Keyless development is for
        # direct loopback connections only, never forwarded identity claims.
        if any(
            name == "forwarded" or name == "x-real-ip" or name.startswith("x-forwarded-")
            for name in request.headers
        ):
            raise RfaError("authentication_required", "프록시 접근에는 API 인증이 필요합니다.")
        try:
            peer = ipaddress.ip_address(request.client.host if request.client else "")
            loopback = peer.is_loopback or bool(
                isinstance(peer, ipaddress.IPv6Address)
                and peer.ipv4_mapped
                and peer.ipv4_mapped.is_loopback
            )
        except ValueError:
            loopback = False
        if not loopback:
            raise RfaError("authentication_required", "로컬 개발 접근 또는 API 인증이 필요합니다.")
    return await container.service.sessions.local_principal()


def create_app(
    settings: Settings | None = None,
    container: Container | None = None,
) -> FastAPI:
    selected_container = container or build_container(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.container = selected_container
        await selected_container.startup()
        try:
            yield
        finally:
            await selected_container.shutdown()

    app = FastAPI(
        title="RFA MAS",
        summary="Policy-aware assistant and domain TaskGraph foundation",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.container = selected_container

    async def present_record(record: RunRecord, principal: TrustedPrincipal) -> RunRecord:
        if record.result is None:
            return record
        result = await selected_container.service.present_result(record.result, principal)
        return record.model_copy(
            update={"request_id": result.request_id, "trace_id": result.trace_id, "result": result}
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Even loc/extra-field names may be private attacker input. Never echo them.
        return JSONResponse(
            status_code=422,
            content={
                "detail": [{"loc": ["body"], "msg": "Invalid request", "type": "value_error"}]
            },
        )

    @app.exception_handler(RfaError)
    async def handle_domain_error(request: Request, exc: RfaError) -> JSONResponse:
        status_code = {
            "not_found": status.HTTP_404_NOT_FOUND,
            "policy_denied": status.HTTP_403_FORBIDDEN,
            "configuration_error": status.HTTP_503_SERVICE_UNAVAILABLE,
            "not_implemented": status.HTTP_501_NOT_IMPLEMENTED,
            "invalid_state_transition": status.HTTP_409_CONFLICT,
            "idempotency_conflict": status.HTTP_409_CONFLICT,
            "draft_version_conflict": status.HTTP_409_CONFLICT,
            "authentication_required": status.HTTP_401_UNAUTHORIZED,
            "thread_busy": status.HTTP_409_CONFLICT,
            "resume_unavailable": status.HTTP_409_CONFLICT,
        }.get(exc.code, status.HTTP_400_BAD_REQUEST)
        error = StructuredError(
            code=safe_error_code(exc.code),
            retryable=False,
            message="요청을 안전하게 처리할 수 없습니다.",
        )
        return JSONResponse(status_code=status_code, content=error.model_dump(mode="json"))

    @app.get("/healthz", tags=["operations"])
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "service": "rfa-mas",
            "schema_version": SCHEMA_VERSION,
        }

    @app.get("/readyz", tags=["operations"])
    async def ready() -> JSONResponse:
        is_ready = selected_container.ready
        return JSONResponse(
            status_code=200 if is_ready else 503,
            content={"status": "ready" if is_ready else "not_ready"},
        )

    @app.post(
        "/v1/work",
        response_model=RunResult,
        status_code=status.HTTP_201_CREATED,
        tags=["work"],
    )
    async def create_work(
        work: WorkRequest,
        trusted_principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
    ) -> RunResult:
        # Keep the 1.0 field for wire compatibility, but never use a caller's
        # proposed run ID as a storage key or an existence probe.
        return await selected_container.service.run(
            work.model_copy(update={"run_id": new_id("run")}), trusted_principal
        )

    @app.get("/v1/work/{run_id}", response_model=RunResult, tags=["work"])
    async def get_work(
        run_id: str,
        trusted_principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
    ) -> RunResult:
        return await selected_container.service.get(run_id, trusted_principal)

    @app.post("/v1/sessions", response_model=SessionRecord, status_code=201, tags=["sessions"])
    async def create_session(
        trusted_principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
        body: SessionCreate | None = None,
    ) -> SessionRecord:
        return await selected_container.service.sessions.create(trusted_principal)

    @app.get("/v1/sessions", response_model=list[SessionRecord], tags=["sessions"])
    async def list_sessions(
        trusted_principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
    ) -> list[SessionRecord]:
        return await selected_container.service.sessions.list(trusted_principal)

    @app.get("/v1/sessions/{session_id}", response_model=SessionDetail, tags=["sessions"])
    async def get_session(
        session_id: str,
        trusted_principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
    ) -> SessionDetail:
        detail = await selected_container.service.sessions.get(session_id, trusted_principal)
        return detail.model_copy(
            update={
                "runs": tuple(
                    [await present_record(record, trusted_principal) for record in detail.runs]
                )
            }
        )

    @app.post(
        "/v1/sessions/{session_id}/work",
        response_model=RunResult,
        status_code=201,
        tags=["sessions"],
    )
    async def continue_session(
        session_id: str,
        work: DirectWorkRequest,
        trusted_principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
    ) -> RunResult:
        # Authorize the path before validating its binding to an optional body ID.
        await selected_container.service.sessions.get(session_id, trusted_principal)
        if work.session_id is not None and work.session_id != session_id:
            raise RfaError("session_mismatch", "세션 참조가 일치하지 않습니다.")
        return await selected_container.service.run(
            work.model_copy(update={"session_id": session_id, "run_id": new_id("run")}),
            trusted_principal,
        )

    @app.get("/v1/runs/{run_id}", response_model=RunRecord, tags=["work"])
    async def get_run_status(
        run_id: str,
        trusted_principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
    ) -> RunRecord:
        record = await selected_container.service.sessions.get_run(run_id, trusted_principal)
        return await present_record(record, trusted_principal)

    @app.post("/v1/runs/{run_id}/resume", response_model=RunResult, tags=["work"])
    async def resume_run(
        run_id: str,
        wakeup: ResumeRequest,
        trusted_principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
    ) -> RunResult:
        return await selected_container.service.resume(run_id, wakeup, trusted_principal)

    @app.post(
        "/v1/knowledge/sources",
        response_model=KnowledgeRevision,
        status_code=201,
        tags=["knowledge"],
    )
    async def create_source(
        body: KnowledgeWrite,
        principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
    ) -> KnowledgeRevision:
        return await selected_container.knowledge.write(body, principal)

    @app.get("/v1/knowledge/sources", response_model=list[KnowledgeRevision], tags=["knowledge"])
    async def list_sources(
        principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
        domain_id: DomainId | None = None,
    ) -> list[KnowledgeRevision]:
        return await selected_container.knowledge.list(principal, domain_id=domain_id)

    @app.get(
        "/v1/knowledge/sources/{source_id}", response_model=KnowledgeRevision, tags=["knowledge"]
    )
    async def get_source(
        source_id: str,
        principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
        revision: str | None = None,
    ) -> KnowledgeRevision:
        return await selected_container.knowledge.get(source_id, principal, revision=revision)

    @app.put(
        "/v1/knowledge/sources/{source_id}", response_model=KnowledgeRevision, tags=["knowledge"]
    )
    async def update_source(
        source_id: str,
        body: KnowledgeWrite,
        principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
    ) -> KnowledgeRevision:
        return await selected_container.knowledge.write(body, principal, source_id=source_id)

    @app.delete(
        "/v1/knowledge/sources/{source_id}", response_model=KnowledgeRevision, tags=["knowledge"]
    )
    async def delete_source(
        source_id: str,
        body: KnowledgeDelete,
        principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
    ) -> KnowledgeRevision:
        return await selected_container.knowledge.delete(source_id, body, principal)

    @app.post("/v1/knowledge/imports", response_model=KnowledgeImportResult, tags=["knowledge"])
    async def import_sources(
        body: KnowledgeExport,
        principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
    ) -> KnowledgeImportResult:
        return await selected_container.knowledge.import_export(body, principal)

    return app
