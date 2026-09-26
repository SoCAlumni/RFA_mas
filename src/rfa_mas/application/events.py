"""P0-025: owner status views and a cursor-paged, reference-only Run event feed.

Event sources read durable owner-scoped stores and emit reference events (IDs, revisions,
hashes, state/reason codes). The feed records each event once per owner under a gap-free
per-owner sequence, which is the polling cursor, so re-sending a cursor returns the same
events and newly observed events always come after every cursor already handed out.
A producer without a durable store of its own (the staged context loader) appends directly.

Never included: raw evidence/draft text, titles, summaries, parent relations, prompts or
tool arguments. Source references are re-authorized against the CURRENT ACL on every read;
a reference the caller can no longer read is only counted (withheld_sources). Lookup is by
the authenticated owner only; a trace ID or any other external identifier grants nothing.

Adding a source (e.g. P0-024 scheduler notifications, kind="notification"): implement
EventSource.collect() and pass it to EventFeed(sources=...). The cursor/DTO stay the same.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any, Protocol

from rfa_mas.contracts import (
    ContextLevel,
    DomainId,
    DraftBundle,
    EventDraftRef,
    EventPage,
    EventReceiptRef,
    EventReviewRef,
    PublicationReceipt,
    ReviewDecision,
    RunEvent,
    RunRecord,
    RunStatusView,
    TaskStatusView,
    TeamStatusView,
    TrustedPrincipal,
    WorkerStatus,
    new_id,
)
from rfa_mas.errors import ResourceNotFoundError, RfaError

CURSOR = re.compile(r"^ev1_([0-9]{1,18})$")
_SAFE = re.compile(r"^[a-z0-9_.:-]{1,80}$")
_REVIEW_KIND = "review_submission"
_WITHHOLD = {"resume_review_required", "policy_denied"}
_DEPTH = {ContextLevel.L0: 0, ContextLevel.L1: 1, ContextLevel.L2: 2}
_PLACED = {"listed", "loaded", "reused", "covered_by_summary"}
MAX_PAGE = 200


def encode_cursor(seq: int) -> str:
    return f"ev1_{seq}"


def decode_cursor(value: str | None) -> int:
    if value is None or value == "":
        return 0
    match = CURSOR.fullmatch(value)
    if match is None:
        raise RfaError("invalid_cursor", "cursor 형식이 올바르지 않습니다.")
    return int(match.group(1))


def safe_code(value: Any) -> str | None:
    """Pass through only short lowercase code strings; anything else is dropped."""
    return value if isinstance(value, str) and _SAFE.fullmatch(value) else None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def draft_ref(draft: DraftBundle) -> EventDraftRef:
    return EventDraftRef(
        draft_id=draft.draft_id,
        version=draft.version,
        content_hash=draft.content_hash,
        target_audience=draft.target.audience,
        policy_version=draft.policy_version,
    )


def review_ref(review: ReviewDecision) -> EventReviewRef:
    return EventReviewRef(
        review_ref=f"review:{review.draft_id}@v{review.draft_version}:{review.content_hash}",
        draft_id=review.draft_id,
        draft_version=review.draft_version,
        content_hash=review.content_hash,
        decision=review.decision,
    )


def receipt_ref(receipt: PublicationReceipt) -> EventReceiptRef:
    return EventReceiptRef(
        publication_id=receipt.publication_id,
        status=receipt.status,
        approval_id=receipt.approval_id,
        draft_version=receipt.binding.version,
        content_hash=receipt.binding.content_hash,
        mode=receipt.mode,
        next_action=receipt.next_action,
    )


def _event(
    key: str,
    run: RunRecord,
    kind: str,
    status: str,
    *,
    occurred_at: datetime | None = None,
    **payload: Any,
) -> dict[str, Any]:
    body = {
        "status": status,
        "session_id": run.session_id,
        "task_id": run.task_id,
        "domain_id": run.domain_id.value if run.domain_id else None,
    }
    body.update({k: v for k, v in payload.items() if v is not None})
    return {"event_key": key, "run_id": run.run_id, "kind": kind,
            "occurred_at": _iso(occurred_at), "payload": body}


def _dump(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude={"schema_version"})


class EventSource(Protocol):
    name: str

    async def collect(self, run: RunRecord, principal: TrustedPrincipal) -> list[dict]: ...


class RunStatusSource:
    name = "run"

    async def collect(self, run, principal):
        events = [_event(f"run:{run.run_id}:created", run, "run", "created",
                         occurred_at=run.created_at)]
        if run.status.value != "created":
            reason = safe_code(run.result.stop_reason) if run.result else None
            events.append(_event(f"run:{run.run_id}:{run.status.value}", run, "run",
                                 run.status.value, occurred_at=run.updated_at,
                                 reason_code=reason))
        return events


class RepositorySources:
    """Draft versions, review mirrors, receipts, team/roles and policy observations."""

    name = "repository"

    def __init__(self, repository: Any) -> None:
        self.repository = repository

    async def collect(self, run, principal):
        repo, events = self.repository, []
        for observation in await repo.list_observations(run.run_id, principal):
            trace = observation.event
            if trace.event != "policy" or trace.status == "started":
                continue
            outcome = {"succeeded": "allowed", "denied": "denied"}.get(trace.status, "error")
            events.append(_event(
                f"policy:{observation.observation_id}", run, "policy", outcome,
                occurred_at=trace.timestamp, reason_code=safe_code(trace.reason_code),
                policy={"outcome": outcome, "observation_id": observation.observation_id},
            ))
        for draft, _attachments in await repo.draft_versions(run.run_id, principal):
            events.append(_event(
                f"draft:{draft.draft_id}@v{draft.version}", run, "draft", "created",
                draft=_dump(draft_ref(draft)),
                sources=[{"source_id": ref.source_id, "source_revision": ref.source_revision}
                         for ref in draft.allowed_evidence],
            ))
        reviews: list[ReviewDecision] = []
        for effect in await repo.run_effects(run.run_id, principal):
            if effect.kind != _REVIEW_KIND:
                continue
            if effect.approval:
                try:
                    reviews.append(ReviewDecision.model_validate(effect.approval))
                except ValueError:
                    continue
            elif effect.state == "outcome_unknown" and effect.result_ref:
                events.append(_event(
                    f"review-unknown:{effect.result_ref}", run, "review", "outcome_unknown",
                    occurred_at=effect.updated_at, reason_code="query_required",
                ))
        if run.result and run.result.review:
            reviews.append(run.result.review)
        for review in reviews:
            ref = review_ref(review)
            events.append(_event(f"{ref.review_ref}:{review.decision.value}", run, "review",
                                 review.decision.value, review=_dump(ref)))
        receipt = await repo.get_publication(run.run_id, principal)
        if receipt is not None:
            events.append(_event(
                f"publication:{receipt.publication_id}:{receipt.status.value}", run,
                "publication", receipt.status.value, receipt=_dump(receipt_ref(receipt)),
            ))
        team = await repo.get_team_result(run.run_id, principal)
        if team is not None:
            for role in team.roles:
                events.append(_event(
                    f"role:{role.execution_key}:{role.status}", run, "role", role.status,
                    team_id=team.team_id, task_id=team.task_id,
                    role={"role": role.role, "status": role.status,
                          "error_code": safe_code(role.error_code)},
                ))
            events.append(_event(
                f"team:{run.run_id}:{team.status}", run, "team", team.status,
                team_id=team.team_id, task_id=team.task_id,
                reason_code=safe_code(team.stop_reason),
            ))
        return events


class EventFeed:
    def __init__(
        self,
        repository: Any,
        *,
        sources: Iterable[EventSource] | None = None,
        validate_draft: Callable[[], Callable[..., Any]] | None = None,
        present_result: Callable[..., Any] | None = None,
    ) -> None:
        self.repository = repository
        self.sources = tuple(sources) if sources is not None else (
            RunStatusSource(), RepositorySources(repository))
        # Read at call time: the same current ResumePolicy the graph uses.
        self._validate_draft = validate_draft
        self._present_result = present_result

    # -- producers --------------------------------------------------------------------
    async def record_context(self, principal: TrustedPrincipal, run_id: str, loaded) -> None:
        """Direct producer: staged context stage outcomes and the sources it placed."""
        try:
            run = await self.repository.get_owned_run(run_id, principal)
        except RfaError:
            return  # Not this caller's run: nothing is recorded.
        depth: dict[tuple[str, str], ContextLevel] = {}
        stages: dict[tuple[str, str], int] = {}
        for record in loaded.records:
            key = (record.stage.value, record.outcome)
            stages[key] = stages.get(key, 0) + record.characters
            if record.outcome in _PLACED:
                ref = (record.source_id, record.source_revision)
                if ref not in depth or _DEPTH[record.stage] > _DEPTH[depth[ref]]:
                    depth[ref] = record.stage
        event = _event(
            f"context:{run_id}:{new_id('ctx')}", run, "context",
            "insufficient" if loaded.insufficient else "loaded",
            occurred_at=datetime.now(UTC),
            reason_code=safe_code(loaded.reason),
            sources=[{"source_id": s, "source_revision": r, "stage": level.value}
                     for (s, r), level in sorted(depth.items())],
            stages=[{"stage": stage, "outcome": outcome, "characters": characters}
                    for (stage, outcome), characters in sorted(stages.items())],
        )
        await self.repository.record_run_events(principal, [event])

    # -- read side --------------------------------------------------------------------
    async def _owned_runs(self, principal: TrustedPrincipal) -> list[RunRecord]:
        runs = []
        for session in await self.repository.list_sessions(principal):
            detail = await self.repository.get_session(session.session_id, principal)
            runs.extend(detail.runs)
        return sorted(runs, key=lambda r: (r.created_at, r.run_id))

    async def _project(self, principal: TrustedPrincipal, runs: list[RunRecord]) -> None:
        events: list[dict] = []
        for run in runs:
            for source in self.sources:
                events.extend(await source.collect(run, principal))
        if events:
            await self.repository.record_run_events(principal, events)

    async def _allowed(self, principal: TrustedPrincipal, domain: str | None) -> set:
        if domain is None:
            return set()
        metadata = await self.repository.authorized_metadata(DomainId(domain), principal)
        return {(m.reference.source_id, m.reference.source_revision) for m in metadata}

    async def page(
        self,
        principal: TrustedPrincipal,
        *,
        cursor: str | None = None,
        limit: int = 50,
        run_id: str | None = None,
    ) -> EventPage:
        after = decode_cursor(cursor)
        if not 1 <= limit <= MAX_PAGE:
            raise RfaError("invalid_limit", "limit 범위가 올바르지 않습니다.")
        if run_id is not None:
            runs = [await self.repository.get_owned_run(run_id, principal)]  # 404 otherwise
        else:
            runs = await self._owned_runs(principal)
        await self._project(principal, runs)
        rows = await self.repository.list_run_events(
            principal, after=after, limit=limit + 1, run_id=run_id)
        has_more = len(rows) > limit
        rows = rows[:limit]
        allowed: dict[str | None, set] = {}
        events = []
        for row in rows:
            payload = dict(row["payload"])
            domain = payload.pop("domain_id", None)
            if domain not in allowed:
                allowed[domain] = await self._allowed(principal, domain)
            refs = payload.pop("sources", [])
            visible = [r for r in refs if (r["source_id"], r["source_revision"]) in allowed[domain]]
            events.append(RunEvent.model_validate({
                **payload,
                "cursor": encode_cursor(row["seq"]),
                "event_id": "evt_" + hashlib.sha256(row["event_key"].encode()).hexdigest()[:32],
                "kind": row["kind"],
                "run_id": row["run_id"],
                "occurred_at": row["occurred_at"],
                "recorded_at": row["recorded_at"],
                "sources": visible,
                "withheld_sources": len(refs) - len(visible),
            }))
        return EventPage(
            events=tuple(events),
            next_cursor=encode_cursor(rows[-1]["seq"] if rows else after),
            has_more=has_more,
        )

    # -- status views -----------------------------------------------------------------
    async def task_status(self, task_id: str, principal: TrustedPrincipal) -> TaskStatusView:
        try:
            lifecycle = await self.repository.get_team_lifecycle(task_id, principal)
        except RfaError as exc:
            raise ResourceNotFoundError("task") from exc
        return TaskStatusView(
            task_id=lifecycle.task.task_id,
            domain_id=lifecycle.task.domain_id,
            status=lifecycle.task.status,
            team=TeamStatusView(
                team_id=lifecycle.team.spec.team_id,
                lifecycle_phase=lifecycle.phase,
                lifecycle_reason=lifecycle.reason,
            ),
        )

    async def run_status(self, run_id: str, principal: TrustedPrincipal) -> RunStatusView:
        repo = self.repository
        record = await repo.get_owned_run(run_id, principal)
        result = record.result
        if result is not None and self._present_result is not None:
            result = await self._present_result(result, principal)
        task = None
        if record.task_id is not None:
            try:
                task = await self.task_status(record.task_id, principal)
            except RfaError:
                task = None
        team_result = await repo.get_team_result(run_id, principal)
        team = task.team if task else None
        workers: tuple[WorkerStatus, ...] = ()
        if team_result is not None:
            team = TeamStatusView(
                team_id=team_result.team_id,
                status=team_result.status,
                stop_reason=safe_code(team_result.stop_reason),
                lifecycle_phase=team.lifecycle_phase if team else None,
                lifecycle_reason=team.lifecycle_reason if team else None,
            )
            workers = tuple(
                WorkerStatus(role=r.role, status=r.status, error_code=safe_code(r.error_code),
                             steps=r.steps, tool_calls=r.tool_calls)
                for r in team_result.roles
            )
        draft, withheld, review = None, False, None
        versions = await repo.draft_versions(run_id, principal)
        if versions:
            latest = versions[-1][0]
            try:
                if self._validate_draft is not None:
                    await self._validate_draft()(latest, principal)
                draft = draft_ref(latest)
            except RfaError as exc:
                if exc.code not in _WITHHOLD:
                    raise
                withheld = True
            if not withheld:
                mirrors = [ReviewDecision.model_validate(e.approval)
                           for e in await repo.run_effects(run_id, principal)
                           if e.kind == _REVIEW_KIND and e.approval]
                if result is not None and result.review is not None:
                    mirrors.append(result.review)
                exact = [m for m in mirrors if m.draft_id == latest.draft_id
                         and m.draft_version == latest.version
                         and m.content_hash == latest.content_hash]
                review = review_ref(exact[-1]) if exact else None
        receipt = await repo.get_publication(run_id, principal)
        return RunStatusView(
            run_id=record.run_id,
            session_id=record.session_id,
            domain_id=record.domain_id,
            status=record.status,
            stop_reason=safe_code(result.stop_reason) if result else None,
            error_codes=tuple(c for c in (safe_code(e.code) for e in result.errors) if c)
            if result else (),
            created_at=record.created_at,
            updated_at=record.updated_at,
            task=task,
            team=team,
            workers=workers,
            draft=draft,
            draft_withheld=withheld,
            review=review,
            receipt=receipt_ref(receipt) if receipt else None,
        )


__all__ = [
    "EventFeed",
    "EventSource",
    "RepositorySources",
    "RunStatusSource",
    "decode_cursor",
    "encode_cursor",
    "safe_code",
]
