import hmac
import ipaddress
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Literal, TypedDict

from fastapi import Depends, FastAPI, Header, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from rfa_mas.application.observations import safe_error_code
from rfa_mas.application.candidates import CandidateService
from rfa_mas.application.feedback import FeedbackService
from rfa_mas.bootstrap import Container, build_container
from rfa_mas.contracts import (
    SCHEMA_VERSION,
    AccumulationReport,
    CandidateDecision,
    AssistantRequest,
    AssistantResponse,
    DirectWorkRequest,
    DomainId,
    DraftEditRequest,
    DraftState,
    FeedbackApplication,
    FeedbackCategory,
    FeedbackClassification,
    FeedbackCreate,
    FeedbackRecord,
    FeedbackRevoke,
    JobRun,
    KnowledgeDelete,
    KnowledgeExport,
    KnowledgeImportResult,
    KnowledgeRevision,
    KnowledgeWrite,
    Notification,
    PublicationReceipt,
    PublishRequest,
    ResumeRequest,
    RunRecord,
    RunResult,
    Schedule,
    ScheduleCreate,
    SessionCreate,
    SessionDetail,
    SessionRecord,
    SourceMetadata,
    StructuredError,
    TeamRunResult,
    TodoCandidate,
    TrustedPrincipal,
    WorkRequest,
    new_id,
)
from rfa_mas.errors import RfaError
from rfa_mas.settings import Settings

# Domain codes that are safe to echo in addition to the shared observation allowlist.
SCHEDULE_ERROR_CODES = frozenset({"invalid_schedule"})


