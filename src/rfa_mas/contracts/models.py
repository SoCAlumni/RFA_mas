from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import (
    AwareDatetime,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from rfa_mas.contracts.common import (
    AdapterInfo,
    Audience,
    ContextLevel,
    ContractModel,
    Digest,
    DomainId,
    EvaluationStatus,
    ExecutionMode,
    ExtendedContractModel,
    FeedbackCategory,
    OpaqueId,
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


# Repository-local 1.1 extensions. These schemas are not authentication or
# evidence that another service implements the protocol. Legacy DTOs stay 1.0.
class KnowledgeDocumentV11(KnowledgeDocument):
    """Exact stored text; legacy 1.0 validation/whitespace behavior is unchanged."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    schema_version: Literal["1.1"] = "1.1"
    project_id: OpaqueId | None = None


class KnowledgeAcl(ExtendedContractModel):
    audience: Audience = Audience.PRIVATE
    company_id: OpaqueId | None = None
    memberships: tuple[OpaqueId, ...] = ()
    project_id: OpaqueId | None = None

    @model_validator(mode="after")
    def consistent_scope(self) -> KnowledgeAcl:
        if self.audience == Audience.BUSINESS_UNIT:
            if not self.company_id or not self.memberships:
                raise ValueError("business-unit sharing requires company and membership")
        elif self.memberships:
            raise ValueError("membership belongs only to business-unit sharing")
        if self.audience == Audience.COMPANY and not self.company_id:
            raise ValueError("company sharing requires company")
        if self.project_id and not self.company_id:
            raise ValueError("project restriction requires company")
        if (
            self.audience not in {Audience.COMPANY, Audience.BUSINESS_UNIT}
            and self.company_id and not self.project_id
        ):
            raise ValueError("company metadata is not part of this sharing scope")
        if len(set(self.memberships)) != len(self.memberships):
            raise ValueError("duplicate membership")
        return self


class KnowledgeProvenance(ExtendedContractModel):
    provider: Literal["note", "github_issue", "confluence"]
    namespace: str = Field(min_length=1, max_length=200)
    external_id: str = Field(min_length=1, max_length=200)


class KnowledgeWrite(ExtendedContractModel):
    domain_id: DomainId
    provenance: KnowledgeProvenance
    provider_revision: str = Field(min_length=1, max_length=200)
    expected_revision: OpaqueId | None = None
    title: str = Field(min_length=1, max_length=1000)
    content: str = Field(max_length=200_000)
    acl: KnowledgeAcl = Field(default_factory=KnowledgeAcl)
    source_modified_at: AwareDatetime | None = None
    synthetic: bool = False


class KnowledgeDelete(ExtendedContractModel):
    expected_revision: OpaqueId
    mutation_id: OpaqueId


class KnowledgeRevision(ExtendedContractModel):
    document: KnowledgeDocumentV11
    provenance: KnowledgeProvenance
    provider_revision: str
    revision_number: int = Field(ge=1)
    acl_revision: OpaqueId
    deleted: bool
    created_at: AwareDatetime
    source_modified_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def same_acl_revision(self) -> KnowledgeRevision:
        if self.acl_revision != self.document.source_revision:
            raise ValueError("ACL and content revision must be bound")
        return self


class KnowledgeExport(ExtendedContractModel):
    provider: Literal["github_issue", "confluence"]
    namespace: str = Field(min_length=1, max_length=200)
    domain_id: DomainId
    rows: tuple[dict[str, Any], ...] = Field(min_length=1, max_length=100)
    synthetic: bool = False


class KnowledgeImportRow(ExtendedContractModel):
    row: int = Field(ge=0)
    status: Literal["accepted", "rejected"]
    source_id: OpaqueId | None = None
    source_revision: OpaqueId | None = None
    error_code: (
        Literal["invalid_input", "policy_denied", "idempotency_conflict", "not_found"] | None
    ) = None

    @model_validator(mode="after")
    def consistent_receipt(self) -> KnowledgeImportRow:
        if self.status == "accepted":
            if not self.source_id or not self.source_revision or self.error_code is not None:
                raise ValueError("accepted receipt requires exact source/revision only")
        elif (
            self.error_code is None
            or self.source_id is not None
            or self.source_revision is not None
        ):
            raise ValueError("rejected receipt requires a safe error only")
        return self


class KnowledgeImportResult(ExtendedContractModel):
    rows: tuple[KnowledgeImportRow, ...]


class ExecutionContext(ExtendedContractModel):
    request_id: OpaqueId
    trace_id: OpaqueId
    run_id: OpaqueId
    agent_id: OpaqueId
    session_id: OpaqueId | None = None
    domain_id: DomainId | None = None
    task_id: OpaqueId | None = None
    team_id: OpaqueId | None = None

    @model_validator(mode="after")
    def coherent_links(self) -> ExecutionContext:
        if self.team_id is not None and self.task_id is None:
            raise ValueError("team requires task")
        if self.task_id is not None and self.domain_id is None:
            raise ValueError("task requires domain")
        return self


class SessionRecord(ExtendedContractModel):
    session_id: OpaqueId
    thread_id: OpaqueId
    owner_id: OpaqueId
    task_ids: tuple[OpaqueId, ...] = ()
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def utc_times(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class SessionCreate(ExtendedContractModel):
    """Empty request: ownership and thread identifiers are allocated by the server."""


class ResumeRequest(ExtendedContractModel):
    """Wake a waiting run to QUERY its review source; never grants approval."""

    event_id: OpaqueId
    action: Literal["refresh_review"] = "refresh_review"


class SessionMessage(ExtendedContractModel):
    message_id: OpaqueId
    session_id: OpaqueId
    run_id: str
    role: Literal["user", "assistant"]
    content: str
    created_at: AwareDatetime


class RunRecord(ExtendedContractModel):
    """Owner-authorized metadata, including runs without a final result yet."""

    run_id: str
    request_id: str
    trace_id: str
    session_id: OpaqueId
    thread_id: OpaqueId
    task_id: OpaqueId | None = None
    owner_id: OpaqueId
    domain_id: DomainId | None = None
    status: WorkStatus
    result: RunResult | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class SessionDetail(SessionRecord):
    messages: tuple[SessionMessage, ...] = ()
    runs: tuple[RunRecord, ...] = ()


class PersistentTask(ExtendedContractModel):
    task_id: OpaqueId
    domain_id: DomainId
    owner_id: OpaqueId
    goal: str = Field(min_length=1, max_length=10000)
    status: Literal["active", "paused", "completed", "cancelled"] = "active"
    team_id: OpaqueId | None = None
    revision: int = Field(default=1, ge=1)


class TeamBudget(ExtendedContractModel):
    max_steps: int = Field(default=30, ge=1, le=1000)
    max_tool_calls: int = Field(default=10, ge=0, le=1000)
    max_tokens: int = Field(default=16000, ge=1)
    timeout_seconds: float = Field(default=180, gt=0, le=3600)
    concurrency: int = Field(default=2, ge=1, le=16)


class TeamTemplate(ExtendedContractModel):
    template_id: OpaqueId
    version: OpaqueId
    pattern: Literal["benchmark", "research"]
    approved: Literal[True]
    required_capabilities: tuple[OpaqueId, ...]
    runtime_kind: Literal["local", "openshell"]
    budget: TeamBudget


class TeamMember(ExtendedContractModel):
    role: Literal[
        "supervisor",
        "paper_scout",
        "experiment_runner",
        "result_analyst",
        "source_scout",
        "evidence_reviewer",
    ]
    spec: AgentSpec
    source_ids: tuple[OpaqueId, ...] = ()
    tool_names: tuple[OpaqueId, ...] = ()
    # Authenticated Runtime/Policy adapters must enforce these requested bounds.
    # A caller-controlled capability list never grants authority.


class TeamSpec(ExtendedContractModel):
    task_id: OpaqueId
    team_id: OpaqueId
    domain_id: DomainId
    owner_id: OpaqueId
    template: TeamTemplate
    members: tuple[TeamMember, ...]
    communication: Literal["supervisor_only"] = "supervisor_only"
    # Optional for the original provisional 1.1 payload; Factory requires both.
    definition_digest: Digest | None = None
    execution_budget: TeamBudget | None = None

    @model_validator(mode="after")
    def bind_members(self) -> TeamSpec:
        required = {
            "benchmark": {"supervisor", "paper_scout", "experiment_runner", "result_analyst"},
            "research": {"supervisor", "source_scout", "evidence_reviewer"},
        }[self.template.pattern]
        if {member.role for member in self.members} != required or len(self.members) != len(
            required
        ):
            raise ValueError("team roles must match approved pattern exactly")
        ids = [member.spec.agent_id for member in self.members]
        memories = [member.spec.memory_namespace for member in self.members]
        if len(set(ids)) != len(ids) or len(set(memories)) != len(memories):
            raise ValueError("agent IDs and memory namespaces must be unique")
        if any(member.spec.domain_id != self.domain_id for member in self.members):
            raise ValueError("team and member domains must match")
        if self.execution_budget is not None and any(
            getattr(self.execution_budget, name) > getattr(self.template.budget, name)
            for name in (
                "max_steps",
                "max_tool_calls",
                "max_tokens",
                "timeout_seconds",
                "concurrency",
            )
        ):
            raise ValueError("execution budget exceeds approved template")
        return self


class MemberLifecycle(ExtendedContractModel):
    agent_id: OpaqueId
    prepare: Literal["not_started", "prepared", "failed", "unknown"] = "not_started"
    cleanup: Literal["not_requested", "cleaned", "failed", "unknown"] = "not_requested"


class TeamInstance(ExtendedContractModel):
    spec: TeamSpec
    state: Literal[
        "provisioning",
        "ready",
        "running",
        "failed",
        "cancelled",
        "cleaned",
        "unknown",
        "cleanup_pending",
    ]
    runtime_ref: OpaqueId | None = None
    sandbox_id: OpaqueId | None = None
    mode: ExecutionMode
    failed_agent_ids: tuple[OpaqueId, ...] = ()
    member_states: tuple[MemberLifecycle, ...] = ()

    @model_validator(mode="after")
    def sandbox_requires_real_runtime(self) -> TeamInstance:
        if self.sandbox_id is not None and (
            self.mode != ExecutionMode.REAL or self.spec.template.runtime_kind != "openshell"
        ):
            raise ValueError("local/mock runtime is not a sandbox")
        expected = {member.spec.agent_id for member in self.spec.members}
        actual = [member.agent_id for member in self.member_states]
        if self.member_states and (set(actual) != expected or len(actual) != len(expected)):
            raise ValueError("lifecycle must bind each member exactly once")
        if not set(self.failed_agent_ids) <= expected:
            raise ValueError("failed member is not in team")
        return self


class TeamLifecycle(ExtendedContractModel):
    """Core durable lifecycle receipt, not runtime authority or a trace span."""

    task: PersistentTask
    team: TeamInstance
    generation: int = Field(ge=1)
    operation: Literal["prepare", "cleanup"]
    operation_key: OpaqueId
    phase: Literal["pending", "finished"]
    reason: Literal[
        "reserved",
        "ready",
        "partial_failure",
        "outcome_unknown",
        "invalid_contract",
        "cleanup_requested",
        "cleaned",
        "cleanup_failed",
    ]
    trace_collection: Literal["uncollected"] = "uncollected"

    @model_validator(mode="after")
    def bind_task(self) -> TeamLifecycle:
        if (
            self.task.task_id != self.team.spec.task_id
            or self.task.team_id != self.team.spec.team_id
            or self.task.owner_id != self.team.spec.owner_id
            or self.task.domain_id != self.team.spec.domain_id
        ):
            raise ValueError("task and team binding mismatch")
        return self


TeamRole = Literal[
    "supervisor",
    "paper_scout",
    "experiment_runner",
    "result_analyst",
    "source_scout",
    "evidence_reviewer",
]


class TeamExecutionRequest(ExtendedContractModel):
    """Explicit team-execution intent (P0-020). No capability, identity or grant claims.

    Template choice, role capabilities and budgets are decided server-side by the
    TeamSelector/TeamFactory from the authenticated principal.
    """

    goal: str = Field(min_length=1, max_length=2000)
    outputs: tuple[OpaqueId, ...] = Field(default=("evidence_summary",), min_length=1, max_length=8)
    requested_pattern: Literal["benchmark", "research"] | None = None


class RoleOutcome(ExtendedContractModel):
    """One role execution receipt collected by the team Supervisor."""

    role: TeamRole
    agent_id: OpaqueId
    execution_key: OpaqueId
    status: Literal[
        "succeeded", "failed", "denied", "timed_out", "cancelled", "budget_exceeded", "unknown"
    ]
    output: dict[str, Any] = Field(default_factory=dict)
    evidence: tuple[EvidenceRef, ...] = ()
    tool_calls: int = Field(default=0, ge=0)
    steps: int = Field(default=0, ge=0)
    duration_ms: float | None = Field(default=None, ge=0)
    # Provider-reported usage only; unknown usage stays null, never 0.
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    simulated: bool
    error_code: OpaqueId | None = None


class TeamBudgetUsage(ExtendedContractModel):
    steps: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    elapsed_ms: float = Field(ge=0)
    tokens: int | None = Field(default=None, ge=0)
    max_concurrency_observed: int = Field(ge=0)
    limits: TeamBudget


class TeamRunResult(ExtendedContractModel):
    """Supervisor-collected team result for one product Run (not approval authority)."""

    run_id: str
    task_id: OpaqueId
    team_id: OpaqueId
    pattern: Literal["benchmark", "research"]
    status: Literal["completed", "partial", "failed", "cancelled"]
    stop_reason: OpaqueId | None = None
    roles: tuple[RoleOutcome, ...] = ()
    summary: str = Field(default="", max_length=20000)
    findings: dict[str, Any] = Field(default_factory=dict)
    usage: TeamBudgetUsage
    simulated: bool

    @model_validator(mode="after")
    def completed_requires_success(self) -> TeamRunResult:
        if self.status == "completed" and any(r.status != "succeeded" for r in self.roles):
            raise ValueError("completed team result requires every role to succeed")
        return self


class ScheduleSpec(ExtendedContractModel):
    schedule_id: OpaqueId
    owner_id: OpaqueId
    job_type: Literal["briefing", "organize", "candidate_scan"]
    domain_id: DomainId
    task_id: OpaqueId | None = None
    cron: str = Field(min_length=1, max_length=100)
    timezone: str = "Asia/Seoul"
    enabled: bool = True
    misfire_policy: Literal["latest_once", "hold"] = "latest_once"
    next_run_at: AwareDatetime | None = None


class DirectWorkRequest(WorkRequest):
    schema_version: Literal["1.1"] = "1.1"
    ingress: Literal["direct"] = "direct"
    session_id: OpaqueId | None = None
    task_id: OpaqueId | None = None
    # Explicit team execution (P0-020). Absent means the existing single-domain path.
    team: TeamExecutionRequest | None = None


class ChannelWorkRequest(DirectWorkRequest):
    ingress: Literal["internal", "public"]
    channel_event_id: OpaqueId

    @model_validator(mode="after")
    def supervisor_only(self) -> ChannelWorkRequest:
        if self.agent_id != "assistant-supervisor":
            raise ValueError("channel requests must enter through assistant-supervisor")
        if self.ingress == "public" and self.target.audience != Audience.PUBLIC:
            raise ValueError("public ingress requires public target")
        return self


# "simulated" marks synthetic experiment numbers (P1-004A); never a measured fact.
EpistemicState = Literal["cited", "inferred", "simulated", "tentative", "conflicting"]
DerivedKind = Literal["summary", "todo", "issue", "decision", "link"]


class SourceRevisionRef(EvidenceRef):
    schema_version: Literal["1.1"] = "1.1"
    acl_revision: OpaqueId
    policy_version: OpaqueId


class PolicyDecisionV11(PolicyDecision):
    schema_version: Literal["1.1"] = "1.1"
    decision_id: OpaqueId
    decision: Literal["allow", "deny", "review"]
    action: Literal["read", "share", "egress", "tool", "publish"]
    subject_id: OpaqueId
    resource_id: OpaqueId
    recipient: OpaqueId
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    source_refs: tuple[SourceRevisionRef, ...] = ()

    @model_validator(mode="after")
    def valid_decision(self) -> PolicyDecisionV11:
        if self.allowed != (self.decision == "allow"):
            raise ValueError("review/deny is not allow")
        if self.expires_at <= self.issued_at:
            raise ValueError("policy validity must have a positive duration")
        return self


class PolicyBindings(ExtendedContractModel):
    read_decision_id: OpaqueId
    share_decision_id: OpaqueId | None = None
    egress_decision_id: OpaqueId | None = None


class ContextRequest(RetrievalRequest):
    schema_version: Literal["1.1"] = "1.1"
    level: ContextLevel = ContextLevel.L0
    goal: str = Field(min_length=1, max_length=10000)
    role: OpaqueId
    target: DraftTarget
    endpoint_id: OpaqueId
    selected_sources: tuple[SourceRevisionRef, ...] = ()
    max_characters: int = Field(default=12000, ge=1)
    policies: PolicyBindings | None = None

    @model_validator(mode="after")
    def explicit_fulltext_selection(self) -> ContextRequest:
        if self.level == ContextLevel.L2 and not self.selected_sources:
            raise ValueError("L2 requires explicit sources and current revisions")
        return self


class ContextItem(EvidenceItem):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    schema_version: Literal["1.1"] = "1.1"
    level: ContextLevel
    parents: tuple[SourceRevisionRef, ...] = Field(min_length=1)
    policies: PolicyBindings
    epistemic_state: EpistemicState = "cited"
    state: Literal["current", "stale", "restricted"] = "current"


class ContextBundle(EvidenceBundle):
    schema_version: Literal["1.1"] = "1.1"
    items: tuple[ContextItem, ...] = ()
    loaded_characters: int = Field(ge=0)
    measured_tokens: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def exact_loading_measurement(self) -> ContextBundle:
        if self.loaded_characters != sum(len(item.excerpt) for item in self.items):
            raise ValueError("loaded_characters must be the measured excerpt length")
        return self


class SourceMetadata(ExtendedContractModel):
    """Only returned after current ACL and every parent have been authorized."""

    reference: SourceRevisionRef
    title: str
    character_count: int = Field(ge=0)
    epistemic_state: EpistemicState = "cited"
    parents: tuple[SourceRevisionRef, ...] = ()
    owner_id: str | None = None
    company_id: str | None = None
    required_memberships: tuple[str, ...] = ()
    project_id: str | None = None


class SourceRead(ExtendedContractModel):
    metadata: SourceMetadata
    content: str

class DerivedItemProposal(ExtendedContractModel):
    """Worker/extractor proposal. Not knowledge until the Supervisor gate accepts it."""

    kind: DerivedKind
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=5000)
    epistemic_state: EpistemicState
    parents: tuple[SourceRevisionRef, ...] = Field(min_length=1, max_length=32)
    conditions: tuple[str, ...] = ()
    uncertainty: str | None = Field(default=None, max_length=1000)
    origin_ref: OpaqueId


class AccumulatedItem(ExtendedContractModel):
    kind: DerivedKind
    title: str
    review_state: Literal["accepted", "rejected"]
    epistemic_state: EpistemicState
    reason: OpaqueId
    source_id: OpaqueId | None = None
    source_revision: OpaqueId | None = None
    parents: tuple[SourceRevisionRef, ...] = ()


class AccumulationReport(ExtendedContractModel):
    """Supervisor review receipt for a batch of derived proposals."""

    items: tuple[AccumulatedItem, ...] = ()

class TodoCandidate(ExtendedContractModel):
    """Discovered work candidate (P1-004B). Discovery never creates a Task or team."""

    candidate_id: OpaqueId
    domain_id: DomainId
    owner_id: OpaqueId
    kind: Literal["todo", "issue", "follow_up"]
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=5000)
    fingerprint: Digest
    parents: tuple[SourceRevisionRef, ...] = Field(min_length=1, max_length=32)
    epistemic_state: EpistemicState
    state: Literal["proposed", "accepted", "deferred", "rejected", "superseded"] = "proposed"
    due_date: str | None = None  # Only when explicitly stated in a parent; never invented.
    blocker: bool = False
    resurface_on_new_evidence: bool = True
    decided_reason: str | None = Field(default=None, max_length=500)
    history: tuple[str, ...] = ()
    created_at: AwareDatetime
    updated_at: AwareDatetime


class CandidateDecision(ExtendedContractModel):
    decision: Literal["accept", "defer", "reject"]
    reason: str = Field(default="", max_length=500)
    resurface_on_new_evidence: bool = True




class ExperimentEvidence(ExtendedContractModel):
    status: Literal["mock", "not_run", "measured"]
    metrics: dict[OpaqueId, float] = Field(default_factory=dict)
    units: dict[OpaqueId, OpaqueId] = Field(default_factory=dict)
    evidence_ref: OpaqueId | None = None
    conditions_ref: OpaqueId | None = None

    @model_validator(mode="after")
    def no_fabricated_measurement(self) -> ExperimentEvidence:
        if self.status == "not_run" and self.metrics:
            raise ValueError("not_run cannot contain measured metrics")
        if set(self.metrics) != set(self.units):
            raise ValueError("metrics require explicit matching units")
        if self.status == "measured" and not (self.evidence_ref and self.conditions_ref):
            raise ValueError("measured experiments require evidence and conditions")
        return self


class AttachmentRef(ExtendedContractModel):
    attachment_id: OpaqueId
    content_hash: Digest


class DraftBinding(ExtendedContractModel):
    draft_id: OpaqueId
    version: int = Field(ge=1)
    content_hash: Digest
    payload_hash: Digest
    target: DraftTarget
    policy_version: OpaqueId
    sources: tuple[SourceRevisionRef, ...]


class DraftBundleV11(DraftBundle):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    schema_version: Literal["1.1"] = "1.1"
    attachments: tuple[AttachmentRef, ...] = ()
    sources: tuple[SourceRevisionRef, ...] = ()
    policy_decision_id: OpaqueId
    payload_hash: Digest

    def calculated_payload_hash(self) -> str:
        # Hash binds exact content, attachment digests, target and policy/source
        # revisions. It neither anonymizes content nor authenticates an approver.
        data = self.model_dump(
            mode="json",
            include={
                "draft_id",
                "version",
                "content",
                "content_hash",
                "attachments",
                "target",
                "policy_version",
                "policy_decision_id",
                "sources",
            },
        )
        return sha256_text(
            json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        )

    @model_validator(mode="after")
    def exact_payload_binding(self) -> DraftBundleV11:
        if self.payload_hash != self.calculated_payload_hash():
            raise ValueError("payload_hash must bind exact draft, attachments, target and policies")
        if {ref.source_id for ref in self.allowed_evidence} != {
            ref.source_id for ref in self.sources
        }:
            raise ValueError("all draft evidence requires a current source/ACL reference")
        for evidence in self.allowed_evidence:
            if not any(
                all(
                    getattr(evidence, k) == getattr(source, k)
                    for k in (
                        "source_id",
                        "source_revision",
                        "location",
                        "audience",
                        "content_hash",
                    )
                )
                for source in self.sources
            ):
                raise ValueError("evidence and source revision binding mismatch")
        return self

    def binding(self) -> DraftBinding:
        return DraftBinding(
            **self.model_dump(
                include={
                    "draft_id",
                    "version",
                    "content_hash",
                    "payload_hash",
                    "target",
                    "policy_version",
                    "sources",
                }
            )
        )


class ApprovalReference(ExtendedContractModel):
    """Mirror only. Transport authentication and server-owned lookup are mandatory."""

    approval_id: OpaqueId
    approver_id: OpaqueId
    binding: DraftBinding
    decision: ReviewStatus
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    mode: ExecutionMode
    authority: Literal["response_service", "reference_mock"]

    @model_validator(mode="after")
    def consistent_authority(self) -> ApprovalReference:
        if (self.authority == "reference_mock") != (self.mode == ExecutionMode.MOCK):
            raise ValueError("mock authority must be explicitly mock")
        if self.expires_at <= self.issued_at:
            raise ValueError("approval must have valid expiry")
        return self

    def matches(self, draft: DraftBundleV11, at: datetime) -> bool:
        if at.tzinfo is None:
            raise ValueError("approval evaluation time must be timezone aware")
        # Assignment/model_copy can bypass Pydantic construction validation.
        # Reparse dictionaries (not existing instances) at this safety boundary.
        try:
            current = DraftBundleV11.model_validate(draft.model_dump())
            approval = ApprovalReference.model_validate(self.model_dump())
        except (ValidationError, ValueError, TypeError):
            return False
        return (
            approval.decision == ReviewStatus.APPROVED
            and approval.issued_at <= at < approval.expires_at
            and approval.binding == current.binding()
        )


class PublicationReceipt(ExtendedContractModel):
    publication_id: OpaqueId
    run_id: OpaqueId
    idempotency_key: OpaqueId
    binding: DraftBinding
    approval_id: OpaqueId
    status: PublicationStatus
    external_result_ref: OpaqueId | None = None
    mode: ExecutionMode
    next_action: Literal["none", "query", "review"]

    @model_validator(mode="after")
    def unknown_requires_query(self) -> PublicationReceipt:
        if self.status == PublicationStatus.OUTCOME_UNKNOWN and self.next_action != "query":
            raise ValueError("unknown publication requires query, never automatic republish")
        if self.status == PublicationStatus.SUCCEEDED and self.external_result_ref is None:
            raise ValueError("successful publication requires result reference")
        return self


class ToolInvocation(ToolRequest):
    schema_version: Literal["1.1"] = "1.1"
    principal: TrustedPrincipal
    capabilities: tuple[OpaqueId, ...]
    task_id: OpaqueId | None = None
    policy_decision_id: OpaqueId
    approval_id: OpaqueId | None = None


class VersionReferences(ExtendedContractModel):
    code: OpaqueId
    contract: Literal["1.1"] = "1.1"
    policy: OpaqueId
    dataset: OpaqueId | None = None
    evaluator: OpaqueId | None = None
    model: OpaqueId | None = None
    prompt: OpaqueId | None = None
    template: OpaqueId | None = None
    sources: tuple[OpaqueId, ...] = ()


class TraceEvent(ExtendedContractModel):
    """Allowlist only. IDs must be minted/mapped by trusted code, never raw inputs."""

    execution: ExecutionContext
    event: Literal[
        "request",
        "retrieval",
        "policy",
        "model",
        "tool",
        "draft",
        "approval",
        "publish",
        "runtime",
        "evaluation",
        "stop",
    ]
    status: Literal[
        "started", "succeeded", "failed", "denied", "waiting", "cancelled", "outcome_unknown"
    ]
    mode: ExecutionMode
    versions: VersionReferences
    timestamp: AwareDatetime
    duration_ms: float | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    call_count: int = Field(default=0, ge=0)
    reason_code: (
        Literal[
            "ok",
            "denied",
            "insufficient",
            "timeout",
            "budget",
            "cancelled",
            "provider_error",
            "invalid_contract",
        ]
        | None
    ) = None
    policy_decision_id: OpaqueId | None = None
    sandbox_id: OpaqueId | None = None
    draft_id: OpaqueId | None = None
    approval_id: OpaqueId | None = None
    publication_id: OpaqueId | None = None
    evidence_ref: OpaqueId | None = None

    @model_validator(mode="after")
    def stage_links(self) -> TraceEvent:
        if self.approval_id is not None and self.draft_id is None:
            raise ValueError("approval trace requires draft reference")
        if self.publication_id is not None and self.approval_id is None:
            raise ValueError("publication trace requires approval reference")
        if self.sandbox_id is not None and self.mode != ExecutionMode.REAL:
            raise ValueError("mock/local trace must not claim sandbox execution")
        return self


class ObservationRecord(ExtendedContractModel):
    """Trusted boundary observation, not approval or authorization evidence."""

    observation_id: OpaqueId
    sequence: int = Field(ge=1)
    origin: Literal["service", "port", "test_sink"]
    provider_ref: OpaqueId
    provider_kind: Literal["builtin", "reference_http", "test"] = "builtin"
    transport: Literal["returned", "raised"] | None = None
    event: TraceEvent


class ObservationCoverage(ExtendedContractModel):
    boundary: Literal[
        "request",
        "retrieval",
        "policy",
        "model",
        "runtime",
        "approval",
        "tool",
        "publish",
        "internal_nodes",
        "test_sink",
    ]
    state: Literal["collected", "uncollected", "incomplete"]
    calls: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def actual_count_only(self) -> ObservationCoverage:
        if (self.state != "collected") != (self.calls is None):
            raise ValueError("uncollected counts are unknown, not zero")
        return self


class ObservationLedger(ExtendedContractModel):
    execution: ExecutionContext
    observations: tuple[ObservationRecord, ...] = ()
    coverage: tuple[ObservationCoverage, ...]


class EvaluationCaseV11(EvaluationCase):
    schema_version: Literal["1.1"] = "1.1"
    scenario_id: OpaqueId
    identity_fixture_id: OpaqueId
    fixture_ref: OpaqueId
    dataset_version: OpaqueId
    seed: int
    synthetic: Literal[True] = True
    expected_observations: tuple[OpaqueId, ...] = Field(min_length=1)


class EvalResultV11(EvalResult):
    schema_version: Literal["1.1"] = "1.1"
    rule_status: EvaluationStatus
    judge_status: EvaluationStatus
    evaluator: Literal["rules", "judge", "combined"]
    versions: VersionReferences
    evidence_refs: tuple[OpaqueId, ...] = ()
    mode: ExecutionMode

    @model_validator(mode="after")
    def independent_gates(self) -> EvalResultV11:
        if self.rule_status == EvaluationStatus.PASS and (
            not self.rule_checks or not all(self.rule_checks.values())
        ):
            raise ValueError("rule pass requires all observed checks to pass")
        if self.rule_status == EvaluationStatus.NOT_RUN and self.rule_checks:
            raise ValueError("not_run rules cannot contain observations")
        if (
            self.judge_status in {EvaluationStatus.PASS, EvaluationStatus.FAIL}
            and self.judge_kind == "not_run"
        ):
            raise ValueError("judge pass/fail requires an executed assessment")
        if (
            self.judge_status
            in {EvaluationStatus.ERROR, EvaluationStatus.NOT_RUN, EvaluationStatus.UNKNOWN}
            and self.judge_kind != "not_run"
        ):
            raise ValueError("unavailable judge cannot provide a scored assessment")
        if (
            self.rule_status in {EvaluationStatus.PASS, EvaluationStatus.FAIL}
            and not self.evidence_refs
        ):
            raise ValueError("rule outcome requires observation evidence")
        return self
