"""결재함 (D-20~D-24) and 소스."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from rfa_mas.nemoclaw.schemas.common import Color

Phase = Literal["drafting", "stalled", "pending", "regenerating", "posting", "publish_failed", "posted", "closed"]


class Badge(BaseModel):
    label: str = Field(description="작성 중 | 초안 없음 | 결재 필요 | 차단됨 | 재생성 중 | 보내는 중 | 게시 실패 | 응답 완료 | 결재 완료")
    tone: Literal["info", "warn", "danger", "ok", "muted"]


class InboxTask(BaseModel):
    id: str
    name: str


class InboxAgent(BaseModel):
    id: str
    name: str
    desk: str = Field(description="agent tag text (e.g. infer-opt)")
    icon: str
    color: Color
    initials: str


class InboxItem(BaseModel):
    id: str = Field(description="stable per mention URL (before and after the approval exists)")
    approvalId: int | None = Field(description="RFA_module approval id (결재 N); null while drafting")
    sourceUrl: str
    channel: str
    channelLabel: str
    target: str | None
    requester: str
    requesterInitials: str
    title: str
    grade: Literal["public", "company"]
    gradeLabel: str = Field(description="사외 | 사내")
    gradeSource: Literal["source", "channel"]
    status: Phase
    badge: Badge
    statusLine: str
    round: int
    task: InboxTask | None
    agent: InboxAgent | None
    injection: bool
    arrivedAt: float | None
    updatedAt: float | None


class InboxSummary(BaseModel):
    needsApproval: int = Field(description="「결재 필요 N」, 결재함 badge")
    byTask: dict[str, int] = Field(description="pending per task id (left-nav badges)")
    byStatus: dict[str, int]
    autoReplied: int = 0
    total: int


class SourceMessage(BaseModel):
    author: str | None
    text: str | None
    at: float | None
    isRequest: bool = False


class SourceView(BaseModel):
    """원본 화면: GitHub (repo, number, title, body, comments) or Slack (channel, messages)."""

    model_config = ConfigDict(extra="allow")

    kind: Literal["github", "slack"]
    url: str


class InjectionView(BaseModel):
    sentences: list[dict]
    note: str


class Regeneration(BaseModel):
    request: str | None
    at: float | None
    draft: str | None


class DraftView(BaseModel):
    text: str
    chars: int
    round: int
    phase: Literal["drafting", "stalled", "ready", "regenerating", "posting", "publish_failed", "posted", "closed"]
    regenerations: list[Regeneration]
    lastRequest: str | None
    postedUrl: str | None
    publishError: str | None
    canRespond: bool
    canRegenerate: bool
    regenerationsLeft: int
    closesOnRegenerate: bool = Field(description="RFA_module closes the approval on its 3rd rejection")


class StepDetail(BaseModel):
    label: str
    meta: str
    tone: Literal["ok", "warn", "block"] = "ok"


class Step(BaseModel):
    key: Literal["rag", "verify", "draft"]
    title: str
    state: Literal["pending", "running", "done", "error", "skipped", "unknown"]
    ms: int | None
    summary: str
    details: list[StepDetail]


class BlockedAttempt(BaseModel):
    at: float | None
    kind: str | None
    action: str
    reason: str


class ApprovalEvent(BaseModel):
    at: float | None
    who: str | None
    what: str | None
    detail: str | None


class InboxDetail(InboxItem):
    question: str
    source: SourceView
    injection: InjectionView | None
    refusal: str | None
    draft: DraftView
    steps: list[Step]
    blockedAttempts: list[BlockedAttempt]
    events: list[ApprovalEvent]


class RespondIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    draft: str | None = Field(None, description="the text on screen; must equal the stored draft (D-22)")


class RegenerateIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    request: str = Field(max_length=400, description="재생성 요청 내용 (RFA_module reject 사유로 전달)")


class SourceOut(BaseModel):
    id: str
    taskId: str
    kind: Literal["github", "slack"]
    kindLabel: str
    target: str
    scopes: list[str]
    grade: Literal["public", "company"]
    gradeLabel: str
    status: Literal["connected"]
    createdAt: float
    requestCount: int
    lastRequestAt: float | None


class SourceIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: Literal["github", "slack"]
    target: str = Field(max_length=200)
    scopes: list[str] = Field(default_factory=list, max_length=6)
    grade: str | None = Field(None, description="public|company (사외|사내); default github 사외, slack 사내")
