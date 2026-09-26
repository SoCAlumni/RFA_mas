from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator

from rfa_mas.contracts.common import (
    AdapterInfo,
    Audience,
    ContractModel,
    DomainId,
    FeedbackCategory,
    PublicationStatus,
    ResultStatus,
    ReviewStatus,
    SimulationScenario,
    StructuredError,
    ToolEffect,
    WorkStatus,
    new_id,
    sha256_text,
)


class TrustedPrincipal(ContractModel):
    """Server-derived identity. Never build this from unverified request claims."""

    user_id: str
    authenticated: bool
    company_id: str | None = None
    business_units: frozenset[str] = Field(default_factory=frozenset)
    roles: frozenset[str] = Field(default_factory=frozenset)


class DraftTarget(ContractModel):
    audience: Audience
    channel: str = "preview"
    destination: str = "local-preview"


class WorkRequest(ContractModel):
    request_id: str = Field(default_factory=lambda: new_id("req"))
    trace_id: str = Field(default_factory=lambda: new_id("trace"))
    run_id: str = Field(default_factory=lambda: new_id("run"))
    agent_id: str = "assistant-supervisor"
    domain_id: DomainId | None = None
    idempotency_key: str | None = None
    query: str = Field(min_length=1, max_length=10_000)
    target: DraftTarget = Field(default_factory=lambda: DraftTarget(audience=Audience.OWNER))
    simulation_scenario: SimulationScenario = SimulationScenario.SUCCESS

    @model_validator(mode="after")
    def set_idempotency_key(self) -> WorkRequest:
        if self.idempotency_key is None:
            object.__setattr__(self, "idempotency_key", f"{self.request_id}:work")
        return self


class AgentSpec(ContractModel):
    agent_id: str
    domain_id: DomainId
    memory_namespace: str
    capabilities: tuple[str, ...]
    allowed_audiences: tuple[Audience, ...]
    instructions_ref: str
    max_steps: int = Field(ge=1, le=100)
    max_tool_calls: int = Field(ge=0, le=100)


class TaskRequest(ContractModel):
    request_id: str
    trace_id: str
    run_id: str
    agent_id: str
    domain_id: DomainId
    idempotency_key: str
    task_type: str
    payload: dict[str, Any]


class TaskResult(ContractModel):
    request_id: str
    trace_id: str
    run_id: str
    agent_id: str
    domain_id: DomainId
    status: ResultStatus
    output: dict[str, Any] = Field(default_factory=dict)
    error: StructuredError | None = None
    simulated: bool
    adapter: str


class SourceLocation(ContractModel):
    uri: str
    section: str | None = None
    page: int | None = Field(default=None, ge=1)
    line_start: int | None = Field(default=None, ge=1)


class KnowledgeDocument(ContractModel):
    source_id: str
    source_revision: str
    domain_id: DomainId
    title: str
    content: str
    location: SourceLocation
    audience: Audience
    classification: str
    policy_version: str
    required_memberships: tuple[str, ...] = Field(default_factory=tuple)
    owner_id: str | None = None
    business_unit: str | None = None
    company_id: str | None = None
    synthetic: bool = True
    privacy_canary: bool = False


class EvidenceItem(ContractModel):
    source_id: str
    source_revision: str
    location: SourceLocation
    audience: Audience
    excerpt: str
    content_hash: str
    policy_version: str


class EvidenceRef(ContractModel):
    source_id: str
    source_revision: str
    location: SourceLocation
    audience: Audience
    content_hash: str


class EvidenceBundle(ContractModel):
    request_id: str
    trace_id: str
    run_id: str
    agent_id: str
    domain_id: DomainId
    items: tuple[EvidenceItem, ...] = Field(default_factory=tuple)
    insufficient: bool = False
    policy_version: str
    simulated: bool
    adapter: str


class DraftBundle(ContractModel):
    request_id: str
    trace_id: str
    run_id: str
    agent_id: str
    domain_id: DomainId
    draft_id: str = Field(default_factory=lambda: new_id("draft"))
    version: int = Field(default=1, ge=1)
    content_hash: str
    target: DraftTarget
    audience: Audience
    policy_version: str
    allowed_evidence: tuple[EvidenceRef, ...]
    content: str
    simulated: bool
    adapter: str

    @model_validator(mode="after")
    def validate_binding_fields(self) -> DraftBundle:
        if self.audience != self.target.audience:
            raise ValueError("draft audience must match target audience")
        if self.content_hash != sha256_text(self.content):
            raise ValueError("draft content_hash must match content")
        return self


class ReviewDecision(ContractModel):
    request_id: str
    trace_id: str
    run_id: str
    agent_id: str
    domain_id: DomainId
    draft_id: str
    draft_version: int = Field(ge=1)
    content_hash: str
    target: DraftTarget
    decision: ReviewStatus
    publication_status: PublicationStatus = PublicationStatus.NOT_REQUESTED
    safe_reason: str
    simulated: bool
    adapter: str


class FeedbackEvent(ContractModel):
    request_id: str
    trace_id: str
    run_id: str
    agent_id: str
    domain_id: DomainId
    draft_id: str
    draft_version: int
    category: FeedbackCategory
    feedback: str
    official_policy_changed: bool = False


