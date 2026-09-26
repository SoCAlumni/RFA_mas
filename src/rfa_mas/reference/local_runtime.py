"""Replaceable local runtime stand-in: TeamSpec lifecycle + allow-listed synthetic handlers.

mode=local, results simulated=true. Not OpenShell, not an OS/network sandbox, not
MCP, not the teammate runtime, and not a declaration that the core's native
runtime HTTP capability is ready. RuntimeHttpAdapter consumes run/status/cancel
unchanged; prepare/cleanup routes are defined by this module's OpenAPI.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from rfa_mas.contracts import (
    AgentSpec,
    Audience,
    DomainId,
    DraftBundle,
    EvidenceBundle,
    EvidenceItem,
    EvidenceRef,
    ResultStatus,
    SourceLocation,
    TaskRequest,
    TaskResult,
    TeamInstance,
    TeamMember,
    TeamSpec,
    TrustedPrincipal,
    WorkRequest,
    sha256_text,
)
from rfa_mas.reference.local_runtime_store import (
    ADAPTER_NAME,
    SERVICE_NAME,
    LocalRunJournal,
    LocalRuntimeStore,
    MemberHook,
    identity_of,
    task_result,
)
from rfa_mas.reference.local_security import (
    LocalBoundaryMiddleware,
    LocalServiceBoundary,
    LocalServiceError,
    fingerprint,
    install_safe_error_handlers,
    is_opaque_id,
    register_safe_messages,
    require_idempotency_key,
    verified_owner_dependency,
)

SUPPORTED_CONTRACT_VERSIONS = ("1.0", "1.1")
TEAM_ROLES = frozenset(
    {
        "supervisor",
        "paper_scout",
        "experiment_runner",
        "result_analyst",
        "source_scout",
        "evidence_reviewer",
    }
)
# The core's documented capability vocabulary (domain supervisors and team roles).
DEFAULT_CAPABILITIES = frozenset(
    {
        "evidence_search",
        "benchmark_analysis",
        "research_synthesis",
        "draft_generation",
        "experiment_run",
        "result_analysis",
        "evidence_review",
    }
)
# Names of P1-008C's synthetic READ tools; no shell/URL/file/WRITE tool exists.
DEFAULT_TOOLS = frozenset({"synthetic.glossary_lookup", "synthetic.text_stats"})

register_safe_messages(
    {
        "openshell_not_supported": (
            "local stand-in은 OpenShell/sandbox runtime을 지원하지 않습니다."
        ),
        "team_spec_incomplete": "검증된 definition digest와 실행 예산이 필요합니다.",
        "owner_mismatch": "팀 owner가 이 설치의 인증된 owner와 다릅니다.",
        "domain_not_allowed": "허용되지 않은 domain입니다.",
        "role_not_allowed": "서버가 허용하지 않은 역할입니다.",
        "tool_not_allowed": "서버가 허용하지 않은 tool입니다.",
        "capability_not_supported": "지원하지 않는 capability입니다.",
        "budget_exceeded": "구성원 예산이 승인된 팀 실행 예산을 초과합니다.",
    }
)


@dataclass(frozen=True)
class LocalRuntimePolicy:
    """Server-owned allowlists. A caller's TeamSpec/AgentSpec never grants authority."""

    allowed_domains: frozenset[DomainId] = frozenset(DomainId)
    allowed_roles: frozenset[str] = TEAM_ROLES
    allowed_tools: frozenset[str] = DEFAULT_TOOLS
    allowed_capabilities: frozenset[str] = DEFAULT_CAPABILITIES


HandlerFunction = Callable[[AgentSpec, TaskRequest], Awaitable[TaskResult]]


@dataclass(frozen=True)
class SyntheticHandler:
    """Server-registered synthetic handler. Callers select only a registered name."""

    run: HandlerFunction
    required_capability: str | None = None
    description: str = field(default="", compare=False)


class RuntimeSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec: AgentSpec
    request: TaskRequest


class TeamPrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec: TeamSpec


class LocalRuntimeHealth(BaseModel):
    status: Literal["ok"] = "ok"
    service: Literal["rfa-local-runtime"] = SERVICE_NAME
    mode: Literal["local"] = "local"
    handlers: Literal["synthetic"] = "synthetic"
    simulated: Literal[True] = True
    sandbox: Literal[False] = False
    supported_contract_versions: tuple[str, ...] = SUPPORTED_CONTRACT_VERSIONS


