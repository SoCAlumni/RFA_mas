"""`/v1/inbox` contract for the 결재 인박스 UI PoC (A · 메일형 3단 + 관리자 화면).

Every field maps to something visible in the UI PoC (2026-09-27 artifact "결재 인박스 UI 시안"):
left rail (inboxes, counts, current approver), request list (avatar/title/subtitle/status/
time), request detail (breadcrumb, original read-only view, decision card, alerts), the
blocked-attempt variant, the Slack and research-terminal variants, notifications, activity,
rules ("비슷한 요청에 이 결정을 규칙으로 쓰기") and the admin agent/sandbox screens.

Ownership of the data behind each route differs (`x-rfa-authority` on every operation):
- `rfa_module.review`  — 승희 review service is the approval source of truth.
- `runtime.sandbox`    — 다영 NemoClaw/OpenShell runtime owns sandboxes, agents, policies.
- `core`               — this repository (task/research decisions, rules, notes).
- `reference`          — served from PoC fixtures only (`python -m rfa_mas.reference.inbox`).

This module is intentionally outside `rfa_mas.contracts` (frozen shared DTOs): the UI
contract is additive and may change without touching the RFA 1.0/1.1 baselines.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

INBOX_CONTRACT_VERSION = "0.1.0"

Clearance = Literal["public", "company", "division", "team"]
"""공개 → 회사 내 → 사업부 내 → 업무 내부. Higher clearance = fewer readers."""
ChannelKind = Literal["github_issue", "slack_dm", "email", "terminal"]
RequestStatus = Literal[
    "needs_approval",  # 결재 필요
    "blocked",  # 차단됨 (sandbox stopped the agent; human decides how to handle)
    "auto_replied",  # 자동응답 (a saved rule answered without approval)
    "decided",  # 결재 완료 (responded / direction chosen)
    "declined",  # 결재 완료 · 응답하지 않기로 결정
    "held",  # 보류
    "in_progress",  # agent still working (RFA_module opened..scanned)
    "needs_human",  # RFA_module needs_human: automation gave up
]
DecisionKind = Literal[
    "disclosure_scope",  # 어디까지 공개할까요? (public channel)
    "share_scope",  # 어디까지 공유할까요? (internal channel)
    "research_direction",  # 어느 방향으로 진행할까요? (own research agent)
    "blocked_handling",  # 이 요청을 어떻게 할까요? (after sandbox blocks)
    "review_body",  # RFA_module reviewed draft: 게시본/원본 choice
]
DecisionAction = Literal[
    "respond",  # 이 범위로 응답 / 이 범위로 답장 / 이 방향으로 진행 (requires option_id)
    "decline",  # 응답하지 않기 / 답장하지 않기
    "hold",  # 보류
    "answer_question_only",  # 질문에만 답하기 (blocked variant)
    "block_author",  # 작성자 요청 받지 않기 (blocked variant)
]
Id = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/#-]*$")]


class InboxModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


# ---------------------------------------------------------------- left rail / identity
class InboxUser(InboxModel):
    """Bottom-left status: '이다영 · 샌드박스 4개 실행 중'."""

    user_id: Id
    display_name: str
    role: Literal["approver", "admin"]
    sandboxes_running: int = Field(ge=0)
    sandboxes_total: int = Field(ge=0)


class InboxSummary(InboxModel):
    """One row under '인박스': GitHub 이슈 · public-desk · 공개 등급 · 2."""

    inbox_id: Id
    name: str
    channel: ChannelKind
    agent_id: Id
    clearance: Clearance
    sandbox_id: Id
    pending_count: int = Field(ge=0)


class InboxCounts(InboxModel):
    """Badges: 결재함 4 · 알림함 4 · 모든 인박스 4."""

    needs_approval: int = Field(ge=0)
    notifications_unread: int = Field(ge=0)
    all_inboxes: int = Field(ge=0)


# ---------------------------------------------------------------- request list
class Requester(InboxModel):
    requester_id: Id
    display_name: str
    initials: str = Field(min_length=1, max_length=3)
    kind: Literal["external", "internal", "agent", "customer"]


class RequestSummary(InboxModel):
    """Request list row. `subtitle` is the second line ('결재 12 · 어디까지 공개할까요?')."""

    request_id: Id
    inbox_id: Id
    requester: Requester
    title: str
    subtitle: str
    status: RequestStatus
    approval_id: Id | None = None
    received_at: datetime
    is_new: bool = False


class RequestPage(InboxModel):
    items: list[RequestSummary]
    next_cursor: str | None = None
    counts: InboxCounts


# ---------------------------------------------------------------- request detail
class SourceRef(InboxModel):
    """Breadcrumb + '원본 화면' header: GitHub 이슈 / team/rfa-test#34 · 읽기 전용 · 원본 열기."""

    kind: ChannelKind
    label: str
    url: HttpUrl | None = None
    read_only: bool = True


