"""Replaceable local stand-in for manual review, mock publication receipts and READ tools.

mode=mock, simulated=true. This is not the teammate Response/Tool service, not
MCP, not a real publication client, and grants no real share permission. The
existing ResponseHttpAdapter/ToolHttpAdapter consume it unchanged.
"""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, Query
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from rfa_mas.contracts import (
    DraftBundle,
    DraftBundleV11,
    DraftTarget,
    ResultStatus,
    ReviewDecision,
    ReviewStatus,
    SimulationScenario,
    StructuredError,
    ToolEffect,
    ToolRequest,
    ToolResult,
)
from rfa_mas.contracts.common import Digest
from rfa_mas.reference.local_response_store import (
    SERVICE_NAME,
    LocalPublicationView,
    LocalResponseStore,
    LocalReviewView,
)
from rfa_mas.reference.local_security import (
    LocalBoundaryMiddleware,
    LocalServiceBoundary,
    LocalServiceError,
    fingerprint,
    install_safe_error_handlers,
    is_opaque_id,
    require_idempotency_key,
    verified_owner_dependency,
)

TOOL_ADAPTER_NAME = "local-reference-tool"
SUPPORTED_CONTRACT_VERSIONS = ("1.0", "1.1")


# ---------- module-only request envelopes (DTOs themselves are the shared originals) ----------
class LegacyReviewSubmission(BaseModel):
    """1.0 wire envelope used by ResponseHttpAdapter. Scenario never auto-approves here."""

    model_config = ConfigDict(extra="forbid")

    draft: DraftBundle
    simulation_scenario: SimulationScenario = SimulationScenario.SUCCESS


class LocalReviewSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft: DraftBundleV11


class LocalDecisionRequest(BaseModel):
    """Binds a manual decision to the latest exact draft. Carries no approver identity."""

    model_config = ConfigDict(extra="forbid")

    draft_version: int = Field(ge=1)
    content_hash: Digest
    payload_hash: Digest | None = None
    target: DraftTarget
    decision: Literal["approved", "rejected", "revision_requested"]


class LocalPublicationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_id: str = Field(min_length=1, max_length=160)
    draft_id: str = Field(min_length=1, max_length=160)
    version: int = Field(ge=1)
    payload_hash: Digest
    simulate_outcome: Literal["succeeded", "outcome_unknown"] = "succeeded"


class LocalHealth(BaseModel):
    status: Literal["ok"] = "ok"
    service: Literal["rfa-local-response"] = SERVICE_NAME
    mode: Literal["mock"] = "mock"
    simulated: Literal[True] = True
    supported_contract_versions: tuple[str, ...] = SUPPORTED_CONTRACT_VERSIONS


class LocalCapabilities(BaseModel):
    service: Literal["rfa-local-response"] = SERVICE_NAME
    mode: Literal["mock"] = "mock"
    authority: Literal["reference_mock"] = "reference_mock"
    simulated: Literal[True] = True
    supported_contract_versions: tuple[str, ...] = SUPPORTED_CONTRACT_VERSIONS
    features: tuple[str, ...] = (
        "review_submit_1_0_pending",
        "manual_review_decision",
        "review_submit_1_1",
        "mock_publication_receipt",
        "read_tools",
    )
    read_tools: tuple[str, ...]
    real_integrations: dict[str, bool] = Field(
        default_factory=lambda: {
            "teammate_response_service": False,
            "mcp": False,
            "external_publication": False,
            "external_writes": False,
            "real_share_permission": False,
            "openshell": False,
        }
    )


# ---------- documented synthetic READ tools (no shell, URL, file path, network or WRITE) ----------
class _GlossaryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    term: str = Field(min_length=1, max_length=100)


class _TextStatsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(max_length=10_000)


_GLOSSARY = {
    "draft": "검토 전 초안. 승인 전에는 공유/게시할 수 없습니다.",
    "approval": "특정 초안 버전/hash/대상에 결합된 수동 승인.",
    "receipt": "모의 게시 결과 기록. 실제 외부 게시를 뜻하지 않습니다.",
}


def _glossary_lookup(args: _GlossaryArgs) -> dict[str, Any]:
    definition = _GLOSSARY.get(args.term.strip().lower())
    return {"found": definition is not None, "definition": definition, "synthetic": True}


