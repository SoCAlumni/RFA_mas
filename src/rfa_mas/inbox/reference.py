"""Reference server for the `/v1/inbox` contract, driven by the UI PoC sample data.

mode=reference, simulated=true. State lives in memory and starts from
`fixtures/inbox/poc_cases.json`; nothing here approves, publishes or changes a sandbox.
The point is to let the UI (다영) and the upstream adapters (승희 review, 다영 runtime,
this core) be built against one schema. Every operation declares its real authority via
`x-rfa-authority` so the UI knows which calls stay reference-only for now.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from rfa_mas.inbox.contract import (
    INBOX_CONTRACT_VERSION,
    ActivityEvent,
    AdminAgent,
    AdminAgentDetail,
    AgentSourceToggle,
    ApplyResult,
    DecisionAction,
    DecisionReceipt,
    DecisionRecord,
    DecisionSubmit,
    GatewayInfo,
    InboxCounts,
    InboxError,
    InboxSummary,
    InboxUser,
    InferenceSettings,
    Notification,
    ProviderOption,
    ReplyPreview,
    RequestDetail,
    RequestPage,
    RequestStatus,
    RequestSummary,
    Rule,
    Sandbox,
    SandboxDetail,
    SandboxSettingsPatch,
    UpstreamRef,
)

FIXTURE = Path(__file__).resolve().parents[3] / "fixtures" / "inbox" / "poc_cases.json"
SERVICE_NAME = "rfa-inbox-reference"
OPEN_STATUSES: frozenset[str] = frozenset({"needs_approval", "blocked", "held", "needs_human"})
UPSTREAM = UpstreamRef(authority="reference", reference=None, simulated=True)
# Blocked-request handling options (PoC "이 요청을 어떻게 할까요?") → resulting status.
BLOCKED_HANDLING: dict[str, tuple[RequestStatus, str]] = {
    "no-reply": ("declined", "응답하지 않기로 결정"),
    "answer-question-only": ("in_progress", "질문에만 답하기 · 공개 범위 결재 대기"),
    "block-author": ("declined", "작성자 요청 받지 않기"),
}
ACTION_TO_HANDLING: dict[str, str] = {
    "answer_question_only": "answer-question-only",
    "block_author": "block-author",
}


def authority(name: str) -> dict[str, Any]:
    return {"x-rfa-authority": name}


class InboxReferenceError(Exception):
    def __init__(self, status_code: int, error: InboxError) -> None:
        super().__init__(error.message)
        self.status_code, self.error = status_code, error


def _not_found(what: str) -> InboxReferenceError:
    return InboxReferenceError(404, InboxError(error="not_found", message=f"{what} not found"))


class InboxReferenceState:
    """In-memory PoC state. Deterministic given the fixture; one asyncio lock for writes."""

    def __init__(self, data: dict[str, Any], *, now: datetime | None = None) -> None:
        self.user = InboxUser.model_validate(data["user"])
        self.inboxes: dict[str, dict[str, Any]] = {i["inbox_id"]: i for i in data["inboxes"]}
        self.requests: dict[str, RequestDetail] = {
            r["request_id"]: RequestDetail.model_validate(r) for r in data["requests"]
        }
        self.previews: dict[str, dict[str, str]] = data.get("previews", {})
        self.notifications: dict[str, Notification] = {
            n["notification_id"]: Notification.model_validate(n) for n in data["notifications"]
        }
        self.activity: list[ActivityEvent] = [
            ActivityEvent.model_validate(a) for a in data["activity"]
        ]
        self.rules: dict[str, Rule] = {r["rule_id"]: Rule.model_validate(r) for r in data["rules"]}
        self.agents: dict[str, dict[str, Any]] = {a["agent_id"]: a for a in data["agents"]}
        self.agent_details: dict[str, dict[str, Any]] = data.get("agent_details", {})
        self.gateways: dict[str, GatewayInfo] = {
            g["gateway_id"]: GatewayInfo.model_validate(g) for g in data["gateways"]
        }
        self.sandboxes: dict[str, dict[str, Any]] = {
            s["sandbox_id"]: dict(s, pending_changes=False) for s in data["sandboxes"]
        }
        self.models: dict[str, list[str]] = data["models"]
        self.context_length_choices: list[int] = data["context_length_choices"]
        self.receipts: dict[tuple[str, str], tuple[str, DecisionReceipt]] = {}
        self.sequence = 100
        self.now = now
        self.lock = asyncio.Lock()

    @classmethod
    def from_fixture(cls, path: Path = FIXTURE, **kwargs: Any) -> InboxReferenceState:
        return cls(json.loads(path.read_text(encoding="utf-8")), **kwargs)

    def clock(self) -> datetime:
        return self.now or datetime.now(UTC)

    def next_id(self, prefix: str) -> str:
        self.sequence += 1
        return f"{prefix}-{self.sequence}"

    # ---- read views -------------------------------------------------------------------
    def counts(self) -> InboxCounts:
        pending = sum(1 for r in self.requests.values() if r.status in OPEN_STATUSES)
        return InboxCounts(
            needs_approval=pending,
            notifications_unread=sum(1 for n in self.notifications.values() if not n.read),
            all_inboxes=pending,
        )

    def inbox_summaries(self) -> list[InboxSummary]:
        return [
            InboxSummary(
                **inbox,
                pending_count=sum(
                    1
                    for r in self.requests.values()
                    if r.inbox_id == inbox["inbox_id"] and r.status in OPEN_STATUSES
                ),
            )
            for inbox in self.inboxes.values()
        ]

    def request_page(
        self,
        *,
        inbox_id: str | None,
        status: str | None,
        query: str | None,
        sort: str,
        limit: int,
    ) -> RequestPage:
        rows = list(self.requests.values())
        if inbox_id is not None:
            rows = [r for r in rows if r.inbox_id == inbox_id]
        if status == "open":
            rows = [r for r in rows if r.status in OPEN_STATUSES]
        elif status is not None:
            rows = [r for r in rows if r.status == status]
        if query:
            needle = query.casefold()
            rows = [
                r
                for r in rows
                if needle in r.title.casefold()
                or needle in r.subtitle.casefold()
                or needle in r.requester.display_name.casefold()
            ]
        rows.sort(key=lambda r: r.received_at, reverse=(sort == "received_desc"))
        summaries = [
            RequestSummary.model_validate(r.model_dump(include=set(RequestSummary.model_fields)))
            for r in rows[:limit]
        ]
        return RequestPage(items=summaries, next_cursor=None, counts=self.counts())

    def request(self, request_id: str) -> RequestDetail:
        try:
            return self.requests[request_id]
        except KeyError:
            raise _not_found("request") from None

    # ---- decisions --------------------------------------------------------------------
    def decide(self, request_id: str, body: DecisionSubmit, *, decided_by: str) -> DecisionReceipt:
        detail = self.request(request_id)
        key = (request_id, body.idempotency_key) if body.idempotency_key else None
        if key is not None and key in self.receipts:
            fingerprint, receipt = self.receipts[key]
            if fingerprint != body.model_dump_json():
                raise InboxReferenceError(
                    409,
                    InboxError(error="invalid_state", message="idempotency key reused"),
                )
            return receipt
        if detail.status not in OPEN_STATUSES or detail.decision is None:
            raise InboxReferenceError(
                409,
                InboxError(error="invalid_state", message=f"request is {detail.status}"),
            )
        card = detail.decision
        if body.approval_id != card.approval_id:
            raise InboxReferenceError(
                409, InboxError(error="stale_approval", message="approval card changed")
            )
        option = None
        if body.option_id is not None:
            option = next((o for o in card.options if o.option_id == body.option_id), None)
            if option is None:
                raise InboxReferenceError(
                    422, InboxError(error="invalid_option", message="unknown option")
                )
        if body.action in ACTION_TO_HANDLING and card.kind != "blocked_handling":
            raise InboxReferenceError(
                422, InboxError(error="invalid_option", message="action needs blocked card")
            )
        status, subtitle = self._transition(card.kind, body.action, option)
        now = self.clock()
        approval_label = card.approval_id.rsplit("-", 1)[-1]
        outcome = DecisionRecord(
            action=body.action,
            option_id=body.option_id,
            decided_by=decided_by,
            decided_at=now,
            note=body.note,
        )
        rule = None
        if body.save_as_rule and body.action != "hold":
            rule = Rule(
                rule_id=self.next_id("rule"),
                inbox_id=detail.inbox_id,
                kind=card.kind,
                action=body.action,
                option_id=body.option_id,
                created_from_approval_id=card.approval_id,
                description=(
                    f"'{detail.title}'와 비슷한 요청은 "
                    f"{option.label if option else body.action}로 처리"
                ),
                created_at=now,
            )
            self.rules[rule.rule_id] = rule
            outcome = outcome.model_copy(update={"rule_id": rule.rule_id})
        updated = detail.model_copy(
            update={
                "status": status,
                "subtitle": f"결재 {approval_label} · {subtitle}",
                "is_new": False,
                "outcome": outcome,
                "decision": None if status not in OPEN_STATUSES else card,
            }
        )
        self.requests[request_id] = updated
        notification = Notification(
            notification_id=self.next_id("ntf"),
            kind="decided",
            message=f"결재 {approval_label}: {subtitle}",
            at=now,
            request_id=request_id,
        )
        self.notifications[notification.notification_id] = notification
        receipt = DecisionReceipt(
            request_id=request_id,
            approval_id=card.approval_id,
            status=status,
            outcome=outcome,
            rule=rule,
            upstream=UPSTREAM,
        )
        if key is not None:
            self.receipts[key] = (body.model_dump_json(), receipt)
        return receipt

    @staticmethod
    def _transition(kind: str, action: DecisionAction, option) -> tuple[RequestStatus, str]:
        if action == "hold":
            return "held", "보류"
        if kind == "blocked_handling":
            handling = ACTION_TO_HANDLING.get(action) or (option.option_id if option else None)
            if action == "decline":
                handling = "no-reply"
            if handling not in BLOCKED_HANDLING:
                raise InboxReferenceError(
                    422, InboxError(error="invalid_option", message="unknown handling")
                )
            return BLOCKED_HANDLING[handling]
        if action == "decline":
            verb = "답장하지 않기로 결정" if kind == "share_scope" else "응답하지 않기로 결정"
            return "declined", verb
        if action != "respond" or option is None:
            raise InboxReferenceError(
                422, InboxError(error="invalid_option", message="respond needs an option")
            )
        if kind == "research_direction":
            return "decided", f"{option.label}로 진행"
        return "decided", f"{option.label}로 응답"

    def preview(self, request_id: str, option_id: str) -> ReplyPreview:
        detail = self.request(request_id)
        card = detail.decision
        if card is None or all(o.option_id != option_id for o in card.options):
            raise _not_found("option")
        body = self.previews.get(request_id, {}).get(option_id)
        if body is None:
            raise _not_found("preview")
        return ReplyPreview(
            request_id=request_id,
            option_id=option_id,
            body=body,
            excluded=list(card.always_excluded),
            generated_at=self.clock(),
            simulated=True,
        )

    # ---- admin -------------------------------------------------------------------------
    def agent(self, agent_id: str) -> AdminAgentDetail:
        base = self.agents.get(agent_id)
        if base is None:
            raise _not_found("agent")
        extra = self.agent_details.get(agent_id) or _default_agent_detail(base)
        return AdminAgentDetail(**base, **extra)

    def toggle_source(self, agent_id: str, source_id: str, enabled: bool) -> AdminAgentDetail:
        detail = self.agent(agent_id)
        sources = []
        found = False
        for source in detail.sources:
            if source.source_id == source_id:
                found = True
                if not source.available:
                    raise InboxReferenceError(
                        409,
                        InboxError(
                            error="not_selectable",
                            message=source.unavailable_reason or "unavailable",
                        ),
                    )
                source = source.model_copy(update={"enabled": enabled})
            sources.append(source)
        if not found:
            raise _not_found("source")
        self.agent_details[agent_id] = dict(
            self.agent_details.get(agent_id, _default_agent_detail(self.agents[agent_id])),
            sources=[s.model_dump(mode="json") for s in sources],
        )
        return self.agent(agent_id)

    def compact(self, agent_id: str, *, clear: bool) -> AdminAgentDetail:
        detail = self.agent(agent_id)
        breakdown = detail.context.breakdown.model_copy(
            update={
                "conversation": 0,
                "memory_notes": 0 if clear else detail.context.breakdown.memory_notes,
            }
        )
        used = (
            breakdown.system_prompt
            + breakdown.tool_definitions
            + breakdown.memory_notes
            + breakdown.conversation
        )
        context = detail.context.model_copy(update={"breakdown": breakdown, "used_tokens": used})
        self.agent_details[agent_id] = dict(
            self.agent_details.get(agent_id, _default_agent_detail(self.agents[agent_id])),
            context=context.model_dump(mode="json"),
        )
        return self.agent(agent_id)

    def sandbox_list(self) -> list[Sandbox]:
        return [Sandbox.model_validate(_sandbox_base(s)) for s in self.sandboxes.values()]

    def sandbox(self, sandbox_id: str) -> SandboxDetail:
        raw = self.sandboxes.get(sandbox_id)
        if raw is None:
            raise _not_found("sandbox")
        gateway = self.gateways[raw["gateway_id"]]
        provider_options = [
            ProviderOption(
                provider="ollama_local",
                label="Ollama 로컬",
                selectable=True,
                reason="프롬프트가 이 PC 밖으로 나가지 않습니다.",
            ),
            ProviderOption(
                provider="nvidia_endpoints",
                label="NVIDIA Endpoints",
                selectable=raw["clearance"] == "public",
                reason=None
                if raw["clearance"] == "public"
                else "공개 등급 샌드박스에서만 쓸 수 있습니다.",
            ),
        ]
        return SandboxDetail(
            **_sandbox_base(raw),
            agents_manifest=raw["agents_manifest"],
            inference=InferenceSettings(
                provider=raw["provider"],
                model=raw["model"],
                context_length=raw["context_length"],
                max_output_tokens=raw["max_output_tokens"],
                provider_options=provider_options,
                models=list(self.models[raw["provider"]]),
                context_length_choices=list(self.context_length_choices),
            ),
            gateway=gateway,
            gateways=list(self.gateways.values()),
            policies=raw["policies"],
            pending_changes=raw["pending_changes"],
        )

    def patch_sandbox(self, sandbox_id: str, patch: SandboxSettingsPatch) -> SandboxDetail:
        raw = self.sandboxes.get(sandbox_id)
        if raw is None:
            raise _not_found("sandbox")
        changes: dict[str, Any] = {}
        if patch.provider is not None:
            if patch.provider == "nvidia_endpoints" and raw["clearance"] != "public":
                raise InboxReferenceError(
                    409,
                    InboxError(
                        error="not_selectable",
                        message="NVIDIA Endpoints는 공개 등급 샌드박스에서만 쓸 수 있습니다.",
                    ),
                )
            changes["provider"] = patch.provider
        provider = changes.get("provider", raw["provider"])
        if patch.model is not None:
            if patch.model not in self.models[provider]:
                raise InboxReferenceError(
                    422, InboxError(error="invalid_option", message="unknown model")
                )
            changes["model"] = patch.model
        elif "provider" in changes and raw["model"] not in self.models[provider]:
            changes["model"] = self.models[provider][0]
        if patch.context_length is not None:
            if patch.context_length not in self.context_length_choices:
                raise InboxReferenceError(
                    422, InboxError(error="invalid_option", message="unknown context length")
                )
            changes["context_length"] = patch.context_length
        if patch.max_output_tokens is not None:
            changes["max_output_tokens"] = patch.max_output_tokens
        if patch.gateway_id is not None:
            if patch.gateway_id not in self.gateways:
                raise _not_found("gateway")
            changes["gateway_id"] = patch.gateway_id
            changes["gateway_port"] = self.gateways[patch.gateway_id].port
        if patch.policies is not None:
            known = {p["policy_id"] for p in raw["policies"]}
            unknown = set(patch.policies) - known
            if unknown:
                raise _not_found("policy")
            changes["policies"] = [
                dict(p, enabled=patch.policies.get(p["policy_id"], p["enabled"]))
                for p in raw["policies"]
            ]
        if changes:
            raw.update(changes)
            raw["pending_changes"] = True
        return self.sandbox(sandbox_id)

    def apply_sandbox(self, sandbox_id: str) -> ApplyResult:
        raw = self.sandboxes.get(sandbox_id)
        if raw is None:
            raise _not_found("sandbox")
        applied = ["provider", "model", "max_output_tokens", "policies"]
        recreate = ["context_length", "gateway"]
        raw["pending_changes"] = False
        return ApplyResult(
            sandbox_id=sandbox_id, applied=applied, requires_recreate=recreate, upstream=UPSTREAM
        )


def _sandbox_base(raw: dict[str, Any]) -> dict[str, Any]:
    return {k: raw[k] for k in Sandbox.model_fields}


def _default_agent_detail(base: dict[str, Any]) -> dict[str, Any]:
    today = "2026-09-27"
    return {
        "context": {
            "compaction": "safeguard",
            "preserve_recent_turns": 1,
            "used_tokens": 0,
            "limit_tokens": 8192,
            "breakdown": {
                "system_prompt": 0,
                "tool_definitions": 0,
                "memory_notes": 0,
                "conversation": 0,
            },
        },
        "sources": [],
        "stats": {
            "day": today,
            "provider": "ollama-local" if base["clearance"] != "public" else "nvidia-endpoints",
            "model": "qwen3.5:9b" if base["clearance"] != "public" else "nemotron",
            "calls": base["calls_today"],
            "tokens_thousands": 0,
            "avg_latency_seconds": 0.0,
            "blocked_calls": 0,
            "last_7_days": [],
        },
    }


def create_inbox_reference_app(
    state: InboxReferenceState | None = None, *, fixture: Path = FIXTURE
) -> FastAPI:
    reference = state or InboxReferenceState.from_fixture(fixture)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.inbox = reference
        yield

    app = FastAPI(
        title="RFA 결재 인박스 API (reference)",
        version=INBOX_CONTRACT_VERSION,
        summary="Contract for the 결재 인박스 UI PoC; served from synthetic PoC data",
        description=(
            "Derived from the UI PoC (A · 메일형 3단 + 관리자). Each operation's "
            "x-rfa-authority names the real owner of the data: rfa_module.review (승희 결재 "
            "원본), runtime.sandbox (다영 NemoClaw/OpenShell), core (this repository) or "
            "reference (PoC fixture only). Nothing here approves, publishes or changes a "
            "sandbox."
        ),
        lifespan=lifespan,
    )
    app.state.inbox = reference

    @app.exception_handler(InboxReferenceError)
    async def handle_reference_error(request: Request, exc: InboxReferenceError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.error.model_dump(mode="json"))

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "detail": [{"loc": ["body"], "msg": "Invalid request", "type": "value_error"}]
            },
        )

    error_responses = {404: {"model": InboxError}, 409: {"model": InboxError}}

    @app.get("/healthz", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": SERVICE_NAME, "mode": "reference"}

    # ---- rail --------------------------------------------------------------------------
    @app.get(
        "/v1/inbox/me",
        response_model=InboxUser,
        tags=["rail"],
        operation_id="getCurrentApprover",
        openapi_extra=authority("core"),
        summary="현재 결재자와 샌드박스 실행 수 (좌측 하단)",
    )
    async def me() -> InboxUser:
        running = sum(1 for s in reference.sandboxes.values() if s["status"] == "running")
        return reference.user.model_copy(
            update={"sandboxes_running": running, "sandboxes_total": len(reference.sandboxes)}
        )

    @app.get(
        "/v1/inbox/inboxes",
        response_model=list[InboxSummary],
        tags=["rail"],
        operation_id="listInboxes",
        openapi_extra=authority("core"),
        summary="인박스 목록 (채널·담당 에이전트·기밀 등급·대기 건수)",
    )
    async def inboxes() -> list[InboxSummary]:
        return reference.inbox_summaries()

    @app.get(
        "/v1/inbox/counts",
        response_model=InboxCounts,
        tags=["rail"],
        operation_id="getCounts",
        openapi_extra=authority("core"),
        summary="결재함/알림함/모든 인박스 배지",
    )
    async def counts() -> InboxCounts:
        return reference.counts()

    # ---- requests ----------------------------------------------------------------------
    @app.get(
        "/v1/inbox/requests",
        response_model=RequestPage,
        tags=["requests"],
        operation_id="listRequests",
        openapi_extra=authority("rfa_module.review"),
        summary="요청 목록 (전체/결재 필요/자동응답 탭, 검색 Ctrl K, 정렬)",
    )
    async def list_requests(
        inbox_id: Annotated[str | None, Query(max_length=160)] = None,
        status: Annotated[
            Literal[
                "open",
                "needs_approval",
                "blocked",
                "auto_replied",
                "decided",
                "declined",
                "held",
                "in_progress",
                "needs_human",
            ]
            | None,
            Query(),
        ] = None,
        q: Annotated[str | None, Query(max_length=200)] = None,
        sort: Annotated[Literal["received_desc", "received_asc"], Query()] = "received_desc",
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> RequestPage:
        return reference.request_page(
            inbox_id=inbox_id, status=status, query=q, sort=sort, limit=limit
        )

    @app.get(
        "/v1/inbox/requests/{request_id}",
        response_model=RequestDetail,
        tags=["requests"],
        operation_id="getRequest",
        openapi_extra=authority("rfa_module.review"),
        responses=error_responses,
        summary="요청 상세: 원본 화면·결재 카드·차단 시도·알림",
    )
    async def get_request(request_id: str) -> RequestDetail:
        return reference.request(request_id)

    @app.post(
        "/v1/inbox/requests/{request_id}/decision",
        response_model=DecisionReceipt,
        tags=["decisions"],
        operation_id="submitDecision",
        openapi_extra=authority("rfa_module.review"),
        responses={**error_responses, 422: {"model": InboxError}},
        summary="사람 결정: 범위 선택·응답하지 않기·보류·차단 처리 (+규칙 저장)",
        description=(
            "Mapping to RFA_module review: the recommended/current-draft option "
            "→ POST /reviews/{id}/approve; another option or decline → POST "
            "/reviews/{id}/reject with the chosen scope as reason (rewrite loop "
            "is 승희 Step 12+). research_direction cards resolve in this core "
            "(team run resume). Stale approval_id → 409 stale_approval."
        ),
    )
    async def submit_decision(request_id: str, body: DecisionSubmit) -> DecisionReceipt:
        async with reference.lock:
            return reference.decide(request_id, body, decided_by=reference.user.user_id)

    @app.get(
        "/v1/inbox/requests/{request_id}/preview",
        response_model=ReplyPreview,
        tags=["decisions"],
        operation_id="previewReply",
        openapi_extra=authority("core"),
        responses={404: {"model": InboxError}},
        summary="응답 전문 미리보기 (선택한 범위 기준, 전송 아님)",
    )
    async def preview(request_id: str, option_id: Annotated[str, Query(max_length=160)]):
        return reference.preview(request_id, option_id)

    # ---- notifications / activity / rules ------------------------------------------------
    @app.get(
        "/v1/inbox/notifications",
        response_model=list[Notification],
        tags=["notifications"],
        operation_id="listNotifications",
        openapi_extra=authority("core"),
        summary="알림함",
    )
    async def notifications(unread_only: bool = False) -> list[Notification]:
        rows = sorted(reference.notifications.values(), key=lambda n: n.at, reverse=True)
        return [n for n in rows if not unread_only or not n.read]

    @app.post(
        "/v1/inbox/notifications/{notification_id}/read",
        response_model=Notification,
        tags=["notifications"],
        operation_id="markNotificationRead",
        openapi_extra=authority("core"),
        responses={404: {"model": InboxError}},
    )
    async def mark_read(notification_id: str) -> Notification:
        async with reference.lock:
            current = reference.notifications.get(notification_id)
            if current is None:
                raise _not_found("notification")
            updated = current.model_copy(update={"read": True})
            reference.notifications[notification_id] = updated
            return updated

    @app.get(
        "/v1/inbox/activity",
        response_model=list[ActivityEvent],
        tags=["activity"],
        operation_id="listActivity",
        openapi_extra=authority("runtime.sandbox"),
        summary="활동 기록 (샌드박스가 막은 호출 포함)",
    )
    async def activity(
        request_id: Annotated[str | None, Query(max_length=160)] = None,
        agent_id: Annotated[str | None, Query(max_length=160)] = None,
        outcome: Annotated[Literal["blocked", "allowed", "info"] | None, Query()] = None,
    ) -> list[ActivityEvent]:
        rows = reference.activity
        if request_id is not None:
            rows = [a for a in rows if a.request_id == request_id]
        if agent_id is not None:
            rows = [a for a in rows if a.agent_id == agent_id]
        if outcome is not None:
            rows = [a for a in rows if a.outcome == outcome]
        return sorted(rows, key=lambda a: a.at, reverse=True)

    @app.get(
        "/v1/inbox/rules",
        response_model=list[Rule],
        tags=["rules"],
        operation_id="listRules",
        openapi_extra=authority("core"),
        summary="결정에서 만든 자동응답 규칙",
    )
    async def rules() -> list[Rule]:
        return sorted(reference.rules.values(), key=lambda r: r.created_at, reverse=True)

    @app.delete(
        "/v1/inbox/rules/{rule_id}",
        status_code=204,
        tags=["rules"],
        operation_id="deleteRule",
        openapi_extra=authority("core"),
        responses={404: {"model": InboxError}},
    )
    async def delete_rule(rule_id: str) -> Response:
        async with reference.lock:
            if reference.rules.pop(rule_id, None) is None:
                raise _not_found("rule")
        return Response(status_code=204)

    # ---- admin · agents ------------------------------------------------------------------
    @app.get(
        "/v1/inbox/admin/agents",
        response_model=list[AdminAgent],
        tags=["admin-agents"],
        operation_id="listAgents",
        openapi_extra=authority("runtime.sandbox"),
        summary="에이전트 목록 (인박스·샌드박스·등급·상태·오늘 호출)",
    )
    async def agents() -> list[AdminAgent]:
        return [AdminAgent.model_validate(a) for a in reference.agents.values()]

    @app.get(
        "/v1/inbox/admin/agents/{agent_id}",
        response_model=AdminAgentDetail,
        tags=["admin-agents"],
        operation_id="getAgent",
        openapi_extra=authority("runtime.sandbox"),
        responses={404: {"model": InboxError}},
        summary="에이전트 상세: 컨텍스트 사용량·불러오는 자료·호출 통계",
    )
    async def agent(agent_id: str) -> AdminAgentDetail:
        return reference.agent(agent_id)

    @app.put(
        "/v1/inbox/admin/agents/{agent_id}/sources/{source_id}",
        response_model=AdminAgentDetail,
        tags=["admin-agents"],
        operation_id="toggleAgentSource",
        openapi_extra=authority("core"),
        responses=error_responses,
        summary="불러오는 자료 켜기/끄기 (다른 구획은 409)",
    )
    async def toggle_source(agent_id: str, source_id: str, body: AgentSourceToggle):
        async with reference.lock:
            return reference.toggle_source(agent_id, source_id, body.enabled)

    @app.post(
        "/v1/inbox/admin/agents/{agent_id}/compact",
        response_model=AdminAgentDetail,
        tags=["admin-agents"],
        operation_id="compactAgentConversation",
        openapi_extra=authority("runtime.sandbox"),
        responses={404: {"model": InboxError}},
        summary="대화 압축",
    )
    async def compact(agent_id: str) -> AdminAgentDetail:
        async with reference.lock:
            return reference.compact(agent_id, clear=False)

    @app.post(
        "/v1/inbox/admin/agents/{agent_id}/clear-memory",
        response_model=AdminAgentDetail,
        tags=["admin-agents"],
        operation_id="clearAgentMemory",
        openapi_extra=authority("runtime.sandbox"),
        responses={404: {"model": InboxError}},
        summary="기억 비우기",
    )
    async def clear_memory(agent_id: str) -> AdminAgentDetail:
        async with reference.lock:
            return reference.compact(agent_id, clear=True)

    # ---- admin · sandboxes ---------------------------------------------------------------
    @app.get(
        "/v1/inbox/admin/sandboxes",
        response_model=list[Sandbox],
        tags=["admin-sandboxes"],
        operation_id="listSandboxes",
        openapi_extra=authority("runtime.sandbox"),
        summary="샌드박스 목록 (기밀 등급마다 하나)",
    )
    async def sandboxes() -> list[Sandbox]:
        return reference.sandbox_list()

    @app.get(
        "/v1/inbox/admin/sandboxes/{sandbox_id}",
        response_model=SandboxDetail,
        tags=["admin-sandboxes"],
        operation_id="getSandbox",
        openapi_extra=authority("runtime.sandbox"),
        responses={404: {"model": InboxError}},
        summary="샌드박스 상세: LLM 추론·게이트웨이·네트워크 정책",
    )
    async def sandbox(sandbox_id: str) -> SandboxDetail:
        return reference.sandbox(sandbox_id)

    @app.patch(
        "/v1/inbox/admin/sandboxes/{sandbox_id}",
        response_model=SandboxDetail,
        tags=["admin-sandboxes"],
        operation_id="updateSandboxSettings",
        openapi_extra=authority("runtime.sandbox"),
        responses={**error_responses, 422: {"model": InboxError}},
        summary="설정 변경 (저장만; '변경 적용' 전까지 pending_changes)",
    )
    async def patch_sandbox(sandbox_id: str, body: SandboxSettingsPatch) -> SandboxDetail:
        async with reference.lock:
            return reference.patch_sandbox(sandbox_id, body)

    @app.post(
        "/v1/inbox/admin/sandboxes/{sandbox_id}/apply",
        response_model=ApplyResult,
        tags=["admin-sandboxes"],
        operation_id="applySandboxSettings",
        openapi_extra=authority("runtime.sandbox"),
        responses={404: {"model": InboxError}},
        summary="변경 적용 (컨텍스트 길이·게이트웨이는 다시 만들 때)",
    )
    async def apply_sandbox(sandbox_id: str) -> ApplyResult:
        async with reference.lock:
            return reference.apply_sandbox(sandbox_id)

    return app
