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


class AgentRef(BaseModel):
    id: str
    name: str
    color: str = Field(description="#rrggbb, stable per agent id")
    sandbox: str | None
    description: str = ""
    kind: Literal["assistant", "task", "supervisor"] = "task"


class Me(BaseModel):
    authenticated: bool
    role: Role
    isAdmin: bool


class TaskView(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    agentId: str
    sandbox: str | None
    gradeLabel: str = Field(description="security grade of the task agent's sandbox (privilege + groups)")
    source: Literal["catalog", "team"] = "catalog"
