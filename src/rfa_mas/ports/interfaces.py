from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from rfa_mas.contracts import (
    AgentSpec,
    Audience,
    ContextBundle,
    ContextRequest,
    DomainId,
    DraftBundle,
    EvaluationCase,
    EvidenceBundle,
    ExecutionContext,
    JudgeAssessment,
    KnowledgeDelete,
    KnowledgeDocument,
    KnowledgeRevision,
    KnowledgeWrite,
    SourceMetadata,
    SourceRead,
    SourceRevisionRef,
    ModelRequest,
    ModelResult,
    ObservationRecord,
    PersistentTask,
    PolicyDecision,
    PolicyRequest,
    RetrievalRequest,
    ReviewDecision,
    RunRecord,
    RunResult,
    SessionDetail,
    SessionRecord,
    SimulationScenario,
    TaskRequest,
    TaskResult,
    TeamInstance,
    TeamLifecycle,
    TeamSpec,
    ToolRequest,
    ToolResult,
    TraceEvent,
    TrustedPrincipal,
    WorkRequest,
    WorkStatus,
)


# -- P0-021 durable effect ledger (port-level DTOs) ------------------------------------
# Internal records, not part of the published RFA-EXTENDED contract. Promoting them to
# contracts/ is a coordinator contract change.
EffectKind = Literal[
    "team_prepare", "team_cleanup", "role_execution", "review_submission", "publication"
]
EffectState = Literal["intent", "inflight", "completed", "outcome_unknown"]
BarrierReason = Literal["cancelled", "permission_revoked"]


class _LedgerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)


class EffectRecord(_LedgerModel):
    """One owner-scoped effectful call: state, payload fingerprint and a result reference.

    The payload itself is never stored here; `result_ref` points at the durable
    receipt that owns the details (publication, role receipt, team slot, review mirror).
    `approval` is the approval/decision mirror bound to the call, never an approval.
    """

    operation_key: str = Field(min_length=1, max_length=512)
    kind: EffectKind
    run_id: str | None = None
    payload_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: EffectState
    outcome: str | None = None
    result_ref: str | None = None
    approval: dict[str, Any] | None = None
    next_action: Literal["none", "query", "wait"]
    created_at: datetime
    updated_at: datetime


class Reconciliation(_LedgerModel):
    """Result of reconciling one unresolved effect by result query (never re-execution)."""

    operation_key: str
    kind: EffectKind
    before: EffectState
    after: EffectState
    method: Literal["result_query", "unsupported"]
    found: bool


class RetryLink(_LedgerModel):
    """Explicit owner retry: a NEW run that names the terminal run it retries."""

    run_id: str
    retry_of: str
    created_at: datetime


class EffectLedger(Protocol):
    """Durable ledger boundary. Run-scoped effects resolve the owner from the stored run.

    Deliberately not named *Port: it is part of the repository, not a teammate/provider
    port, and must not alter the frozen 1.0 port surface.
    """

    async def begin_effect(
        self,
        run_id: str,
        *,
        operation_key: str,
        kind: EffectKind,
        payload_fingerprint: str,
        result_ref: str | None = None,
        approval: dict[str, Any] | None = None,
    ) -> tuple[bool, EffectRecord]: ...

    async def advance_effect(
        self,
        run_id: str,
        operation_key: str,
        *,
        state: EffectState,
        outcome: str | None = None,
        approval: dict[str, Any] | None = None,
    ) -> EffectRecord: ...

    async def effects_by_ref(
        self, run_id: str, *, kind: EffectKind, result_ref: str
    ) -> tuple[EffectRecord, ...]: ...

    async def run_effects(
        self, run_id: str, principal: TrustedPrincipal
    ) -> tuple[EffectRecord, ...]: ...

    async def set_run_barrier(
        self, run_id: str, principal: TrustedPrincipal, reason: BarrierReason
    ) -> str: ...

    async def run_barrier(self, run_id: str, principal: TrustedPrincipal) -> str | None: ...