class FlaggedSpan(InboxModel):
    """Prompt-injection highlight inside an original message."""

    start: int = Field(ge=0)
    end: int = Field(ge=0)
    reason: str

    @model_validator(mode="after")
    def ordered(self) -> FlaggedSpan:
        if self.end < self.start:
            raise ValueError("span end must not precede start")
        return self


class OriginalMessage(InboxModel):
    author: str
    at: datetime
    body: str
    flagged_spans: list[FlaggedSpan] = Field(default_factory=list)


class TerminalLine(InboxModel):
    """Research variant: 08:09 · 가설 1 · 캘리브레이션 데이터 교체."""

    at: datetime
    kind: Literal["command", "reading", "hypothesis", "evidence", "approval", "waiting"]
    label: str
    text: str


class OriginalView(InboxModel):
    """Read-only embed of where the request came from. Exactly one representation is used."""

    source: SourceRef
    header: str | None = None
    messages: list[OriginalMessage] = Field(default_factory=list)
    terminal: list[TerminalLine] = Field(default_factory=list)
    reply_placeholder: str | None = None
    injection_warning: str | None = None


class DecisionOption(InboxModel):
    """One radio card: label · 추천 · description · badge ('내부 지표', '기밀 문서')."""

    option_id: Id
    label: str
    description: str
    recommended: bool = False
    badge: str | None = None
    disclosure_level: int | None = Field(default=None, ge=0, le=10)


class BlockedAttempt(InboxModel):
    """'public 샌드박스가 막은 시도 3건' rows."""

    at: datetime
    action: str
    reason: str
    kind: Literal["review_approve", "knowledge_path", "network", "tool", "other"] = "other"
    activity_id: Id | None = None


class DecisionCard(InboxModel):
    """The approval question the agent asks the human."""

    approval_id: Id
    asked_by: Id
    kind: DecisionKind
    question: str
    note: str | None = None
    always_excluded: list[str] = Field(default_factory=list)
    options: list[DecisionOption] = Field(default_factory=list)
    primary_label: str
    secondary_label: str
    secondary_action: Literal["decline", "hold"] = "decline"
    extra_actions: list[DecisionAction] = Field(default_factory=list)
    allow_rule: bool = True
    preview_available: bool = False
    evidence_link_label: str | None = None
    blocked_attempts: list[BlockedAttempt] = Field(default_factory=list)
    effect_note: str | None = None

    @model_validator(mode="after")
    def one_recommended(self) -> DecisionCard:
        if sum(1 for option in self.options if option.recommended) > 1:
            raise ValueError("at most one recommended option")
        if len({option.option_id for option in self.options}) != len(self.options):
            raise ValueError("option ids must be unique")
        return self


class RequestAlert(InboxModel):
    """Footer line: '샌드박스가 이 결재를 직접 승인하려다 차단됐습니다. 08:55 · 활동에서 보기'."""

    kind: Literal["sandbox_blocked", "auto_reply", "info"]
    message: str
    at: datetime
    activity_id: Id | None = None


class DecisionRecord(InboxModel):
    """Recorded outcome once a request leaves needs_approval/blocked."""

    action: DecisionAction
    option_id: Id | None = None
    decided_by: Id
    decided_at: datetime
    rule_id: Id | None = None
    note: str | None = None


class RequestDetail(RequestSummary):
    source: SourceRef
    received_via: str
    original: OriginalView
    decision: DecisionCard | None = None
    outcome: DecisionRecord | None = None
    alerts: list[RequestAlert] = Field(default_factory=list)
    policy_link_label: str | None = None


# ---------------------------------------------------------------- decisions
class DecisionSubmit(InboxModel):
    """POST .../decision body. `approval_id` must equal the card shown (stale card = 409)."""

    approval_id: Id
    action: DecisionAction
    option_id: Id | None = None
    save_as_rule: bool = False
    note: str | None = Field(default=None, max_length=2000)
    idempotency_key: Id | None = None

    @model_validator(mode="after")
    def option_required_for_respond(self) -> DecisionSubmit:
        if self.action == "respond" and self.option_id is None:
            raise ValueError("respond requires option_id")
        if self.action != "respond" and self.option_id is not None:
            raise ValueError("option_id only applies to respond")
        return self


class UpstreamRef(InboxModel):
    authority: Literal["rfa_module.review", "runtime.sandbox", "core", "reference"]
    reference: str | None = None
    simulated: bool


class Rule(InboxModel):
    """'비슷한 요청에 이 결정을 규칙으로 쓰기' → later rows show '결재 9의 규칙으로 자동응답'."""

    rule_id: Id
    inbox_id: Id
    kind: DecisionKind
    action: DecisionAction
    option_id: Id | None = None
    created_from_approval_id: Id
    description: str
    created_at: datetime
    applied_count: int = Field(ge=0, default=0)
    enabled: bool = True


class DecisionReceipt(InboxModel):
    request_id: Id
    approval_id: Id
    status: RequestStatus
    outcome: DecisionRecord
    rule: Rule | None = None
    upstream: UpstreamRef


class ReplyPreview(InboxModel):
    """'응답 전문 미리보기' for a given option (never sent from here)."""

    request_id: Id
    option_id: Id
    body: str
    excluded: list[str] = Field(default_factory=list)
    generated_at: datetime
    simulated: bool


