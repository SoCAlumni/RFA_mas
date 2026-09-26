from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from rfa_mas.contracts import (
    AgentSpec,
    DraftBundle,
    EvaluationCase,
    EvidenceBundle,
    JudgeAssessment,
    KnowledgeDocument,
    ModelRequest,
    ModelResult,
    PolicyDecision,
    PolicyRequest,
    RetrievalRequest,
    ReviewDecision,
    RunResult,
    SimulationScenario,
    TaskRequest,
    TaskResult,
    ToolRequest,
    ToolResult,
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
    async def initialize(self) -> None: ...

    async def create_run(self, request: WorkRequest) -> None: ...

    async def transition_run(self, run_id: str, status: WorkStatus) -> None: ...

    async def save_result(self, result: RunResult) -> None: ...

    async def get_result(self, run_id: str) -> RunResult | None: ...

    async def save_checkpoint(self, run_id: str, node: str, metadata: dict[str, Any]) -> None: ...

    async def upsert_documents(self, documents: list[KnowledgeDocument]) -> None: ...

    async def list_documents(self, domain_id: str) -> list[KnowledgeDocument]: ...


class TracePort(Protocol):
    adapter_name: str
    simulated: bool

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