class ModelPort(Protocol):
    adapter_name: str
    simulated: bool

    async def generate(self, request: ModelRequest) -> ModelResult: ...


class RetrievalPort(Protocol):
    adapter_name: str
    simulated: bool

    async def search(self, request: RetrievalRequest) -> EvidenceBundle: ...

    async def load_context(self, request: ContextRequest) -> ContextBundle:
        """1.1: authorize read/share/endpoint BEFORE ranking or loading each stage.

        Revalidate every parent revision/ACL; even metadata requires authorization.
        Existing 1.0 adapters require explicit capability checks until upgraded.
        """
        ...


class ResponsePort(Protocol):
    adapter_name: str
    simulated: bool

    async def submit_draft(
        self,
        draft: DraftBundle,
        *,
        idempotency_key: str,
        simulation_scenario: SimulationScenario = SimulationScenario.SUCCESS,
    ) -> ReviewDecision: ...

    async def get_decision(self, draft_id: str) -> ReviewDecision | None: ...


class ToolPort(Protocol):
    adapter_name: str
    simulated: bool

    async def execute(self, request: ToolRequest) -> ToolResult: ...


class RuntimePort(Protocol):
    adapter_name: str
    simulated: bool

    async def run(self, spec: AgentSpec, request: TaskRequest) -> TaskResult: ...

    async def status(self, run_id: str) -> TaskResult | None: ...

    async def cancel(self, run_id: str) -> TaskResult: ...

    async def prepare(self, spec: TeamSpec, *, idempotency_key: str) -> TeamInstance:
        """1.1: idempotently prepare one Task's team; clean partial provisioning."""
        ...

    async def cleanup(self, team_id: str, *, idempotency_key: str) -> TeamInstance: ...


TaskHandler = Callable[[AgentSpec, TaskRequest], Awaitable[TaskResult]]


class PolicyPort(Protocol):
    adapter_name: str
    simulated: bool
    policy_version: str

    async def evaluate(self, request: PolicyRequest) -> PolicyDecision: ...


class JudgePort(Protocol):
    adapter_name: str
    simulated: bool

    async def evaluate(self, case: EvaluationCase, result: RunResult) -> JudgeAssessment:
        """Return auxiliary quality scores, never privacy or access decisions."""

        ...