# ---------------------------------------------------------------- rules / notifications / activity
class Notification(InboxModel):
    notification_id: Id
    kind: Literal["needs_approval", "blocked", "auto_reply", "decided", "info"]
    message: str
    at: datetime
    request_id: Id | None = None
    read: bool = False


class ActivityEvent(InboxModel):
    """'활동 기록' rows, including sandbox blocks (POST /reviews/14/approve → 결재는 사람만)."""

    activity_id: Id
    at: datetime
    agent_id: Id
    sandbox_id: Id
    action: str
    outcome: Literal["blocked", "allowed", "info"]
    reason: str | None = None
    request_id: Id | None = None


# ---------------------------------------------------------------- admin · agents
class ContextBreakdown(InboxModel):
    system_prompt: int = Field(ge=0)
    tool_definitions: int = Field(ge=0)
    memory_notes: int = Field(ge=0)
    conversation: int = Field(ge=0)


class ContextUsage(InboxModel):
    compaction: Literal["safeguard", "none"] = "safeguard"
    preserve_recent_turns: int = Field(ge=0)
    used_tokens: int = Field(ge=0)
    limit_tokens: int = Field(ge=1)
    breakdown: ContextBreakdown


class LoadedSource(InboxModel):
    """'불러오는 자료' toggle rows; unavailable ones explain why (다른 구획)."""

    source_id: Id
    title: str
    clearance: Clearance
    enabled: bool
    available: bool = True
    unavailable_reason: str | None = None


class DailyCount(InboxModel):
    day: date
    calls: int = Field(ge=0)


class CallStats(InboxModel):
    day: date
    provider: str
    model: str
    calls: int = Field(ge=0)
    tokens_thousands: int = Field(ge=0)
    avg_latency_seconds: float = Field(ge=0)
    blocked_calls: int = Field(ge=0)
    last_7_days: list[DailyCount] = Field(default_factory=list)


class AdminAgent(InboxModel):
    agent_id: Id
    name: str
    description: str
    inbox_id: Id | None = None
    inbox_name: str
    sandbox_id: Id
    clearance: Clearance
    status: Literal["running", "waiting_decision", "stopped"]
    calls_today: int = Field(ge=0)


class AdminAgentDetail(AdminAgent):
    context: ContextUsage
    sources: list[LoadedSource] = Field(default_factory=list)
    stats: CallStats


class AgentSourceToggle(InboxModel):
    enabled: bool


# ---------------------------------------------------------------- admin · sandboxes
InferenceProvider = Literal["ollama_local", "nvidia_endpoints"]


class ProviderOption(InboxModel):
    provider: InferenceProvider
    label: str
    selectable: bool
    reason: str | None = None


class InferenceSettings(InboxModel):
    provider: InferenceProvider
    model: str
    context_length: int = Field(ge=1)
    max_output_tokens: int = Field(ge=1)
    provider_options: list[ProviderOption] = Field(default_factory=list)
    models: list[str] = Field(default_factory=list)
    context_length_choices: list[int] = Field(default_factory=list)


class GatewayInfo(InboxModel):
    gateway_id: Id
    label: str
    port: int = Field(ge=1, le=65535)
    providers: list[str] = Field(default_factory=list)
    has_external_key: bool
    control_port: int | None = Field(default=None, ge=1, le=65535)
    current_route: str
    shared_with: list[Id] = Field(default_factory=list)


class NetworkPolicy(InboxModel):
    policy_id: Id
    description: str
    enabled: bool
    note: str | None = None


class Sandbox(InboxModel):
    sandbox_id: Id
    name: str
    clearance: Clearance
    inbox_ids: list[Id] = Field(default_factory=list)
    inbox_names: list[str] = Field(default_factory=list)
    provider: InferenceProvider
    gateway_port: int = Field(ge=1, le=65535)
    status: Literal["running", "stopped"]
    agent_count: int = Field(ge=0)


class SandboxDetail(Sandbox):
    agents_manifest: str
    inference: InferenceSettings
    gateway: GatewayInfo
    gateways: list[GatewayInfo] = Field(default_factory=list)
    policies: list[NetworkPolicy] = Field(default_factory=list)
    pending_changes: bool = False
    apply_note: str = "제공자와 모델은 바로 적용 · 컨텍스트 길이와 게이트웨이는 다시 만들 때 적용"


class SandboxSettingsPatch(InboxModel):
    """PATCH body; only present fields change. Server rejects unselectable providers."""

    provider: InferenceProvider | None = None
    model: str | None = None
    context_length: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    gateway_id: Id | None = None
    policies: dict[str, bool] | None = None


class ApplyResult(InboxModel):
    sandbox_id: Id
    applied: list[str]
    requires_recreate: list[str]
    upstream: UpstreamRef


class InboxError(InboxModel):
    error: Literal[
        "not_found", "invalid_state", "stale_approval", "invalid_option", "not_selectable"
    ]
    message: str
