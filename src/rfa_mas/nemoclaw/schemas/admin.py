"""관리 · 에이전트 / 샌드박스 (D-16~D-19)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from rfa_mas.nemoclaw.schemas.common import Color


class TaskRefOut(BaseModel):
    id: str
    name: str


class AdminAgent(BaseModel):
    id: str
    name: str = Field(description="agent id (list line 1)")
    title: str
    subtitle: str = Field(description="list line 2: `{태스크} 태스크`, team role, or the management role")
    group: Literal["task", "management"] = Field(description="task agents first; management agents are read-only")
    role: Literal["task", "supervisor", "member", "assistant", "censor"]
    parentId: str | None = Field(None, description="team member → its supervisor")
    tasks: list[TaskRefOut] = []
    sandbox: str | None
    sandboxLine: str = Field(description="list line 3, e.g. `rfa-main 샌드박스`")
    securityLabel: str
    status: Literal["running", "stopped", "applying"]
    observed: bool = Field(description="false until the first live `nemoclaw` snapshot (status is then declared)")
    callsToday: int = Field(description="LLM calls today (오늘 N회)")
    icon: str = "generic"
    color: Color
    initials: str
    editable: bool
    readOnlyReason: str | None = Field(None, description="데모에서는 관리 에이전트를 수정할 수 없습니다.")


class AgentCounts(BaseModel):
    total: int
    task: int
    management: int


class AdminAgentList(BaseModel):
    agents: list[AdminAgent]
    counts: AgentCounts = Field(description="sidebar 「에이전트 N」 = total")
    observedAt: float | None
    hint: str


class ContextBreakdown(BaseModel):
    systemPrompt: int
    toolDefinitions: int
    memoryNotes: int
    conversation: int


class ContextUsage(BaseModel):
    usedTokens: int
    limitTokens: int = Field(description="the sandbox's context-length setting")
    measured: bool = Field(description="true: total from the upstream's usage.prompt_tokens; parts estimated")
    measuredAt: float | None
    compaction: str = "safeguard"
    preserveRecentTurns: int = 1
    breakdown: ContextBreakdown


class LoadedSource(BaseModel):
    id: str
    title: str
    description: str = ""
    enabled: bool
    available: bool
    unavailableReason: str | None = None


class DailyCount(BaseModel):
    day: str
    calls: int


class CallStats(BaseModel):
    provider: str
    model: str
    calls: int
    tokens: int
    tokensThousands: float
    avgLatencySeconds: float
    blockedCalls: int
    last7Days: list[DailyCount]


class AgentActions(BaseModel):
    compact: bool
    clearMemory: bool
    editPrompt: bool
    toggleSources: bool


class AdminAgentDetail(AdminAgent):
    description: str
    alias: str | None = None
    skill: str | None = None
    groups: list[str]
    tools: dict[str, Any]
    model: str
    context: ContextUsage | None = Field(description="null until the agent's first LLM call is recorded")
    sources: list[LoadedSource]
    stats: CallStats
    instructions: str
    instructionsUpdatedAt: float | None
    actions: AgentActions


class AgentActionResult(BaseModel):
    agentId: str
    action: Literal["compact", "clear-memory"]
    applied: bool
    removedSessions: int | None = None
    note: str


class PromptView(BaseModel):
    agentId: str
    identity: str = Field(description="IDENTITY.md as seeded (signed marker hidden)")
    skill: str | None
    skillText: str
    instructions: str = Field(description="owner's extra instructions (appended to IDENTITY.md)")
    instructionsUpdatedAt: float | None


class PromptUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    instructions: str = Field(max_length=4000)


class PromptUpdateResult(PromptView):
    applied: bool
    note: str


class SourceToggle(BaseModel):
    enabled: bool


class SourceToggleResult(BaseModel):
    sources: list[LoadedSource]
    applied: bool
    note: str


class SandboxItem(BaseModel):
    id: str
    name: str
    default: bool
    groups: list[str]
    privilege: int
    securityLabel: str
    status: Literal["running", "stopped", "unknown"]
    observed: bool
    agentCount: int
    tasks: list[TaskRefOut]
    provider: str
    model: str
    gatewayPort: int | None


class SandboxList(BaseModel):
    sandboxes: list[SandboxItem]
    limit: int
    canAdd: bool
    limitMessage: str
    observedAt: float | None


class ProviderOption(BaseModel):
    provider: str
    label: str
    selectable: bool
    reason: str | None = None
    models: list[str]


class InferenceSettings(BaseModel):
    provider: str
    providerLabel: str
    model: str
    providerOptions: list[ProviderOption]
    contextLength: int
    maxOutputTokens: int
    contextLengthChoices: list[int]
    maxOutputChoices: list[int]
    pendingRecreate: list[str] = Field(description="stored settings that wait for the next recreate")


class GatewayInfo(BaseModel):
    id: str
    label: str
    url: str | None
    port: int | None
    registeredProvider: str
    hasKey: bool
    sandboxModel: str | None = None
    sandboxProvider: str | None = None
    currentRoute: str
    sharedWith: list[str]


class SecurityGroupView(BaseModel):
    id: str
    privilege: int
    description: str
    presets: list[str]
    mcpServers: list[str]


class SandboxAgentRef(BaseModel):
    id: str
    kind: str


class SandboxDetail(SandboxItem):
    agentsManifest: str
    agents: list[SandboxAgentRef]
    securityGroups: list[SecurityGroupView]
    inference: InferenceSettings
    gateway: GatewayInfo
    applyNote: str


class SandboxPatch(BaseModel):
    """Only present fields change. provider/model apply now; the lengths wait for a recreate."""

    model_config = ConfigDict(extra="ignore")

    provider: str | None = None
    model: str | None = None
    contextLength: int | None = None
    maxOutputTokens: int | None = None


class SandboxUpdateResult(BaseModel):
    sandboxId: str
    applied: list[str]
    requiresRecreate: list[str]
    sandbox: SandboxDetail