class RunCancelState(TypedDict):
    """Cancel sets a durable barrier; effects already done stay done.

    "cancelling": a live invocation was signalled and stops at its next effect boundary.
    "cancelled": no invocation was running (e.g. waiting for approval), so the run ended here
    (P0-021 durable cancel).
    """

    state: Literal["cancelling", "cancelled"]


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
            "publication_exists": status.HTTP_409_CONFLICT,
            "approval_required": status.HTTP_409_CONFLICT,
            "approval_binding_mismatch": status.HTTP_409_CONFLICT,
            "resume_review_required": status.HTTP_409_CONFLICT,
            "authentication_required": status.HTTP_401_UNAUTHORIZED,
            "thread_busy": status.HTTP_409_CONFLICT,
            "resume_unavailable": status.HTTP_409_CONFLICT,
            "invalid_schedule": status.HTTP_422_UNPROCESSABLE_CONTENT,
        }.get(exc.code, status.HTTP_400_BAD_REQUEST)
        error = StructuredError(
            code=exc.code if exc.code in SCHEDULE_ERROR_CODES else safe_error_code(exc.code),
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

    @app.post(
        "/v1/assistant",
        response_model=AssistantResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["assistant"],
    )
    async def assistant(
        body: AssistantRequest,
        trusted_principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
    ) -> AssistantResponse:
        # Deterministic intent routing; identity comes from authentication only.
        return await selected_container.service.assist(
            body, trusted_principal, knowledge=selected_container.knowledge,
            schedules=selected_container.schedules,
            feedback=feedback,
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
        return await selected_container.service.present_session(session_id, trusted_principal)

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

    @app.get("/v1/runs/{run_id}/team", response_model=TeamRunResult, tags=["work"])
    async def get_team_result(
        run_id: str,
        trusted_principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
    ) -> TeamRunResult:
        # Owner-authorized; another owner's run and a run without a team are both 404.
        return await selected_container.service.team_result(run_id, trusted_principal)

    @app.post("/v1/runs/{run_id}/cancel", response_model=RunCancelState, tags=["work"])
    async def cancel_run(
        run_id: str,
        trusted_principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
    ) -> RunCancelState:
        # Terminal run -> invalid_state_transition (409); unsupported path -> 501.
        state = await selected_container.service.cancel(run_id, trusted_principal)
        return {"state": state}

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

    # P1-004A/B: reviewed derived items and owner work candidates (no Task auto-creation).
    candidates = CandidateService(
        selected_container.repository, selected_container.knowledge.accumulator
    )

    @app.post("/v1/knowledge/derive", response_model=AccumulationReport, tags=["knowledge"])
    async def derive_items(
        domain_id: DomainId,
        principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
    ) -> AccumulationReport:
        accumulator = selected_container.knowledge.accumulator
        proposals = await accumulator.extract(domain_id, principal)
        return await accumulator.accumulate(domain_id, proposals, principal)

    @app.get("/v1/knowledge/derived", response_model=list[SourceMetadata], tags=["knowledge"])
    async def list_derived(
        domain_id: DomainId,
        principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
    ) -> list[SourceMetadata]:
        return await selected_container.knowledge.accumulator.list_derived(domain_id, principal)

    @app.post("/v1/candidates/discover", response_model=list[TodoCandidate], tags=["candidates"])
    async def discover_candidates(
        domain_id: DomainId,
        principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
    ) -> list[TodoCandidate]:
        return await candidates.discover(domain_id, principal)

    @app.get("/v1/candidates", response_model=list[TodoCandidate], tags=["candidates"])
    async def list_candidates(
        principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
        domain_id: DomainId | None = None,
        include_hidden: bool = False,
    ) -> list[TodoCandidate]:
        return await candidates.list(domain_id, principal, include_hidden=include_hidden)

    @app.post(
        "/v1/candidates/{candidate_id}/decision",
        response_model=TodoCandidate,
        tags=["candidates"],
    )
    async def decide_candidate(
        candidate_id: str,
        body: CandidateDecision,
        principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
    ) -> TodoCandidate:
        return await candidates.decide(candidate_id, body, principal)

    # P1-005B: owner feedback memory (4 categories, scope, revocation, application log).
    feedback = FeedbackService(selected_container.repository)

    @app.post(
        "/v1/feedback/classify", response_model=FeedbackClassification, tags=["feedback"]
    )
    async def classify_feedback(
        body: FeedbackCreate,
        principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
    ) -> FeedbackClassification:
        # Preview only: nothing is stored until the owner submits (confirms) a category.
        return feedback.classify(body)

    @app.post(
        "/v1/feedback", response_model=FeedbackRecord, status_code=201, tags=["feedback"]
    )
    async def submit_feedback(
        body: FeedbackCreate,
        principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
    ) -> FeedbackRecord:
        return await feedback.submit(body, principal)

    @app.get("/v1/feedback", response_model=list[FeedbackRecord], tags=["feedback"])
    async def list_feedback(
        principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
        domain_id: DomainId | None = None,
        state: Literal["active", "revoked"] | None = None,
        category: FeedbackCategory | None = None,
    ) -> list[FeedbackRecord]:
        return await feedback.list(principal, domain_id=domain_id, state=state, category=category)

    @app.get("/v1/feedback/{feedback_id}", response_model=FeedbackRecord, tags=["feedback"])
    async def get_feedback(
        feedback_id: str,
        principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
    ) -> FeedbackRecord:
        return await feedback.get(feedback_id, principal)

    @app.get(
        "/v1/feedback/{feedback_id}/revisions",
        response_model=list[FeedbackRecord],
        tags=["feedback"],
    )
    async def feedback_revisions(
        feedback_id: str,
        principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
    ) -> list[FeedbackRecord]:
        return await feedback.revisions(feedback_id, principal)

    @app.get(
        "/v1/feedback/{feedback_id}/applications",
        response_model=list[FeedbackApplication],
        tags=["feedback"],
    )
    async def feedback_item_applications(
        feedback_id: str,
        principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
    ) -> list[FeedbackApplication]:
        return await feedback.applications(principal, feedback_id=feedback_id)

    @app.post(
        "/v1/feedback/{feedback_id}/revoke", response_model=FeedbackRecord, tags=["feedback"]
    )
    async def revoke_feedback(
        feedback_id: str,
        body: FeedbackRevoke,
        principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
    ) -> FeedbackRecord:
        return await feedback.revoke(feedback_id, body, principal)

    @app.get(
        "/v1/runs/{run_id}/feedback", response_model=list[FeedbackApplication], tags=["feedback"]
    )
    async def run_feedback_applications(
        run_id: str,
        principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
    ) -> list[FeedbackApplication]:
        return await feedback.applications(principal, run_id=run_id)

    # P1-005A: immutable DRAFT versions, approval validity and (mock) publication state.
    def _drafts():
        if selected_container.drafts is None:
            raise RfaError("not_implemented", "DRAFT lifecycle이 구성되지 않았습니다.")
        return selected_container.drafts

    @app.get("/v1/runs/{run_id}/draft", response_model=DraftState, tags=["drafts"])
    async def get_draft_state(
        run_id: str,
        principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
    ) -> DraftState:
        return await _drafts().state(run_id, principal)

    @app.post(
        "/v1/runs/{run_id}/draft/edits",
        response_model=DraftState,
        status_code=201,
        tags=["drafts"],
    )
    async def edit_draft(
        run_id: str,
        body: DraftEditRequest,
        principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
    ) -> DraftState:
        return await _drafts().edit(run_id, body, principal)

    @app.post("/v1/runs/{run_id}/draft/review", response_model=DraftState, tags=["drafts"])
    async def request_draft_review(
        run_id: str,
        principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
    ) -> DraftState:
        return await _drafts().request_review(run_id, principal)

    @app.post(
        "/v1/runs/{run_id}/publication", response_model=PublicationReceipt, tags=["drafts"]
    )
    async def publish_draft(
        run_id: str,
        body: PublishRequest,
        principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
    ) -> PublicationReceipt:
        return await _drafts().publish(run_id, body, principal)

    @app.get(
        "/v1/runs/{run_id}/publication", response_model=PublicationReceipt, tags=["drafts"]
    )
    async def get_publication(
        run_id: str,
        principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
    ) -> PublicationReceipt:
        return await _drafts().query(run_id, principal)

    # P0-022: owner schedule intent. The API stores intent only; `rfa scheduler` executes.
    def _schedules():
        if selected_container.schedules is None:
            raise RfaError("not_implemented", "예약 기능이 구성되지 않았습니다.")
        return selected_container.schedules

    @app.post("/v1/schedules", response_model=Schedule, status_code=201, tags=["schedules"])
    async def create_schedule(
        body: ScheduleCreate,
        principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
    ) -> Schedule:
        return await _schedules().create(body, principal)

    @app.get("/v1/schedules", response_model=list[Schedule], tags=["schedules"])
    async def list_schedules(
        principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
    ) -> list[Schedule]:
        return await _schedules().list(principal)

    @app.get("/v1/schedules/{schedule_id}", response_model=Schedule, tags=["schedules"])
    async def get_schedule(
        schedule_id: str,
        principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
    ) -> Schedule:
        return await _schedules().get(schedule_id, principal)

    @app.post(
        "/v1/schedules/{schedule_id}/disable", response_model=Schedule, tags=["schedules"]
    )
    async def disable_schedule(
        schedule_id: str,
        principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
    ) -> Schedule:
        return await _schedules().disable(schedule_id, principal)

    @app.post("/v1/schedules/{schedule_id}/enable", response_model=Schedule, tags=["schedules"])
    async def enable_schedule(
        schedule_id: str,
        principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
    ) -> Schedule:
        return await _schedules().enable(schedule_id, principal)

    @app.post("/v1/schedules/{schedule_id}/cancel", response_model=Schedule, tags=["schedules"])
    async def cancel_schedule(
        schedule_id: str,
        principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
    ) -> Schedule:
        return await _schedules().cancel(schedule_id, principal)

    @app.get(
        "/v1/schedules/{schedule_id}/runs", response_model=list[JobRun], tags=["schedules"]
    )
    async def list_schedule_runs(
        schedule_id: str,
        principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
    ) -> list[JobRun]:
        return await _schedules().runs(schedule_id, principal)

    # P0-024: owner-only notification history (no push/external channel in P0).
    @app.get("/v1/notifications", response_model=list[Notification], tags=["schedules"])
    async def list_notifications(
        principal: Annotated[TrustedPrincipal, Depends(resolve_principal)],
        include_held: bool = False,
    ) -> list[Notification]:
        return await _schedules().notifications(principal, include_held=include_held)

    return app
