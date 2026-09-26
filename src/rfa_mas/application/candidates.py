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
# P1-004C rule table (with knowledge.EXTRACTION_RULES_VERSION). A blocker is an open item
# other work waits on: an explicit marker or a gate ("확인 전에는 ... 쓰지 않는다").
BLOCKER_TERMS = ("확인 필요", "선행", "blocker", "막힘", "blocked", "전에 확인", "전에는",
                 "전까지는", "선결")
CLOSED_TERMS = ("closed", "완료", "해결됨", "resolved", "done")
TENTATIVE_TERMS = ("가설", "검증 전", "미검증", "잠정", "추정", "tentative", "unverified")
# An unverified hypothesis/idea with no action item in its source becomes a follow-up.
FOLLOW_UP_CUES = ("가설", "아이디어", "idea", "hypothesis")
# A mention quoting a tracked item's title (>= this many characters) refers to that item.
MENTION_MIN_CHARS = 8


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


def _due(proposal, year: int) -> str | None:
    """Due date literally present in the item or in its source's attached due fields."""
    for text in (proposal.content, *proposal.conditions):
        if found := explicit_due(text, year):
            return found
    return None


def fingerprint(kind: str, content: str, parent_ids: list[str]) -> str:
    """Same change -> same key. An issue reference dedups mentions across sources."""
    issue = ISSUE_REF.search(content)
    key = ["issue", issue.group(1)] if issue else [kind, _normalized(content), sorted(parent_ids)]
    return sha256_text(json.dumps(key, ensure_ascii=False))


def tracked_key(source_id: str) -> str:
    """Stable key of a tracked item (a source with a status field) across its revisions."""
    return sha256_text(json.dumps(["tracked", source_id]))


def _source_title(proposal) -> str:
    return _normalized(proposal.title.rsplit(" — ", 1)[0]).lower()