class WorkRepositoryPort(Protocol):
    # Trusted Factory-only mutations; product_task_owners remains owner source.
    async def get_team_lifecycle(
        self, task_id: str, principal: TrustedPrincipal
    ) -> TeamLifecycle: ...

    async def reserve_team(
        self,
        task: PersistentTask,
        spec: TeamSpec,
        principal: TrustedPrincipal,
        *,
        idempotency_key: str,
        request_fingerprint: str,
        mode: str,
        existing_task_id: str | None,
    ) -> tuple[TeamLifecycle, bool]: ...

    async def finish_team_operation(
        self,
        task_id: str,
        principal: TrustedPrincipal,
        *,
        generation: int,
        instance: TeamInstance,
        reason: str,
    ) -> TeamLifecycle: ...

    async def start_team_cleanup(
        self, task_id: str, principal: TrustedPrincipal
    ) -> tuple[TeamLifecycle, bool]: ...

    async def observation_context(
        self, run_id: str, principal: TrustedPrincipal
    ) -> ExecutionContext: ...

    async def observation_alias(
        self, run_id: str, principal: TrustedPrincipal, kind: str, reference: str = ""
    ) -> str: ...

    async def append_observation(
        self,
        run_id: str,
        principal: TrustedPrincipal,
        event: TraceEvent,
        *,
        origin: str,
        provider_ref: str,
        transport: str | None = None,
        provider_kind: str = "builtin",
    ) -> ObservationRecord: ...

    async def get_observation(self, observation_id: str) -> ObservationRecord | None: ...

    async def list_observations(
        self, run_id: str, principal: TrustedPrincipal
    ) -> tuple[ObservationRecord, ...]: ...

    async def initialize(self) -> None: ...

    async def local_principal(self) -> TrustedPrincipal: ...

    async def create_session(self, principal: TrustedPrincipal) -> SessionRecord: ...

    async def list_sessions(self, principal: TrustedPrincipal) -> list[SessionRecord]: ...

    async def get_session(self, session_id: str, principal: TrustedPrincipal) -> SessionDetail: ...

    async def register_task_owner(self, task: PersistentTask) -> None: ...

    async def create_owned_run(
        self,
        request: WorkRequest,
        principal: TrustedPrincipal,
        *,
        session_id: str | None,
        task_id: str | None = None,
        retry_of: str | None = None,
    ) -> SessionRecord: ...

    async def get_owned_run(self, run_id: str, principal: TrustedPrincipal) -> RunRecord: ...

    # P0-021: explicit retry lineage and the stored (owner-authorized) request.
    async def owned_run_request(self, run_id: str, principal: TrustedPrincipal) -> dict: ...

    async def retry_links(
        self, run_id: str, principal: TrustedPrincipal
    ) -> tuple[RetryLink, ...]: ...

    async def create_run(self, request: WorkRequest) -> None: ...

    async def transition_run(self, run_id: str, status: WorkStatus) -> None: ...

    async def save_result(self, result: RunResult) -> None: ...

    async def get_result(self, run_id: str) -> RunResult | None: ...

    async def save_checkpoint(self, run_id: str, node: str, metadata: dict[str, Any]) -> None: ...

    async def upsert_documents(self, documents: list[KnowledgeDocument]) -> None: ...

    async def write_knowledge(
        self,
        request: KnowledgeWrite,
        principal: TrustedPrincipal,
        *,
        policy_version: str,
        source_id: str | None = None,
    ) -> KnowledgeRevision: ...

    async def delete_knowledge(
        self,
        source_id: str,
        request: KnowledgeDelete,
        principal: TrustedPrincipal,
    ) -> KnowledgeRevision: ...

    async def get_knowledge(
        self,
        source_id: str,
        principal: TrustedPrincipal,
        *,
        revision: str | None = None,
    ) -> KnowledgeRevision: ...

    async def list_knowledge(
        self,
        principal: TrustedPrincipal,
        *,
        domain_id: DomainId | None = None,
    ) -> list[KnowledgeRevision]: ...

    async def seed_documents_once(self, documents: list[KnowledgeDocument]) -> None:
        """Atomic installation fixture seed; never resurrect removed/changed sources."""
        ...

    async def list_documents(self, domain_id: str) -> list[KnowledgeDocument]: ...

    async def authorized_metadata(self, domain_id, principal, *, audiences=tuple(Audience),
        target=Audience.OWNER, endpoint="local-preview", policy_version="local-v1",
        query=None, limit=100) -> list[SourceMetadata]: ...

    async def read_sources(self, domain_id, principal, references, *, audiences=tuple(Audience),
        target=Audience.OWNER, endpoint="local-preview", policy_version="local-v1",
        metadata_only=False) -> list[SourceRead] | list[SourceMetadata]: ...

    async def write_derived_knowledge(self, request: KnowledgeWrite, principal: TrustedPrincipal,
        *, parents: tuple[SourceRevisionRef, ...], policy_version: str,
        epistemic_state="inferred") -> KnowledgeRevision: ...


class TracePort(Protocol):
    adapter_name: str
    simulated: bool

    async def emit_observation(self, record: ObservationRecord) -> None: ...

    async def emit(
        self,
        *,
        event: str,
        request_id: str,
        trace_id: str,
        run_id: str,
        status: str,
        metadata: dict[str, Any] | None = None,
    ) -> None: ...

    async def emit_event(self, event: TraceEvent) -> None:
        """1.1 allowlist; no arbitrary metadata and no raw input copied into IDs."""
        ...
