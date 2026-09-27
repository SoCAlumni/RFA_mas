"""RFA_module head contract v0.2.0 (``RFA_module/contracts/head.openapi.yaml``), served as
``POST /v1/head/ask`` (D-21). Field names and types follow RFA_module; the extra response fields
(``requestId``, ``itemId`` …) are ignored by RFA_module's client."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ThreadMessageIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    author: str = ""
    text: str = ""
    at: str = Field("", description="ISO date-time")


class RejectionIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    draft: str = ""
    reason: str = ""
    at: str = ""


class HeadAskIn(BaseModel):
    """RFA_module ``AskRequest``. No size limits here: oversized fields are cut, never refused."""

    model_config = ConfigDict(extra="ignore")

    question: str
    channel: Literal["github", "slack"]
    audience: Literal["public", "company"]
    target: str
    url: str
    requester: str = ""
    context: list[ThreadMessageIn] = Field(default_factory=list)
    feedback: list[RejectionIn] = Field(default_factory=list, description="every earlier rejection of this approval")


class TaskRefOut(BaseModel):
    id: str
    name: str


class HeadAskOut(BaseModel):
    """RFA_module ``AskResponse`` (+ rfa_mas extras). ``knowledge`` is already censored; when it is
    empty ``refusal`` is a Korean reason."""

    knowledge: str
    task: TaskRefOut | None
    refusal: str | None
    requestId: str = Field(description="derived: rfa-<sha1(url)>-r<round>; retries of a round share it")
    itemId: str = Field(description="결재함 item id of this mention")
    round: int
    grade: Literal["public", "company"] = Field(description="the request's grade (source grade wins, D-24)")
    gradeSource: Literal["source", "channel"]
    sourceId: str | None = None
    censor: dict[str, Any] = {}