def _groups(proposals) -> list[list[tuple[str, object, str]]]:
    """Union same-change entries: equal key, one tracked source, or a title mention.

    Entries are (candidate_kind, proposal, key). Deterministic for a given proposal set.
    """
    actionable = [p for p in proposals if p.kind in {"todo", "issue"}]
    with_actions = {p.parents[0].source_id for p in actionable if p.kind == "todo"}
    entries = [("issue" if p.kind == "issue" else "todo", p) for p in actionable]
    entries += [("follow_up", p) for p in proposals
                if p.kind == "summary" and p.epistemic_state == "tentative"
                and p.parents[0].source_id not in with_actions
                and any(cue in p.content.lower() for cue in FOLLOW_UP_CUES)]
    keyed = [(kind, p, tracked_key(p.parents[0].source_id) if kind == "issue"
              else fingerprint(kind, p.content, [x.source_id for x in p.parents]))
             for kind, p in entries]
    parent = list(range(len(keyed)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        a, b = find(i), find(j)
        if a != b:
            parent[max(a, b)] = min(a, b)

    first: dict[str, int] = {}
    tracked: dict[str, int] = {}
    for index, (kind, proposal, key) in enumerate(keyed):
        union(index, first.setdefault(key, index))
        if kind == "issue":
            tracked[proposal.parents[0].source_id] = index
    titles = {source: _source_title(keyed[i][1]) for source, i in tracked.items()}
    for index, (kind, proposal, _) in enumerate(keyed):
        source = proposal.parents[0].source_id
        if kind == "issue":
            continue
        if source in tracked:
            union(index, tracked[source])  # an action of the tracked item itself
        text = _normalized(proposal.content).lower()
        for other, title in sorted(titles.items()):
            if other != source and len(title) >= MENTION_MIN_CHARS and title in text:
                union(index, tracked[other])  # a mention quoting the item's title
    grouped: dict[int, list] = {}
    for index, entry in enumerate(keyed):
        grouped.setdefault(find(index), []).append(entry)
    return [grouped[root] for root in sorted(grouped)]


class CandidateService:
    def __init__(self, repository, accumulator, *, clock=lambda: datetime.now(UTC)):
        self.repository, self.accumulator, self.clock = repository, accumulator, clock

    async def discover(self, domain_id: DomainId, principal: TrustedPrincipal):
        now = self.clock()
        proposals = await self.accumulator.extract(domain_id, principal, origin_ref="candidates")
        existing = {c.fingerprint: c for c in
                    await self.repository.list_candidates(principal, domain_id)}
        seen: set[str] = set()
        order = {"accepted": 0, "rejected": 1, "deferred": 2, "proposed": 3, "superseded": 4}
        for group in _groups(proposals):
            keys = sorted({key for _, _, key in group})
            tracked = sorted({p.parents[0].source_id for kind, p, _ in group if kind == "issue"})
            statuses = [p.content for kind, p, _ in group if kind == "issue"]
            # Continuity: reuse a stored candidate of any member key (decided ones first),
            # so a later mention or a new revision never forks a second candidate.
            matches = sorted((k for k in keys if k in existing),
                             key=lambda k: (order[existing[k].state], k))
            key = matches[0] if matches else (tracked_key(tracked[0]) if tracked else keys[0])
            seen.update(keys)
            for duplicate in matches[1:]:
                other = existing[duplicate]
                if other.state in {"proposed", "deferred"}:
                    await self.repository.upsert_candidate(other.model_copy(update={
                        "state": "superseded", "updated_at": now,
                        "history": other.history + (f"{now.isoformat()} superseded:duplicate",),
                    }), principal, expected_state=other.state)
            content = [(kind, p) for kind, p, _ in group if kind != "issue"]
            best_kind, best = sorted(content, key=lambda entry: (
                bool(tracked) and entry[1].parents[0].source_id not in tracked,  # mentions last
                entry[0] == "follow_up",
                _due(entry[1], now.year) is None,
                -len(entry[1].content),
                entry[1].content,
            ))[0] if content else (None, None)
            text = best.content if best is not None else ""
            # A tracked item's own status field decides closure; otherwise the text does.
            closed = (any(term in _normalized(s) for s in statuses for term in CLOSED_TERMS)
                      if statuses else any(term in _normalized(text) for term in CLOSED_TERMS))
            current = existing.get(key)
            if best is None:
                if current is not None and closed and current.state != "superseded":
                    await self.repository.upsert_candidate(current.model_copy(update={
                        "state": "superseded", "updated_at": now,
                        "history": current.history + (f"{now.isoformat()} superseded:closed",),
                    }), principal, expected_state=current.state)
                continue
            parents_by_revision = {(ref.source_id, ref.source_revision): ref
                                   for _, p, _ in group for ref in p.parents}
            parents = tuple(parents_by_revision[k] for k in sorted(parents_by_revision))[:32]
            due = next((d for _, p in sorted(content, key=lambda e: e[1].content)
                        if (d := _due(p, now.year))), None)
            due = _due(best, now.year) or due
            evidence = [_normalized(x).lower() for _, p, _ in group
                        for x in (p.content, *p.conditions)]
            blocker = any(term in x for x in evidence for term in BLOCKER_TERMS)
            kind = "todo" if best_kind != "follow_up" else "follow_up"
            revisions = set(parents_by_revision)
            if current is None:
                if closed:
                    continue
                await self.repository.upsert_candidate(TodoCandidate(
                    candidate_id=new_id("candidate"), domain_id=domain_id,
                    owner_id=principal.user_id, kind=kind,
                    title=best.title, content=text, fingerprint=key,
                    parents=parents,
                    epistemic_state="tentative" if any(
                        term in _normalized(text) for term in TENTATIVE_TERMS
                    ) else best.epistemic_state,
                    due_date=due, blocker=blocker,
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
                    "parents": parents, "content": text, "due_date": due,
                    "blocker": blocker, "updated_at": now,
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