class LocalRuntimeCapabilities(BaseModel):
    service: Literal["rfa-local-runtime"] = SERVICE_NAME
    mode: Literal["local"] = "local"
    simulated: Literal[True] = True
    sandbox: Literal[False] = False
    supported_contract_versions: tuple[str, ...] = SUPPORTED_CONTRACT_VERSIONS
    supported_runtime_kinds: tuple[str, ...] = ("local",)
    handlers: tuple[str, ...]
    allowed_roles: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    allowed_capabilities: tuple[str, ...]
    restart_policy: Literal["inflight_marked_unknown_no_auto_replay"] = (
        "inflight_marked_unknown_no_auto_replay"
    )
    real_integrations: dict[str, bool] = Field(
        default_factory=lambda: {
            "openshell": False,
            "os_sandbox": False,
            "teammate_runtime_service": False,
            "core_runtime_http_capability": False,
            "mcp": False,
            "network_tools": False,
            "shell_or_file_tools": False,
        }
    )


# ---------------- default synthetic handlers ----------------
_ROLE_NOTE_PAYLOAD_KEYS: frozenset[str] = frozenset()
_DOMAIN_PAYLOAD_KEYS = frozenset({"work_request", "principal"})


def _failed(request: TaskRequest, code: str, message: str, **output: Any) -> TaskResult:
    return task_result(
        identity_of(request), ResultStatus.FAILED, code=code, message=message, output=output
    )


async def _role_note(spec: AgentSpec, request: TaskRequest) -> TaskResult:
    if set(request.payload) - _ROLE_NOTE_PAYLOAD_KEYS:
        return _failed(request, "invalid_task_payload", "허용되지 않은 payload입니다.", steps=0)
    return task_result(
        identity_of(request),
        ResultStatus.SUCCEEDED,
        output={
            "role_note": "로컬 합성 handler가 역할 실행 자리만 기록했습니다. 실제 작업은 없습니다.",
            "steps": 1,
            "synthetic": True,
        },
    )


async def _domain_task(spec: AgentSpec, request: TaskRequest) -> TaskResult:
    """Same synthetic public-evidence DRAFT shape as the reference contract fixture."""

    if set(request.payload) - _DOMAIN_PAYLOAD_KEYS:
        return _failed(request, "invalid_task_payload", "허용되지 않은 payload입니다.", steps=0)
    try:
        work = WorkRequest.model_validate(request.payload["work_request"])
    except (KeyError, TypeError, ValidationError):
        return _failed(
            request, "invalid_task_payload", "유효한 work_request payload가 필요합니다.", steps=0
        )
    if (
        work.request_id != request.request_id
        or work.trace_id != request.trace_id
        or work.run_id != request.run_id
        or (work.domain_id is not None and work.domain_id != request.domain_id)
        or Audience.PUBLIC not in spec.allowed_audiences
    ):
        return _failed(
            request,
            "task_payload_binding_mismatch",
            "work_request payload가 task 식별자 또는 허용 범위와 일치하지 않습니다.",
            steps=0,
        )
    if spec.max_steps < 3:
        return _failed(
            request, "budget_exceeded", "domain task step 예산이 부족합니다.", steps=spec.max_steps
        )
    evidence_text = "이 근거는 local runtime stand-in 검증용 합성 공개 자료입니다."
    item = EvidenceItem(
        source_id="local-runtime-public-evidence",
        source_revision="1",
        location=SourceLocation(
            uri="fixture://local-runtime/public-evidence", section="synthetic-public-evidence"
        ),
        audience=Audience.PUBLIC,
        excerpt=evidence_text,
        content_hash=sha256_text(evidence_text),
        policy_version="local-runtime-fixture-v1",
    )
    ids = identity_of(request)
    evidence = EvidenceBundle(
        **ids,
        items=(item,),
        policy_version="local-runtime-fixture-v1",
        simulated=True,
        adapter=ADAPTER_NAME,
    )
    content = (
        "local runtime stand-in이 합성 공개 근거로 생성한 모의 DRAFT입니다. "
        "실제 NVIDIA 모델, 외부 서비스, OpenShell을 호출하지 않았습니다."
    )
    draft = DraftBundle(
        **ids,
        content_hash=sha256_text(content),
        target=work.target,
        audience=work.target.audience,
        policy_version="local-runtime-fixture-v1",
        allowed_evidence=(
            EvidenceRef(
                source_id=item.source_id,
                source_revision=item.source_revision,
                location=item.location,
                audience=item.audience,
                content_hash=item.content_hash,
            ),
        ),
        content=content,
        simulated=True,
        adapter=ADAPTER_NAME,
    )
    return task_result(
        ids,
        ResultStatus.SUCCEEDED,
        output={
            "draft": draft.model_dump(mode="json"),
            "evidence": evidence.model_dump(mode="json"),
            "steps": 3,
            "synthetic": True,
        },
    )


