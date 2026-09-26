"""Work candidates (P1-004B): discovery, dedup and owner decisions.

Discovery reads only the owner's current authorized sources through the P1-004A extractor.
It never creates a Task or team; accepting a candidate records the decision only (automatic
Task creation is disabled in P0). A rejected candidate resurfaces only when one of its
parent sources has a new revision and the owner allowed resurfacing.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

from rfa_mas.contracts import (
    CandidateDecision,
    DomainId,
    TodoCandidate,
    TrustedPrincipal,
    new_id,
    sha256_text,
)
from rfa_mas.errors import ResourceNotFoundError, RfaError

ISSUE_REF = re.compile(r"(?:issue\s*)?#(\d+)", re.IGNORECASE)
DATE_ISO = re.compile(r"(20\d\d)-(\d\d)-(\d\d)")
DATE_KO = re.compile(r"(\d{1,2})월\s*(\d{1,2})일")
BLOCKER_TERMS = ("확인 필요", "선행", "blocker", "막힘", "blocked", "전에 확인")
CLOSED_TERMS = ("closed", "완료", "해결됨", "resolved", "done")
TENTATIVE_TERMS = ("가설", "검증 전", "미검증", "잠정", "추정", "tentative", "unverified")


def _normalized(text: str) -> str:
    return " ".join(text.split()).lower()


def explicit_due(text: str, year: int) -> str | None:
    """Only a date literally present in the text; no inferred deadlines."""
    if match := DATE_ISO.search(text):
        return "-".join(match.groups())
    if (match := DATE_KO.search(text)) and ("마감" in text or "까지" in text or "due" in text):
        month, day = int(match.group(1)), int(match.group(2))
        if 1 <= month <= 12 and 1 <= day <= 31:
            return f"{year:04d}-{month:02d}-{day:02d}"
    return None


def fingerprint(kind: str, content: str, parent_ids: list[str]) -> str:
    """Same change -> same key. An issue reference dedups mentions across sources."""
    issue = ISSUE_REF.search(content)
    key = ["issue", issue.group(1)] if issue else [kind, _normalized(content), sorted(parent_ids)]
    return sha256_text(json.dumps(key, ensure_ascii=False))


class CandidateService:
    def __init__(self, repository, accumulator, *, clock=lambda: datetime.now(UTC)):
        self.repository, self.accumulator, self.clock = repository, accumulator, clock

    async def discover(self, domain_id: DomainId, principal: TrustedPrincipal):
        now = self.clock()
        proposals = await self.accumulator.extract(domain_id, principal, origin_ref="candidates")
        existing = {c.fingerprint: c for c in
                    await self.repository.list_candidates(principal, domain_id)}
        # Group mentions of the same change first; keep the richest statement (explicit
        # due date, then longest text) and the union of parents, independent of order.
        grouped: dict[str, list] = {}
        for proposal in proposals:
            if proposal.kind in {"todo", "issue"}:
                key = fingerprint(proposal.kind, proposal.content,
                                  [p.source_id for p in proposal.parents])
                grouped.setdefault(key, []).append(proposal)
        merged = []
        for key, group in sorted(grouped.items()):
            best = sorted(group, key=lambda p: (explicit_due(p.content, now.year) is None,
                                                -len(p.content), p.content))[0]
            parents = {(p.source_id, p.source_revision): p for g in group for p in g.parents}
            merged.append(best.model_copy(update={
                "parents": tuple(parents[k] for k in sorted(parents))[:32],
                "content": best.content,
            }))
        seen: set[str] = set()
        for proposal in merged:
            text = proposal.content
            closed = any(term in _normalized(text) for term in CLOSED_TERMS)
            key = fingerprint(proposal.kind, text, [p.source_id for p in proposal.parents])
            if key in seen:
                continue  # Same change mentioned twice in one discovery.
            seen.add(key)
            current = existing.get(key)
            revisions = {(p.source_id, p.source_revision) for p in proposal.parents}
            if current is None:
                if closed:
                    continue
                await self.repository.upsert_candidate(TodoCandidate(
                    candidate_id=new_id("candidate"), domain_id=domain_id,
                    owner_id=principal.user_id, kind=proposal.kind,
                    title=proposal.title, content=text, fingerprint=key,
                    parents=proposal.parents,
                    epistemic_state="tentative" if any(
                        term in _normalized(text) for term in TENTATIVE_TERMS
                    ) else proposal.epistemic_state,
                    due_date=explicit_due(text, now.year),
                    blocker=any(t in _normalized(text) for t in BLOCKER_TERMS),
                    history=(f"{now.isoformat()} proposed",), created_at=now, updated_at=now,
                ), principal)
                continue
            new_evidence = revisions != {(p.source_id, p.source_revision)
                                         for p in current.parents}
            update = None
            if closed and current.state != "superseded":
                update = {"state": "superseded", "history": current.history
                          + (f"{now.isoformat()} superseded:closed",)}
            elif (current.state == "rejected" and new_evidence
                  and current.resurface_on_new_evidence):
                update = {"state": "proposed", "history": current.history
                          + (f"{now.isoformat()} resurfaced:new_revision",)}
            if update is not None or new_evidence:
                changed = current.model_copy(update=(update or {}) | {
                    "parents": proposal.parents, "content": text,
                    "due_date": explicit_due(text, now.year), "updated_at": now,
                })
                await self.repository.upsert_candidate(changed, principal,
                                                       expected_state=current.state)
        # A candidate whose source line disappeared from the current revision is stale.
        for key, current in existing.items():
            if key not in seen and current.state in {"proposed", "deferred"}:
                current_revisions = await self._current_parents(domain_id, principal, current)
                if not current_revisions:
                    await self.repository.upsert_candidate(current.model_copy(update={
                        "state": "superseded", "updated_at": now,
                        "history": current.history + (f"{now.isoformat()} superseded:source",),
                    }), principal, expected_state=current.state)
        return await self.list(domain_id, principal)

    async def _current_parents(self, domain_id, principal, candidate) -> bool:
        try:
            await self.repository.read_sources(domain_id, principal, candidate.parents,
                                               metadata_only=True)
            return True
        except RfaError:
            return False

    async def list(self, domain_id: DomainId | None, principal: TrustedPrincipal,
                   *, include_hidden: bool = False):
        candidates = await self.repository.list_candidates(principal, domain_id)
        if include_hidden:
            return candidates
        return [c for c in candidates if c.state in {"proposed", "deferred", "accepted"}]

    async def ranked(self, domain_id: DomainId | None, principal: TrustedPrincipal):
        """Recommendation order only; ranking never changes candidate state or authority."""
        return rank(await self.list(domain_id, principal), now=self.clock())

    async def decide(self, candidate_id: str, decision: CandidateDecision,
                     principal: TrustedPrincipal) -> TodoCandidate:
        decision = CandidateDecision.model_validate(decision.model_dump())
        current = next((c for c in await self.repository.list_candidates(principal)
                        if c.candidate_id == candidate_id), None)
        if current is None:
            raise ResourceNotFoundError("candidate")
        if current.state == "superseded":
            raise RfaError("invalid_state_transition", "대체된 후보는 결정할 수 없습니다.")
        now = self.clock()
        state = {"accept": "accepted", "defer": "deferred", "reject": "rejected"}[decision.decision]
        # Acceptance records intent only. Creating a Task/team is a separate, explicit
        # assistant/team request; P0 never auto-creates one from a candidate.
        return await self.repository.upsert_candidate(current.model_copy(update={
            "state": state, "decided_reason": decision.reason or None,
            "resurface_on_new_evidence": decision.resurface_on_new_evidence,
            "updated_at": now, "history": current.history + (f"{now.isoformat()} {state}",),
        }), principal, expected_state=current.state)

# -- P2-003 explainable default ranking ----------------------------------------------------
# Fixed, documented rule weights (rule_version candidate-rank-v1). Missing inputs score 0
# with an explicit reason; no deadline or impact is ever invented.
RANK_RULES = {
    "overdue": 60, "due_within_3d": 45, "due_within_7d": 30, "due_later": 10,
    "blocker": 25, "impact_release": 15, "cited": 10, "tentative": -20, "inferred": -5,
    "accepted": 5, "deferred": -10,
}
IMPACT_TERMS = ("출시", "release", "공개", "sdk", "고객")


def rank(candidates, *, now: datetime, zone: str = "Asia/Seoul"):
    from zoneinfo import ZoneInfo

    from rfa_mas.contracts import RankedCandidate, RankReason

    today = now.astimezone(ZoneInfo(zone)).date()
    scored = []
    for candidate in candidates:
        reasons = []
        if candidate.due_date:
            due = datetime.fromisoformat(candidate.due_date).date()
            days = (due - today).days
            key = ("overdue" if days < 0 else "due_within_3d" if days <= 3
                   else "due_within_7d" if days <= 7 else "due_later")
            reasons.append(RankReason(factor="due", points=RANK_RULES[key],
                                      explanation=f"원문 마감 {candidate.due_date} (D{days:+d})"))
        else:
            reasons.append(RankReason(factor="due", points=0, explanation="원문에 마감 정보 없음"))
        if candidate.blocker:
            reasons.append(RankReason(factor="dependency", points=RANK_RULES["blocker"],
                                      explanation="선행 확인이 필요한 미완료 항목(의존성 미충족)"))
        text = candidate.content.lower()
        if any(term in text for term in IMPACT_TERMS):
            reasons.append(RankReason(factor="impact", points=RANK_RULES["impact_release"],
                                      explanation="출시/공개 관련 원문 표현"))
        certainty = RANK_RULES.get(candidate.epistemic_state, 0)
        reasons.append(RankReason(
            factor="certainty", points=certainty,
            explanation={"cited": "원문 인용 근거", "tentative": "검증 전/불확실 근거",
                         "inferred": "추론된 항목"}.get(candidate.epistemic_state,
                                                      candidate.epistemic_state),
        ))
        if candidate.state in {"accepted", "deferred"}:
            reasons.append(RankReason(factor="state", points=RANK_RULES[candidate.state],
                                      explanation=f"사용자 결정: {candidate.state}"))
        score = sum(r.points for r in reasons)
        scored.append((score, candidate, tuple(reasons)))
    # Stable tie-break: score desc, earlier due, earlier creation, candidate id.
    scored.sort(key=lambda item: (-item[0], item[1].due_date or "9999-12-31",
                                  item[1].created_at, item[1].candidate_id))
    return [RankedCandidate(rank=i, score=s, candidate=c, reasons=r)
            for i, (s, c, r) in enumerate(scored, 1)]
