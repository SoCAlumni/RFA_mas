"""P1-005B: owner feedback memory in four categories, with scope and revocation.

- The user dimension of every scope is the authenticated caller; nothing names another user.
- Style preferences are advisory model guidance for in-scope drafts only.
- Personal disclosure preferences are withhold markers: they can only narrow what a
  non-owner draft may carry. There is no representation that widens sharing.
- A factual correction is stored as a tentative user assertion pending evidence review. It
  is never applied as a fact or written to the KB here.
- An official-policy change is stored as a proposal only. Nothing here changes policy or
  its version; relaxation language is always routed to a proposal, whatever its label.
- Revocation is a new revision. Selection and application logging share one transaction,
  so a committed revocation is never applied afterwards.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from rfa_mas.contracts import (
    Audience,
    DomainId,
    DraftTarget,
    FeedbackApplication,
    FeedbackCategory,
    FeedbackClassification,
    FeedbackCreate,
    FeedbackRecord,
    FeedbackRevoke,
    TrustedPrincipal,
    new_id,
)
from rfa_mas.contracts.models import FEEDBACK_DISPOSITION
from rfa_mas.errors import ResourceNotFoundError, RfaError

# Deterministic local rules (Korean + English). Order is a safety order: a relaxation
# request wins over everything, then narrowing, then factual correction, then style.
_POLICY_RELAXATION = re.compile(
    r"(?:공개|공유|전송|반출|전달)해도\s*(?:돼|된다|됨|괜찮)"
    r"|(?:정책|규칙|기밀\s*등급|보안\s*등급|권한|공유\s*범위)(?:을|를)?\s*"
    r"(?:바꿔|바꾸|변경|완화|풀어|낮춰|해제|넓혀)"
    r"|(?:정책|규칙)\s*변경|기밀\s*해제|제한(?:을)?\s*(?:풀|없애|해제)"
    r"|(?:항상|모두|전부)\s*(?:공개|공유)(?:해|하|로)|공유(?:를)?\s*허용"
    r"|\b(?:change|relax|loosen|lower)\s+(?:the\s+)?(?:policy|rule|classification)"
    r"|\bdeclassif|\b(?:ok|okay|fine|allowed)\s+to\s+(?:share|publish|disclose)"
    r"|\balways\s+(?:share|publish)|\bignore\s+(?:the\s+)?(?:policy|confidential)",
    re.IGNORECASE,
)
_DISCLOSURE_NARROWING = re.compile(
    r"(?:공유|공개|언급|노출|전달|인용|포함)하지\s*(?:마|말|않)"
    r"|(?:밝히|알리|말하|보여주)지\s*(?:마|말)|숨겨|빼\s*(?:줘|주세요)|제외(?:해|하)|비공개로"
    r"|\b(?:don't|do\s+not|never)\s+(?:share|mention|disclose|reveal|include)"
    r"|\bkeep\s+.{1,80}\s+private|\bwithhold\b",
    re.IGNORECASE,
)
_FACTUAL = re.compile(
    r"사실(?:은|이\s*아니)|틀렸|틀린|틀림|잘못\s*(?:된|됐|되었|알고)|정정|실제로는"
    r"|(?:이|가)\s*아니라|맞지\s*않"
    r"|\bcorrection\b|\b(?:is|was|are|were)\s+(?:wrong|incorrect)\b|\bactually\b",
    re.IGNORECASE,
)
_STYLE = re.compile(
    r"문장|톤|말투|어조|문체|간결|짧게|길게|형식|포맷|표시|목록|불릿|존댓말|반말|이내|요약|강조|이모지"
    r"|표로|\b(?:tone|concise|brief|format|sentences?|bullets?|style|shorter|longer|emoji)\b",
    re.IGNORECASE,
)
_QUOTED = re.compile(r"[\"“'‘「『]([^\"”'’」』]{2,200})[\"”'’」』]")
_RULES = (
    (_POLICY_RELAXATION, FeedbackCategory.OFFICIAL_POLICY_CHANGE_PROPOSAL, "policy_relaxation"),
    (_DISCLOSURE_NARROWING, FeedbackCategory.PERSONAL_DISCLOSURE_PREFERENCE,
     "disclosure_narrowing"),
    (_FACTUAL, FeedbackCategory.FACTUAL_CORRECTION, "factual_correction"),
    (_STYLE, FeedbackCategory.STYLE_PREFERENCE, "style"),
)
_OWNER_TARGETS = frozenset({Audience.OWNER, Audience.PRIVATE})


def classify_text(text: str) -> tuple[FeedbackCategory | None, str]:
    for pattern, category, rule in _RULES:
        if pattern.search(text):
            return category, rule
    return None, "unclassified"


def normalize_markers(markers: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result = []
    for marker in markers:
        value = " ".join(marker.split())
        if len(value) >= 2 and value.casefold() not in seen:
            seen.add(value.casefold())
            result.append(value)
    return tuple(result)


def classify_feedback(body: FeedbackCreate) -> FeedbackClassification:
    """Deterministic classification preview. Raises when no safe category is known."""
    detected, rule = classify_text(body.text)
    policy = FeedbackCategory.OFFICIAL_POLICY_CHANGE_PROPOSAL
    if detected == policy and body.category != policy:
        # Relaxation language never becomes applied memory, whatever label it came with.
        category, by = policy, "rule"
        rule = "policy_relaxation_override" if body.category is not None else rule
    elif body.category is not None:
        category, by, rule = body.category, "user", "user_selected"
    elif detected is None:
        raise RfaError("feedback_unclassified", "피드백 분류를 선택해 주세요.")
    else:
        category, by = detected, "rule"
    markers: tuple[str, ...] = ()
    if category == FeedbackCategory.PERSONAL_DISCLOSURE_PREFERENCE:
        markers = normalize_markers(body.withhold_markers or _QUOTED.findall(body.text))
        if not markers:
            raise RfaError("feedback_invalid", "공유하지 않을 표현을 지정해 주세요.")
    return FeedbackClassification(
        category=category,
        classified_by=by,
        rule=rule,
        disposition=FEEDBACK_DISPOSITION[category],
        withhold_markers=markers,
    )


def in_scope(record: FeedbackRecord, *, domain_id: DomainId, target: DraftTarget) -> bool:
    """Pure scope rule (mirrors the repository's SQL selection)."""
    scope = record.scope
    return (
        record.state == "active"
        and scope.domain_id in {None, domain_id}
        and scope.target_audience in {None, target.audience}
        and scope.target_channel in {None, target.channel}
    )


class FeedbackService:
    def __init__(self, repository: Any) -> None:
        self._repository = repository

    # -- owner API -------------------------------------------------------------------------
    def classify(self, body: FeedbackCreate) -> FeedbackClassification:
        return classify_feedback(body)

    async def submit(self, body: FeedbackCreate, principal: TrustedPrincipal) -> FeedbackRecord:
        classification = classify_feedback(body)
        source = body.source
        if source.run_id is not None:
            # Provenance must be the caller's own run/draft; otherwise indistinguishable 404.
            await self._repository.get_owned_run(source.run_id, principal)
            if source.draft_id is not None:
                versions = await self._repository.draft_versions(source.run_id, principal)
                if not any(
                    draft.draft_id == source.draft_id and draft.version == source.draft_version
                    for draft, _ in versions
                ):
                    raise ResourceNotFoundError("draft")
        category = classification.category
        if category is None:  # classify_feedback raises first; defensive only.
            raise RfaError("feedback_unclassified", "피드백 분류를 선택해 주세요.")
        now = datetime.now(UTC)
        record = FeedbackRecord(
            feedback_id=new_id("feedback"),
            revision=1,
            category=category,
            classified_by=classification.classified_by,
            classification_rule=classification.rule,
            text=body.text,
            scope=body.scope,
            source=source,
            withhold_markers=classification.withhold_markers,
            disposition=FEEDBACK_DISPOSITION[category],
            epistemic_state="tentative"
            if category == FeedbackCategory.FACTUAL_CORRECTION
            else None,
            created_at=now,
            updated_at=now,
            history=(f"r1:created:{category.value}:{classification.rule}",),
        )
        return await self._repository.create_feedback(record, principal)

    async def get(self, feedback_id: str, principal: TrustedPrincipal) -> FeedbackRecord:
        return await self._repository.get_feedback(feedback_id, principal)

    async def list(
        self,
        principal: TrustedPrincipal,
        *,
        domain_id: DomainId | None = None,
        state: str | None = None,
        category: FeedbackCategory | None = None,
    ) -> list[FeedbackRecord]:
        return await self._repository.list_feedback(
            principal,
            domain_id=domain_id,
            state=state,
            category=category.value if category is not None else None,
        )

    async def revisions(
        self, feedback_id: str, principal: TrustedPrincipal
    ) -> list[FeedbackRecord]:
        return await self._repository.feedback_revisions(feedback_id, principal)

    async def revoke(
        self, feedback_id: str, body: FeedbackRevoke, principal: TrustedPrincipal
    ) -> FeedbackRecord:
        current = await self._repository.get_feedback(feedback_id, principal)
        if current.state != "active" or current.revision != body.expected_revision:
            raise RfaError("invalid_state_transition", "피드백 revision이 변경되었습니다.")
        revision = current.revision + 1
        revoked = current.model_copy(
            update={
                "revision": revision,
                "state": "revoked",
                "revoked_reason": body.reason or "revoked_by_owner",
                "updated_at": datetime.now(UTC),
                "history": (*current.history, f"r{revision}:revoked"),
            }
        )
        return await self._repository.replace_feedback(
            revoked, principal, expected_revision=current.revision
        )

    async def applications(
        self,
        principal: TrustedPrincipal,
        *,
        run_id: str | None = None,
        feedback_id: str | None = None,
    ) -> list[FeedbackApplication]:
        if run_id is not None:
            await self._repository.get_owned_run(run_id, principal)
        if feedback_id is not None:
            await self._repository.get_feedback(feedback_id, principal)
        return await self._repository.feedback_applications(
            principal, run_id=run_id, feedback_id=feedback_id
        )

    # -- domain graph hooks (injected in bootstrap) ----------------------------------------
    async def disclosure_markers(
        self,
        principal: TrustedPrincipal,
        domain_id: DomainId,
        *,
        target: DraftTarget | None = None,
        run_id: str | None = None,
    ) -> tuple[str, ...]:
        """Withhold markers for a non-owner draft. Only ever ADDS screening."""
        if target is None:
            # Unknown target: every in-domain marker (over-narrowing is the safe side).
            records = await self._repository.list_feedback(
                principal,
                domain_id=domain_id,
                state="active",
                category=FeedbackCategory.PERSONAL_DISCLOSURE_PREFERENCE.value,
            )
        elif target.audience in _OWNER_TARGETS:
            return ()  # Markers do not apply to the owner's own drafts; nothing is logged.
        else:
            records = await self._repository.apply_feedback(
                principal,
                effect="withhold_markers",
                domain_id=domain_id,
                target_audience=target.audience.value,
                target_channel=target.channel,
                run_id=run_id,
            )
        return normalize_markers([m for record in records for m in record.withhold_markers])

    async def style_guidance(
        self,
        principal: TrustedPrincipal,
        domain_id: DomainId,
        *,
        target: DraftTarget,
        run_id: str | None = None,
        keep: Callable[[str], bool] | None = None,
    ) -> tuple[str, ...]:
        """In-scope active style guidance; keep() screens texts before they are logged."""
        records = await self._repository.apply_feedback(
            principal,
            effect="style_guidance",
            domain_id=domain_id,
            target_audience=target.audience.value,
            target_channel=target.channel,
            run_id=run_id,
            keep=(lambda record: keep(record.text)) if keep is not None else None,
        )
        return tuple(record.text for record in records)