DEFAULT_HANDLERS: Mapping[str, SyntheticHandler] = {
    "domain_task": SyntheticHandler(
        _domain_task, "draft_generation", "합성 공개 근거 DRAFT (reference fixture와 같은 형태)"
    ),
    "synthetic.role_note": SyntheticHandler(_role_note, None, "역할 실행 자리 기록"),
}


def _noop_member(member: TeamMember) -> None:
    """Local metadata only: no process, container or sandbox is provisioned."""


def _team_rejection(spec: TeamSpec, owner_id: str, policy: LocalRuntimePolicy) -> str | None:
    if spec.template.runtime_kind != "local":
        return "openshell_not_supported"
    if not spec.definition_digest or spec.execution_budget is None:
        return "team_spec_incomplete"
    if spec.owner_id != owner_id:
        return "owner_mismatch"
    if spec.domain_id not in policy.allowed_domains:
        return "domain_not_allowed"
    if any(member.role not in policy.allowed_roles for member in spec.members):
        return "role_not_allowed"
    if any(set(member.tool_names) - policy.allowed_tools for member in spec.members):
        return "tool_not_allowed"
    capabilities = set(spec.template.required_capabilities).union(
        *(member.spec.capabilities for member in spec.members)
    )
    if capabilities - policy.allowed_capabilities:
        return "capability_not_supported"
    budget = spec.execution_budget
    if any(
        member.spec.max_steps > budget.max_steps
        or member.spec.max_tool_calls > budget.max_tool_calls
        for member in spec.members
    ):
        return "budget_exceeded"
    return None


_TEAM_STATUS = {
    "openshell_not_supported": 501,
    "team_spec_incomplete": 422,
}


def _run_denial(
    spec: AgentSpec,
    request: TaskRequest,
    *,
    owner_id: str,
    policy: LocalRuntimePolicy,
    handlers: Mapping[str, SyntheticHandler],
) -> TaskResult | None:
    ids = identity_of(request)

    def denied(code: str, message: str) -> TaskResult:
        return task_result(ids, ResultStatus.DENIED, code=code, message=message)

    if spec.agent_id != request.agent_id or spec.domain_id != request.domain_id:
        return task_result(
            ids,
            ResultStatus.FAILED,
            code="agent_spec_binding_mismatch",
            message="AgentSpec과 TaskRequest의 agent 또는 domain 식별자가 일치하지 않습니다.",
        )
    if request.domain_id not in policy.allowed_domains:
        return denied("domain_not_allowed", "허용되지 않은 domain입니다.")
    if set(spec.capabilities) - policy.allowed_capabilities:
        return denied("capability_not_supported", "지원하지 않는 capability입니다.")
    handler = handlers.get(request.task_type)
    if handler is None:
        return denied("handler_not_allowed", "서버에 등록된 합성 handler가 아닙니다.")
    if handler.required_capability and handler.required_capability not in spec.capabilities:
        return denied("capability_denied", "agent에 필요한 capability가 없습니다.")
    if "principal" in request.payload:
        # A payload principal is an untrusted claim: it may only agree with the server owner.
        try:
            claimed = TrustedPrincipal.model_validate(request.payload["principal"])
        except (TypeError, ValidationError):
            claimed = None
        if claimed is None or not claimed.authenticated or claimed.user_id != owner_id:
            return denied("identity_mismatch", "요청 identity가 설치 owner와 일치하지 않습니다.")
    return None


