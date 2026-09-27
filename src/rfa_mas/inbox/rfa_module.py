"""Map 승희's RFA_module `Review` documents onto the `/v1/inbox` contract.

Pure functions over the review JSON (`fixtures/contracts/rfa_module/review.openapi.yaml`,
pinned at 819053f). No HTTP here: a later adapter fetches `GET /reviews` and calls
`POST /reviews/{id}/approve|reject` from the host loopback, exactly as the review service
requires. The mapping is lossless for what the UI shows and never invents scope options
that the review service cannot honour yet (3-level scopes need 승희's rewrite loop).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from rfa_mas.inbox.contract import (
    ChannelKind,
    DecisionCard,
    DecisionOption,
    DecisionRecord,
    DecisionSubmit,
    OriginalMessage,
    OriginalView,
    ReplyPreview,
    RequestAlert,
    RequestDetail,
    Requester,
    RequestStatus,
    SourceRef,
)

# RFA_module ReviewStatus → inbox RequestStatus. Only `reviewed` waits for a human.
STATUS_MAP: dict[str, RequestStatus] = {
    "opened": "in_progress",
    "knowledge_ready": "in_progress",
    "drafted": "in_progress",
    "scanned": "in_progress",
    "reviewed": "needs_approval",
    "approved": "decided",
    "posted": "decided",
    "rejected": "declined",
    "needs_human": "needs_human",
}
STAGE_LABEL = {
    "opened": "접수됨 · 실무대장에게 질문 중",
    "knowledge_ready": "지식 확보 · 초안 작성 중",
    "drafted": "초안 작성됨 · 스캔 대기",
    "scanned": "스캔 완료 · 기밀 검토 중",
    "approved": "승인됨 · 게시 중",
    "posted": "게시됨",
}
DEFAULT_INBOXES: dict[str, tuple[str, ChannelKind]] = {
    "public": ("github-issues", "github_issue"),
    "internal": ("slack-inquiries", "slack_dm"),
}
OPTION_AS_REVIEWED = "as-reviewed"
OPTION_ORIGINAL = "original-draft"


def review_request_id(review_id: int) -> str:
    return f"review-{review_id}"


def review_to_request(
    review: dict[str, Any],
    *,
    inboxes: dict[str, tuple[str, ChannelKind]] | None = None,
) -> RequestDetail:
    """Project one RFA_module Review onto RequestDetail (UI request + decision card)."""
    inbox_id, channel_kind = (inboxes or DEFAULT_INBOXES)[review["channel"]]
    review_id = int(review["id"])
    status = STATUS_MAP[review["status"]]
    events = review.get("events") or []
    received_at = _at(events[0]["at"]) if events else datetime.now(UTC)
    requester = review["requester"]
    title = review["question"].strip().splitlines()[0][:120]
    verdict = review.get("verdict")
    decision = review.get("decision")
    card = _decision_card(review, review_id, verdict) if status == "needs_approval" else None
    outcome = None
    if decision is not None and review["status"] in {"approved", "posted", "rejected"}:
        outcome = DecisionRecord(
            action="respond" if review["status"] != "rejected" else "decline",
            option_id=OPTION_AS_REVIEWED if review["status"] != "rejected" else None,
            decided_by=decision["by"],
            decided_at=_at(decision["at"]),
            note=decision.get("reason"),
        )
    alerts: list[RequestAlert] = []
    if review["status"] == "needs_human":
        detail = next(
            (e.get("detail") for e in reversed(events) if e.get("what") == "needs_human"), None
        )
        alerts.append(
            RequestAlert(
                kind="info",
                message=detail or "자동 처리 불가. 사람에게 넘겼습니다.",
                at=_at(events[-1]["at"]) if events else received_at,
            )
        )
    source = SourceRef(kind=channel_kind, label=review["target"], url=review["source_url"])
    return RequestDetail(
        request_id=review_request_id(review_id),
        inbox_id=inbox_id,
        requester=Requester(
            requester_id=requester,
            display_name=requester,
            initials=requester[:2].upper(),
            kind="external" if review["channel"] == "public" else "internal",
        ),
        title=title,
        subtitle=_subtitle(review, review_id),
        status=status,
        approval_id=review_request_id(review_id),
        received_at=received_at,
        is_new=review["status"] == "reviewed",
        source=source,
        received_via=(
            "GitHub 이슈로 들어옴" if channel_kind == "github_issue" else "사내 채널로 들어옴"
        ),
        original=OriginalView(
            source=source,
            header=review["target"],
            messages=[OriginalMessage(author=requester, at=received_at, body=review["question"])],
            reply_placeholder=(
                "에이전트의 답글은 결재가 끝나면 이 자리에 등록됩니다."
                if status in {"in_progress", "needs_approval"}
                else None
            ),
        ),
        decision=card,
        outcome=outcome,
        alerts=alerts,
        policy_link_label=f"{review['channel']} 기밀 기준 보기",
    )


def _decision_card(review: dict[str, Any], review_id: int, verdict: dict | None) -> DecisionCard:
    verdict_kind = (verdict or {}).get("verdict", "allow")
    reasons = (verdict or {}).get("reasons") or []
    excluded = sorted({r["rule"] for r in reasons if r.get("action") in {"remove", "blur"}})
    options: list[DecisionOption] = []
    if verdict_kind != "block":
        options.append(
            DecisionOption(
                option_id=OPTION_AS_REVIEWED,
                label="게시본 그대로",
                description=(
                    "기밀 검토가 수정한 본문을 게시합니다."
                    if verdict_kind == "redact"
                    else "기밀 검토를 통과한 초안을 게시합니다."
                ),
                recommended=True,
                disclosure_level=1,
            )
        )
        if verdict_kind == "redact" and review.get("draft"):
            options.append(
                DecisionOption(
                    option_id=OPTION_ORIGINAL,
                    label="원본 초안까지",
                    description=(
                        "기밀 검토 전 초안을 그대로 게시합니다. 거절 후 재작성 루프가 필요합니다."
                    ),
                    badge="기밀 검토 전",
                    disclosure_level=3,
                )
            )
    return DecisionCard(
        approval_id=review_request_id(review_id),
        asked_by="censor-public" if review["channel"] == "public" else "censor-internal",
        kind="review_body",
        question=(
            "게시할 수 없는 초안입니다. 어떻게 할까요?"
            if verdict_kind == "block"
            else "게시본을 이대로 승인할까요?"
        ),
        note=(verdict or {}).get("summary"),
        always_excluded=excluded,
        options=options,
        primary_label="이대로 게시" if verdict_kind != "block" else "재작성 요청",
        secondary_label="거절",
        secondary_action="decline",
        allow_rule=False,
        preview_available=bool(review.get("final_body") or review.get("draft")),
        evidence_link_label="원본↔게시본 비교",
        effect_note=("승인하면 서명 토큰이 발급되고 호스트가 게시합니다 (RFA_module review)."),
    )


def _subtitle(review: dict[str, Any], review_id: int) -> str:
    status = review["status"]
    if status == "reviewed":
        verdict = (review.get("verdict") or {}).get("verdict", "allow")
        return f"결재 {review_id} · " + (
            "게시할 수 없는 초안" if verdict == "block" else "게시본을 승인할까요?"
        )
    if status == "rejected":
        reason = (review.get("decision") or {}).get("reason") or "사유 없음"
        return f"결재 {review_id} · 거절 · {reason}"
    if status == "needs_human":
        return f"결재 {review_id} · 사람 판단 필요"
    return f"결재 {review_id} · {STAGE_LABEL.get(status, status)}"


def review_preview(review: dict[str, Any], option_id: str) -> ReplyPreview:
    body = review.get("final_body") if option_id == OPTION_AS_REVIEWED else review.get("draft")
    if not body:
        raise KeyError(option_id)
    reasons = (review.get("verdict") or {}).get("reasons") or []
    return ReplyPreview(
        request_id=review_request_id(int(review["id"])),
        option_id=option_id,
        body=body,
        excluded=sorted({r["rule"] for r in reasons if r.get("action") in {"remove", "blur"}}),
        generated_at=datetime.now(UTC),
        simulated=False,
    )


ReviewCall = tuple[Literal["approve", "reject"], dict[str, Any] | None]


def to_review_call(submit: DecisionSubmit) -> ReviewCall:
    """Translate a UI decision into the loopback-only review call the host must make.

    `respond` with the reviewed body → approve. Anything else is a reject whose reason
    carries the human's intent (scope choice / hold / decline) for 승희's rewrite loop.
    """
    if submit.action == "respond" and submit.option_id == OPTION_AS_REVIEWED:
        return "approve", None
    if submit.action == "respond":
        return "reject", {"reason": f"scope:{submit.option_id}" + _note(submit)}
    if submit.action == "hold":
        return "reject", {"reason": "hold" + _note(submit)}
    return "reject", {"reason": submit.action + _note(submit)}


def _note(submit: DecisionSubmit) -> str:
    return f" — {submit.note}" if submit.note else ""


def _at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
