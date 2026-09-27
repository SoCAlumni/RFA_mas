from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from rfa_mas.nemoclaw.schemas.common import Role

CONVERSATION_ID = r"^[A-Za-z0-9_.:-]{1,64}$"


class HistoryItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: Literal["user", "assistant"] = "user"
    text: str = Field(max_length=8000)


class ChatStreamRequest(BaseModel):
    """``POST /chat`` (SSE). ``role`` is what the front-end claims; only an authenticated caller
    (bearer RFA_ASK_TOKEN) is granted ``owner`` — anyone else is treated as ``guest``. The owner's
    conversation is stored (earlier turns come from the server); a guest's is not, so a guest sends its
    earlier turns in ``history``."""

    model_config = ConfigDict(extra="ignore")

    text: str = Field(min_length=1, max_length=10000)
    role: Role = "guest"
    conversationId: str | None = Field(default=None, max_length=64, pattern=CONVERSATION_ID)
    agentId: str | None = Field(default=None, max_length=64,
                                description="pinned chat partner (`#chat/{agentId}`): skip ranking, delegate to it")
    history: list[HistoryItem] = Field(default_factory=list, max_length=50, description="guest only")


class ConversationCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agentId: str = Field(default="assistant", max_length=64)


class ConversationSummary(BaseModel):
    id: str
    agentId: str
    title: str = Field(description="first user message, else 새 대화")
    preview: str = ""
    createdAt: float
    updatedAt: float
    busy: bool = Field(description="a run is streaming in this conversation")
    count: int = Field(description="user messages")
    persisted: bool = Field(description="false for a guest: the client keeps the turns")


class ChatMessage(BaseModel):
    """A user message ``{id, role: user, text, ts}`` or a finished assistant turn (the front-end reducer's
    shape: ``status, phase, preface, search, selection, calls, guard, answer, runId, durationMs, error``)."""

    model_config = ConfigDict(extra="allow")

    id: str
    role: Literal["user", "assistant"]
    ts: float
    text: str | None = None
    status: str | None = None
    preface: str | None = None
    search: dict[str, Any] | None = None
    selection: dict[str, Any] | None = None
    calls: list[dict[str, Any]] | None = None
    guard: dict[str, Any] | None = None
    answer: str | None = None


class ConversationDetail(ConversationSummary):
    messages: list[ChatMessage]


# SSE event names (data shapes documented here; the envelope is schemas.common.SseEnvelope)
CHAT_EVENTS = {
    "run.start": "{runId, conversationId, messageId, role, agentId, level: public|company, requestId}",
    "assistant.delta": "{text} — the 요청 파악 preface",
    "agents.search": "{query, candidates: [{agentId, reason, score 0..1}]}",
    "agents.select": "{selected: [{agentId, task}], skipped: [{agentId, reason}]} — every candidate ≥ head.select_threshold",
    "delegate.start": "{callId, agentId, task}",
    "delegate.log": "{callId, text} — guest: fixed phrases only",
    "delegate.end": "{callId, status: ok|none|blocked|error, summary, refs: [], durationMs}",
    "guard.start": "{level: public(사외)|company(사내)}",
    "guard.end": "{status: pass|redacted|blocked, note, redactions: int}",
    "answer.delta": "{text} — one sentence of the checked answer",
    "run.end": "{status: done|error, durationMs, messageId, error?, code?} (stop = the client closes the stream)",
}
