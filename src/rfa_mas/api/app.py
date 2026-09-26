import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Request, status
from fastapi.responses import JSONResponse

from rfa_mas.bootstrap import Container, build_container
from rfa_mas.contracts import (
    SCHEMA_VERSION,
    RunResult,
    StructuredError,
    TrustedPrincipal,
    WorkRequest,
)
from rfa_mas.errors import RfaError
from rfa_mas.settings import Settings


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
        }.get(exc.code, status.HTTP_400_BAD_REQUEST)
        error = StructuredError(
            code=exc.code,
            retryable=exc.retryable,
            message=exc.safe_message,
            request_id=request.headers.get("X-Request-Id"),
        )
        return JSONResponse(status_code=status_code, content=error.model_dump(mode="json"))

    async def principal(
        authorization: Annotated[str | None, Header()] = None,
    ) -> TrustedPrincipal:
        configured = selected_container.settings.app_api_key
        if configured is not None:
            expected = f"Bearer {configured.get_secret_value()}"
            if authorization is None or not hmac.compare_digest(authorization, expected):
                raise RfaError("authentication_required", "유효한 API 인증이 필요합니다.")
            return TrustedPrincipal(
                user_id="api-client",
                authenticated=True,
                company_id="local-company",
                business_units=frozenset({"triv3-team", "quantization-research-team"}),
                roles=frozenset({"company"}),
            )
        return TrustedPrincipal(
            user_id="fixture-owner-001",
            authenticated=True,
            company_id="local-company",
            business_units=frozenset({"triv3-team", "quantization-research-team"}),
            roles=frozenset({"company", "local_development_identity"}),
        )

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
        trusted_principal: Annotated[TrustedPrincipal, Depends(principal)],
    ) -> RunResult:
        return await selected_container.service.run(work, trusted_principal)

    @app.get("/v1/work/{run_id}", response_model=RunResult, tags=["work"])
    async def get_work(
        run_id: str,
        _: Annotated[TrustedPrincipal, Depends(principal)],
    ) -> RunResult:
        return await selected_container.service.get(run_id)

    return app
