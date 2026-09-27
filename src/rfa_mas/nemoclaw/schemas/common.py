from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SSE_ENVELOPE_VERSION = 1
Role = Literal["owner", "guest"]


class SseEnvelope(BaseModel):
    """Every SSE event of every stream: ``{type, runId, seq, ts, data}``. ``type`` names may change
    with the front-end; the envelope does not."""

    type: str = Field(description="event name, e.g. run.started | stage | delta | guard.final | done | error")
    runId: str
    seq: int = Field(ge=0, description="0-based, strictly increasing within a run")
    ts: float = Field(description="unix seconds")
    data: dict[str, Any] = Field(default_factory=dict)


class Color(BaseModel):
    bg: str = Field(description="#rrggbb background")
    fg: str = Field(description="#rrggbb foreground")


AgentStatus = Literal["running", "waiting_decision", "stopped", "applying"]
Icon = Literal["star", "github", "research", "slack", "mail", "shield", "generic"]


class AgentView(BaseModel):
    """A chat partner: the assistant or one task's agent (one task = one agent). No 사내/사외 grade here —
    the grade belongs to each request (in chat: to the asker's role)."""

    id: str = Field(description="chat route id (`#chat/{id}`); equals taskId for task agents")
    name: str
    kind: Literal["assistant", "task"]
    icon: Icon = "generic"
    color: Color
    initials: str = Field("", description="shown when icon is generic")
    description: str = ""
    taskId: str | None = None
    taskName: str | None = None
    desk: str | None = None
    status: AgentStatus = "running"
    itemCount: int = Field(0, description="pending approvals of this task (0 until the approval server is wired)")
    tags: list[str] = []
    suggestions: list[str] = Field(default_factory=list, description="assistant: the global suggestions")


class Me(BaseModel):
    authenticated: bool
    role: Role
    isAdmin: bool
    name: str


class TaskView(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str = Field(description="task name (left-nav title)")
    agentId: str = Field(description="its chat partner (`GET /agents`)")
    agentName: str
    desk: str = Field(description="left-nav subtitle")
    icon: Icon = "generic"
    color: Color
    initials: str = ""
    itemCount: int = 0
    status: Literal["ready", "applying", "failed"] = "ready"
    error: str | None = None
    source: Literal["catalog", "team"] = "catalog"
    worker: str | None = Field(None, description="backend agent that answers (task agent or team supervisor)")
    sandbox: str | None = None
    securityLabel: str = Field("", description="sandbox network privilege (admin), not the 사내/사외 grade")


class ErrorOut(BaseModel):
    """Front-end error body: ``code`` for the client, ``message`` ready to show (Korean)."""

    code: str
    message: str


class TaskCreateRequest(BaseModel):
    """``POST /tasks`` (「태스크 추가」). No 사내/사외 grade: it belongs to each request, not the task."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(default="", max_length=30, description="태스크명 (비면 422 task_name_required)")
    agentName: str | None = Field(default=None, max_length=40, description="비면 「{태스크명} 대응 에이전트」")
    description: str | None = Field(default=None, max_length=200, description="비면 「{태스크명} 태스크를 맡은 에이전트입니다.」")
    tags: list[str] = Field(default_factory=list, max_length=32,
                            description="≤8 kept; # stripped, trimmed, de-duplicated. A tag in a question routes it here")


TASK_EVENTS = {
    "task.start": "{taskId, name, agentName}",
    "task.stage": "{stage: analyze|design|spawn, status: running|done|error, ms, detail} — analyze.detail {capabilities: "
                  "[{id, label}], source}, design.detail {roles, supervisor, members: [{agentId, role}], sandbox}, "
                  "spawn.detail {agentsApply, seeded}",
    "task.log": "{stage, text}",
    "task.done": "{task: TaskView, agent: AgentView, ms}",
    "task.error": "{stage, code: no_role_for_requirement|invalid_team|apply_failed|task_create_failed, message, detail}",
}