def _text_stats(args: _TextStatsArgs) -> dict[str, Any]:
    return {
        "characters": len(args.text),
        "lines": len(args.text.splitlines()),
        "words": len(args.text.split()),
        "synthetic": True,
    }


READ_TOOLS: dict[str, tuple[type[BaseModel], Callable[[Any], dict[str, Any]]]] = {
    "synthetic.glossary_lookup": (_GlossaryArgs, _glossary_lookup),
    "synthetic.text_stats": (_TextStatsArgs, _text_stats),
}


def _tool_result(
    request: ToolRequest,
    status: ResultStatus,
    *,
    output: dict[str, Any] | None = None,
    code: str | None = None,
    message: str | None = None,
) -> ToolResult:
    return ToolResult(
        request_id=request.request_id,
        trace_id=request.trace_id,
        run_id=request.run_id,
        agent_id=request.agent_id,
        domain_id=request.domain_id,
        idempotency_key=request.idempotency_key,
        status=status,
        output=output or {},
        error=(
            None
            if code is None
            else StructuredError(
                code=code,
                retryable=False,
                message=message or "",
                request_id=request.request_id,
                trace_id=request.trace_id,
                run_id=request.run_id,
            )
        ),
        simulated=True,
        adapter=TOOL_ADAPTER_NAME,
    )


def run_read_tool(request: ToolRequest) -> ToolResult:
    """Deterministic allowlist dispatch. Denials never reflect tool names or arguments."""

    if request.effect != ToolEffect.READ:
        return _tool_result(
            request,
            ResultStatus.DENIED,
            code="external_writes_disabled",
            message="local stand-in은 WRITE tool을 실행하지 않습니다.",
        )
    entry = READ_TOOLS.get(request.tool_name)
    if entry is None:
        return _tool_result(
            request,
            ResultStatus.DENIED,
            code="tool_not_allowed",
            message="문서화된 합성 READ allowlist에 없는 tool입니다.",
        )
    args_model, handler = entry
    try:
        args = args_model.model_validate(request.arguments)
    except ValidationError:
        return _tool_result(
            request,
            ResultStatus.DENIED,
            code="invalid_tool_arguments",
            message="tool 인자가 허용된 형식이 아닙니다.",
        )
    return _tool_result(request, ResultStatus.SUCCEEDED, output=handler(args))


