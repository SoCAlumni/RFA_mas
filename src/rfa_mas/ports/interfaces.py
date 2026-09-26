from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from rfa_mas.contracts import (
    AgentSpec,
    ContextBundle,
    ContextRequest,
    DraftBundle,
    EvaluationCase,
    EvidenceBundle,
    ExecutionContext,
    JudgeAssessment,
    KnowledgeDocument,
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
    ) -> SessionRecord: ...

    async def get_owned_run(self, run_id: str, principal: TrustedPrincipal) -> RunRecord: ...

    async def create_run(self, request: WorkRequest) -> None: ...

    async def transition_run(self, run_id: str, status: WorkStatus) -> None: ...

    async def save_result(self, result: RunResult) -> None: ...

    async def get_result(self, run_id: str) -> RunResult | None: ...

    async def save_checkpoint(self, run_id: str, node: str, metadata: dict[str, Any]) -> None: ...

    async def upsert_documents(self, documents: list[KnowledgeDocument]) -> None: ...

    async def seed_documents_once(self, documents: list[KnowledgeDocument]) -> None:
        """Atomic installation fixture seed; never resurrect removed/changed sources."""
        ...

    async def list_documents(self, domain_id: str) -> list[KnowledgeDocument]: ...


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