class ToolRequest(ContractModel):
    request_id: str
    trace_id: str
    run_id: str
    agent_id: str
    domain_id: DomainId
    idempotency_key: str
    tool_name: str
    effect: ToolEffect
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(ContractModel):
    request_id: str
    trace_id: str
    run_id: str
    agent_id: str
    domain_id: DomainId
    idempotency_key: str
    status: ResultStatus
    output: dict[str, Any] = Field(default_factory=dict)
    error: StructuredError | None = None
    simulated: bool
    adapter: str


class PolicyRequest(ContractModel):
    request_id: str
    trace_id: str
    run_id: str
    agent_id: str
    domain_id: DomainId
    action: str
    resource_audience: Audience
    resource_owner_id: str | None = None
    resource_business_unit: str | None = None
    resource_company_id: str | None = None
    target_audience: Audience | None = None
    tool_effect: ToolEffect | None = None
    principal: TrustedPrincipal


class PolicyDecision(ContractModel):
    request_id: str
    trace_id: str
    run_id: str
    agent_id: str
    domain_id: DomainId
    allowed: bool
    code: str
    safe_reason: str
    policy_version: str
    allowed_audiences: tuple[Audience, ...] = Field(default_factory=tuple)
    simulated: bool
    adapter: str


class RetrievalRequest(ContractModel):
    request_id: str
    trace_id: str
    run_id: str
    agent_id: str
    domain_id: DomainId
    query: str
    allowed_audiences: tuple[Audience, ...]
    principal: TrustedPrincipal
    limit: int = Field(default=5, ge=1, le=20)
    simulation_scenario: SimulationScenario = SimulationScenario.SUCCESS


class ModelRequest(ContractModel):
    request_id: str
    trace_id: str
    run_id: str
    agent_id: str
    domain_id: DomainId
    query: str
    evidence: EvidenceBundle
    target: DraftTarget
    simulation_scenario: SimulationScenario = SimulationScenario.SUCCESS


class ModelResult(ContractModel):
    content: str
    simulated: bool
    adapter: str


class RunResult(ContractModel):
    request_id: str
    trace_id: str
    run_id: str
    agent_id: str
    domain_id: DomainId | None = None
    status: WorkStatus
    draft: DraftBundle | None = None
    review: ReviewDecision | None = None
    publication_status: PublicationStatus = PublicationStatus.NOT_REQUESTED
    errors: tuple[StructuredError, ...] = Field(default_factory=tuple)
    stop_reason: str
    simulated: bool
    adapters: tuple[AdapterInfo, ...]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class EvaluationMaterialScope(ContractModel):
    domain_id: DomainId
    authenticated_principal: str
    memberships: tuple[str, ...]
    requested_audience: Audience
    allowed_source_ids: tuple[str, ...]


class EvaluationCase(ContractModel):
    case_id: str
    persona: str
    input: str
    material_scope: EvaluationMaterialScope
    expected_evidence: tuple[str, ...] = Field(default_factory=tuple)
    forbidden_information: tuple[str, ...] = Field(default_factory=tuple)
    expected_behavior: str


class JudgeDimensions(ContractModel):
    """Auxiliary quality dimensions; never authorization or privacy decisions."""

    evidence_faithfulness: float = Field(ge=0, le=1)
    question_resolution: float = Field(ge=0, le=1)
    task_candidate_usefulness: float = Field(ge=0, le=1)


class JudgeAssessment(ContractModel):
    """Non-authoritative assessment produced by an enabled Judge adapter."""

    kind: Literal["actual", "mock"]
    score: float = Field(ge=0, le=1)
    reason: str
    dimensions: JudgeDimensions
    simulated: bool
    adapter: str

    @model_validator(mode="after")
    def validate_kind_matches_simulation(self) -> JudgeAssessment:
        if (self.kind == "mock") != self.simulated:
            raise ValueError("mock judge assessments must be simulated; actual ones must not be")
        return self


class EvalResult(ContractModel):
    case_id: str
    run_id: str | None = None
    trace_id: str | None = None
    rule_checks: dict[str, bool]
    judge_kind: Literal["actual", "mock", "not_run"]
    judge_score: float | None = Field(default=None, ge=0, le=1)
    judge_reason: str | None = None
    judge_dimensions: JudgeDimensions | None = None
    judge_adapter: str | None = None
    executed: bool
    simulated: bool

    @model_validator(mode="after")
    def validate_judge_execution_state(self) -> EvalResult:
        if self.judge_kind == "not_run":
            if self.executed:
                raise ValueError("not_run evaluations cannot be marked executed")
            if self.simulated:
                raise ValueError("not_run evaluations are not simulated Judge executions")
            if any(
                value is not None
                for value in (self.judge_score, self.judge_dimensions, self.judge_adapter)
            ):
                raise ValueError("not_run evaluations cannot contain a judge assessment")
            return self
        if not self.executed:
            raise ValueError("actual and mock judge evaluations must be marked executed")
        if any(
            value is None
            for value in (
                self.judge_score,
                self.judge_reason,
                self.judge_dimensions,
                self.judge_adapter,
            )
        ):
            raise ValueError("executed evaluations require a complete judge assessment")
        if (self.judge_kind == "mock") != self.simulated:
            raise ValueError("judge_kind and simulated must agree")
        return self


class SecretStatus(ContractModel):
    name: str
    configured: bool
    required_for_selected_mode: bool


class AuthenticationConfig(ContractModel):
    api_key: SecretStr | None = Field(default=None, exclude=True)

    @field_validator("api_key", mode="before")
    @classmethod
    def empty_secret_is_none(cls, value: object) -> object:
        return None if value == "" else value