def create_local_runtime_app(
    *,
    db_path: Path,
    boundary: LocalServiceBoundary,
    policy: LocalRuntimePolicy | None = None,
    handlers: Mapping[str, SyntheticHandler] | None = None,
    prepare_member: MemberHook = _noop_member,
    cleanup_member: MemberHook = _noop_member,
    handler_timeout_seconds: float = 30.0,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    """Build the local runtime stand-in with fixed server-side injection.

    db_path is a dedicated file. On construction every journaled in-flight run is
    marked unknown and never replayed. Owner/token come only from 'boundary'.
    """

    if handler_timeout_seconds <= 0:
        raise ValueError("handler_timeout_seconds must be positive")
    selected_policy = policy or LocalRuntimePolicy()
    registry = dict(DEFAULT_HANDLERS if handlers is None else handlers)
    store = LocalRuntimeStore(db_path, clock=clock or (lambda: datetime.now(UTC)))
    recovered = store.initialize()
    owner = verified_owner_dependency(boundary)
    Owner = Annotated[str, Depends(owner)]
    IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]
    inflight: dict[str, asyncio.Task[TaskResult]] = {}
    start_lock = asyncio.Lock()

    app = FastAPI(
        title="RFA local runtime stand-in (synthetic handlers, not a sandbox)",
        version="1.1-local",
        docs_url=None,
        redoc_url=None,
    )
    app.state.local_runtime_store = store
    app.state.recovered_runs = recovered
    install_safe_error_handlers(app)
    app.add_middleware(LocalBoundaryMiddleware, boundary=boundary)

    def checked_id(value: str) -> str:
        if not is_opaque_id(value):
            raise LocalServiceError("not_found", 404)
        return value

    async def execute(spec: AgentSpec, request: TaskRequest, handler: SyntheticHandler):
        ids = identity_of(request)
        try:
            try:
                raw = await asyncio.wait_for(
                    handler.run(spec, request), timeout=handler_timeout_seconds
                )
                result = TaskResult.model_validate(
                    raw.model_dump() | {"simulated": True, "adapter": ADAPTER_NAME}
                )
                if identity_of(request) != {
                    "request_id": result.request_id,
                    "trace_id": result.trace_id,
                    "run_id": result.run_id,
                    "agent_id": result.agent_id,
                    "domain_id": result.domain_id.value,
                }:
                    raise ValueError("handler identity mismatch")
            except TimeoutError:
                result = task_result(
                    ids,
                    ResultStatus.TIMED_OUT,
                    code="runtime_timeout",
                    message="로컬 합성 handler 실행 시간이 초과되었습니다.",
                )
            except (ValidationError, ValueError, TypeError, AttributeError):
                result = task_result(
                    ids,
                    ResultStatus.FAILED,
                    code="handler_contract_error",
                    message="합성 handler 결과가 TaskResult 계약을 충족하지 않습니다.",
                )
            except Exception:
                result = task_result(
                    ids,
                    ResultStatus.FAILED,
                    code="handler_error",
                    message="합성 handler 실행에 실패했습니다.",
                )
            return await asyncio.to_thread(store.finish_run, request.run_id, result)
        finally:
            if inflight.get(request.idempotency_key) is asyncio.current_task():
                inflight.pop(request.idempotency_key, None)

    @app.get("/healthz", response_model=LocalRuntimeHealth, tags=["operations"])
    async def health() -> LocalRuntimeHealth:
        return LocalRuntimeHealth()

    @app.get("/v1/capabilities", response_model=LocalRuntimeCapabilities, tags=["operations"])
    async def capabilities(_: Owner) -> LocalRuntimeCapabilities:
        return LocalRuntimeCapabilities(
            handlers=tuple(sorted(registry)),
            allowed_roles=tuple(sorted(selected_policy.allowed_roles)),
            allowed_tools=tuple(sorted(selected_policy.allowed_tools)),
            allowed_capabilities=tuple(sorted(selected_policy.allowed_capabilities)),
        )

    @app.post("/v1/runtime/teams", response_model=TeamInstance, tags=["teams"])
    async def prepare(
        body: TeamPrepareRequest, owner_id: Owner, idempotency_key: IdempotencyKey = None
    ) -> TeamInstance:
        key = require_idempotency_key(idempotency_key)
        rejection = _team_rejection(body.spec, owner_id, selected_policy)
        if rejection is not None:
            raise LocalServiceError(rejection, _TEAM_STATUS.get(rejection, 403))
        return await asyncio.to_thread(
            store.prepare,
            body.spec,
            owner_id=owner_id,
            idempotency_key=key,
            request_fingerprint=fingerprint({"prepare": body.spec.model_dump(mode="json")}),
            prepare_member=prepare_member,
        )

    @app.get("/v1/runtime/teams/{team_id}", response_model=TeamInstance, tags=["teams"])
    async def get_team(team_id: str, owner_id: Owner) -> TeamInstance:
        result = await asyncio.to_thread(store.get_team, checked_id(team_id), owner_id=owner_id)
        if result is None:
            raise LocalServiceError("not_found", 404)
        return result

    @app.post("/v1/runtime/teams/{team_id}/cleanup", response_model=TeamInstance, tags=["teams"])
    async def cleanup(
        team_id: str, owner_id: Owner, idempotency_key: IdempotencyKey = None
    ) -> TeamInstance:
        key = require_idempotency_key(idempotency_key)
        team = checked_id(team_id)
        return await asyncio.to_thread(
            store.cleanup,
            team,
            owner_id=owner_id,
            idempotency_key=key,
            request_fingerprint=fingerprint({"cleanup": team}),
            cleanup_member=cleanup_member,
        )

    @app.post("/v1/runtime/tasks", response_model=TaskResult, tags=["tasks"])
    async def run_task(
        body: RuntimeSubmission, owner_id: Owner, idempotency_key: IdempotencyKey = None
    ) -> TaskResult:
        spec, request = body.spec, body.request
        key = require_idempotency_key(request.idempotency_key)
        if idempotency_key is not None and idempotency_key != key:
            raise LocalServiceError("idempotency_conflict", 409)
        checked_id(request.run_id)
        denial = _run_denial(
            spec, request, owner_id=owner_id, policy=selected_policy, handlers=registry
        )
        async with start_lock:
            decision = await asyncio.to_thread(
                store.begin_run,
                spec,
                request,
                owner_id=owner_id,
                request_fingerprint=fingerprint(body.model_dump(mode="json")),
                denial=denial,
                task_type_label=(
                    request.task_type if request.task_type in registry else "unregistered"
                ),
            )
            if decision.kind == "result":
                assert decision.result is not None
                return decision.result
            if decision.kind == "wait":
                task = inflight.get(key)
                if task is None:
                    raise LocalServiceError("run_in_progress", 409)
            else:
                task = asyncio.create_task(
                    execute(spec, request, registry[request.task_type]), name=request.run_id
                )
                inflight[key] = task
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if task.cancelled() and (current is None or not current.cancelling()):
                # Cancelled through the cancel endpoint; return the durable receipt.
                stored = await asyncio.to_thread(
                    store.get_result, request.run_id, owner_id=owner_id
                )
                if stored is not None:
                    return stored
            raise

    @app.get("/v1/runtime/tasks/{run_id}", response_model=TaskResult, tags=["tasks"])
    async def task_status(run_id: str, owner_id: Owner) -> TaskResult:
        # Running tasks have no TaskResult yet (same as the in-process LocalRuntime).
        result = await asyncio.to_thread(store.get_result, checked_id(run_id), owner_id=owner_id)
        if result is None:
            raise LocalServiceError("not_found", 404)
        return result

    @app.get("/v1/runtime/tasks/{run_id}/journal", response_model=LocalRunJournal, tags=["tasks"])
    async def task_journal(run_id: str, owner_id: Owner) -> LocalRunJournal:
        result = await asyncio.to_thread(store.get_journal, checked_id(run_id), owner_id=owner_id)
        if result is None:
            raise LocalServiceError("not_found", 404)
        return result

    @app.post("/v1/runtime/tasks/{run_id}/cancel", response_model=TaskResult, tags=["tasks"])
    async def cancel_task(run_id: str, owner_id: Owner) -> TaskResult:
        result, was_running = await asyncio.to_thread(
            store.cancel_run, checked_id(run_id), owner_id=owner_id
        )
        if result is None:
            raise LocalServiceError("not_found", 404)
        if was_running:
            for task in list(inflight.values()):
                if task.get_name() == run_id:
                    task.cancel()
        return result

    return app
