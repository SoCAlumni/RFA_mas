from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from rfa_mas.nemoclaw.schemas.common import Role


class HistoryItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: Literal["user", "assistant"] = "user"
    text: str = Field(max_length=8000)


class ChatStreamRequest(BaseModel):
    """``POST /chat`` (SSE). ``role`` is what the front-end claims; only an authenticated caller
    (bearer RFA_ASK_TOKEN) is granted ``owner`` — anyone else is treated as ``guest``."""

    model_config = ConfigDict(extra="ignore")

    text: str = Field(min_length=1, max_length=10000)
    role: Role = "guest"
    conversationId: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_.:-]{1,64}$")
    history: list[HistoryItem] = Field(default_factory=list, max_length=50)


# SSE event names (data shapes documented here; the envelope is schemas.common.SseEnvelope)
CHAT_EVENTS = {
    "run.started": "{role, audience, profile, conversationId, requestId}",
    "stage": "{stage: head|task:<id>|censor, ms, outcome, …}",
    "guard.final": "guest only — {verdict: allow|redact|block, redactions: [reason], blocked: bool, reason?}",
    "delta": "{text} — one sentence of the censored answer",
    "done": "{requestId, task: {id,name}|null, refusal: {code,message}|null, censor: {profile,verdict,redactions}, ms}",
    "error": "{code, message}",
}