def create_local_response_app(
    *,
    db_path: Path,
    boundary: LocalServiceBoundary,
    approval_ttl: timedelta = timedelta(minutes=30),
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    """Build the local review/publication/tool stand-in with fixed server-side injection.

    db_path must be a dedicated file (never the core DB). The owner and token come
    from 'boundary' only; JSON bodies cannot claim identity or approval authority.
    """

    if approval_ttl <= timedelta(0):
        raise ValueError("approval_ttl must be positive")
    store = LocalResponseStore(db_path, clock=clock or (lambda: datetime.now(UTC)))
    store.initialize()
    owner = verified_owner_dependency(boundary)
    Owner = Annotated[str, Depends(owner)]
    IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]

    app = FastAPI(
        title="RFA local review/publication/tool stand-in (mock)",
        version="1.1-local",
        docs_url=None,
        redoc_url=None,
    )
    app.state.local_response_store = store
    install_safe_error_handlers(app)
    app.add_middleware(LocalBoundaryMiddleware, boundary=boundary)

    def draft_path(draft_id: str) -> str:
        if not is_opaque_id(draft_id):
            raise LocalServiceError("not_found", 404)
        return draft_id

    @app.get("/healthz", response_model=LocalHealth, tags=["operations"])
    async def health() -> LocalHealth:
        return LocalHealth()

    @app.get("/v1/capabilities", response_model=LocalCapabilities, tags=["operations"])
    async def capabilities(_: Owner) -> LocalCapabilities:
        return LocalCapabilities(read_tools=tuple(sorted(READ_TOOLS)))

    @app.post("/v1/reviews", response_model=ReviewDecision, tags=["reviews-1.0"])
    async def submit_legacy(
        body: LegacyReviewSubmission, _: Owner, idempotency_key: IdempotencyKey = None
    ) -> ReviewDecision:
        key = require_idempotency_key(idempotency_key)
        result = await asyncio.to_thread(
            store.submit,
            body.draft,
            idempotency_key=key,
            request_fingerprint=fingerprint(body.model_dump(mode="json")),
            scenario=body.simulation_scenario,
        )
        assert isinstance(result, ReviewDecision)
        return result

    @app.get("/v1/reviews/{draft_id}", response_model=ReviewDecision, tags=["reviews-1.0"])
    async def get_legacy(draft_id: str, _: Owner) -> ReviewDecision:
        result = await asyncio.to_thread(store.get_legacy, draft_path(draft_id))
        if result is None:
            raise LocalServiceError("not_found", 404)
        return result

    @app.post("/v1/local/reviews", response_model=LocalReviewView, tags=["reviews-1.1"])
    async def submit_v11(
        body: LocalReviewSubmission, _: Owner, idempotency_key: IdempotencyKey = None
    ) -> LocalReviewView:
        key = require_idempotency_key(idempotency_key)
        result = await asyncio.to_thread(
            store.submit,
            body.draft,
            idempotency_key=key,
            request_fingerprint=fingerprint(body.model_dump(mode="json")),
            scenario=None,
        )
        assert isinstance(result, LocalReviewView)
        return result

    @app.get("/v1/local/reviews", response_model=list[LocalReviewView], tags=["reviews-1.1"])
    async def list_reviews(
        _: Owner,
        decision: ReviewStatus | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> list[LocalReviewView]:
        return await asyncio.to_thread(
            store.list_views, decision.value if decision else None, limit
        )

    @app.get("/v1/local/reviews/{draft_id}", response_model=LocalReviewView, tags=["reviews-1.1"])
    async def get_review(draft_id: str, _: Owner) -> LocalReviewView:
        result = await asyncio.to_thread(store.get_view, draft_path(draft_id))
        if result is None:
            raise LocalServiceError("not_found", 404)
        return result

    @app.post(
        "/v1/local/reviews/{draft_id}/decision",
        response_model=LocalReviewView,
        tags=["reviews-1.1"],
    )
    async def decide(
        draft_id: str,
        body: LocalDecisionRequest,
        approver_id: Owner,
        idempotency_key: IdempotencyKey = None,
    ) -> LocalReviewView:
        key = require_idempotency_key(idempotency_key)
        checked_id = draft_path(draft_id)
        return await asyncio.to_thread(
            store.decide,
            checked_id,
            idempotency_key=key,
            request_fingerprint=fingerprint(
                {"draft_id": checked_id, **body.model_dump(mode="json")}
            ),
            draft_version=body.draft_version,
            content_hash=body.content_hash,
            payload_hash=body.payload_hash,
            target=body.target,
            decision=ReviewStatus(body.decision),
            approver_id=approver_id,
            approval_ttl=approval_ttl,
        )

    @app.post("/v1/local/publications", response_model=LocalPublicationView, tags=["publications"])
    async def publish(
        body: LocalPublicationRequest, _: Owner, idempotency_key: IdempotencyKey = None
    ) -> LocalPublicationView:
        key = require_idempotency_key(idempotency_key)
        if not (is_opaque_id(body.approval_id) and is_opaque_id(body.draft_id)):
            raise LocalServiceError("approval_not_found", 404)
        return await asyncio.to_thread(
            store.publish,
            idempotency_key=key,
            request_fingerprint=fingerprint(body.model_dump(mode="json")),
            approval_id=body.approval_id,
            draft_id=body.draft_id,
            version=body.version,
            payload_hash=body.payload_hash,
            simulate_outcome=body.simulate_outcome,
        )

    @app.get(
        "/v1/local/publications/{publication_id}",
        response_model=LocalPublicationView,
        tags=["publications"],
    )
    async def get_publication(publication_id: str, _: Owner) -> LocalPublicationView:
        if not is_opaque_id(publication_id):
            raise LocalServiceError("not_found", 404)
        result = await asyncio.to_thread(store.get_publication, publication_id)
        if result is None:
            raise LocalServiceError("not_found", 404)
        return result

    @app.post("/v1/tools/execute", response_model=ToolResult, tags=["tools"])
    async def execute_tool(
        request: ToolRequest, _: Owner, idempotency_key: IdempotencyKey = None
    ) -> ToolResult:
        key = require_idempotency_key(request.idempotency_key)
        if idempotency_key is not None and idempotency_key != key:
            raise LocalServiceError("idempotency_conflict", 409)
        return await asyncio.to_thread(
            store.tool_once,
            idempotency_key=key,
            request_fingerprint=fingerprint(request.model_dump(mode="json")),
            compute=lambda: run_read_tool(request),
        )

    return app
