"""Owner schedules (P0-022): intent storage, ownership checks and execution-time rechecks.

The API process stores schedule intent only. The dedicated `rfa scheduler` runner owns the
APScheduler job store and calls ScheduleExecutor for each fire. Jobs are a closed allowlist of
internal work (kb_refresh, candidate_scan, briefing). A job never approves, publishes, creates a
Task/team or calls an external channel, and it re-resolves the owner on every fire.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from rfa_mas.application.candidates import rank
from rfa_mas.contracts import (
    JobRun,
    Notification,
    NotificationItem,
    Schedule,
    ScheduleCreate,
    TrustedPrincipal,
    new_id,
    sha256_text,
)
from rfa_mas.errors import RfaError

DEFAULT_TIMEZONE = "Asia/Seoul"
JOB_TYPES = ("kb_refresh", "candidate_scan", "briefing")
# Active notifications older than this are held (history only) at the owner's next run.
NOTIFICATION_HOLD_AFTER = timedelta(hours=24)
PrincipalResolver = Callable[[str], Awaitable[TrustedPrincipal | None]]


@dataclass(frozen=True)
class MissPolicy:
    """APScheduler 3.x add_job options for one job_type (P0-023/P0-024)."""

    coalesce: bool
    misfire_grace_seconds: int | None  # None: run late once, however late (APScheduler 3.x).
    max_instances: int = 1


# Missed fires (PC off/asleep) are coalesced by APScheduler into the most recent one.
# briefing: always one late run with the latest allowed material (older notifications held).
# candidate_scan: one late run within 24h; older fires become skipped ledger rows.
# kb_refresh: one late run within 1h; the next run recomputes only changed sources.
MISS_POLICIES = {
    "briefing": MissPolicy(coalesce=True, misfire_grace_seconds=None),
    "candidate_scan": MissPolicy(coalesce=True, misfire_grace_seconds=86_400),
    "kb_refresh": MissPolicy(coalesce=True, misfire_grace_seconds=3_600),
}
SKIP_MISSED = MissPolicy(coalesce=True, misfire_grace_seconds=60)


def miss_policy(job_type: str, mode: str = "job_type_default") -> MissPolicy:
    """SCHEDULER_MISFIRE_POLICY=skip_missed disables catch-up for every job type."""
    return SKIP_MISSED if mode == "skip_missed" else MISS_POLICIES[job_type]


def _now() -> datetime:
    return datetime.now(UTC)


def _owner(principal: TrustedPrincipal) -> str:
    if not principal.authenticated or not principal.user_id:
        raise RfaError("authentication_required", "유효한 API 인증이 필요합니다.")
    return principal.user_id


def job_id(owner_id: str, schedule_id: str) -> str:
    """Stable APScheduler job ID per owner/schedule; the same schedule never gets two jobs."""
    return "rfa-schedule:" + sha256_text(json.dumps([owner_id, schedule_id]))[:40]


def occurrence(fire_time: datetime, timezone: str) -> str:
    """Local wall-clock occurrence. A DST fall-back repeat maps to the same value."""
    return fire_time.astimezone(ZoneInfo(timezone)).replace(tzinfo=None).isoformat(
        timespec="seconds"
    )


def run_key(schedule_id: str, local_occurrence: str) -> str:
    """Stable operation key for one scheduled occurrence (also offered to the effect hook)."""
    return sha256_text(json.dumps(["rfa-schedule-run-v1", schedule_id, local_occurrence]))


def safe_reason(value: str) -> str:
    return value if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value) else "internal_error"


def evidence_key(candidate) -> str:
    """Identity of the evidence behind a candidate: its parent source revisions."""
    return sha256_text(json.dumps(sorted([p.source_id, p.source_revision]
                                         for p in candidate.parents)))


def notification_items(ranked) -> tuple[NotificationItem, ...]:
    """References, rank explanations and source revision IDs only; no source text."""
    return tuple(
        NotificationItem(
            candidate_id=entry.candidate.candidate_id, rank=entry.rank,
            title=entry.candidate.title[:300], due_date=entry.candidate.due_date,
            blocker=entry.candidate.blocker,
            reasons=tuple(reason.explanation for reason in entry.reasons)[:8],
            source_refs=tuple(f"{p.source_id}:{p.source_revision}"
                              for p in entry.candidate.parents)[:32],
        )
        for entry in ranked[:50]
    )


class ScheduleService:
    """API-side schedule management. Every call is owner-scoped; others see not_found."""

    def __init__(self, repository, triggers, *, default_timezone: str = DEFAULT_TIMEZONE,
                 clock: Callable[[], datetime] = _now):
        self.repository, self.triggers = repository, triggers
        self.default_timezone, self.clock = default_timezone, clock

    def present(self, schedule: Schedule) -> Schedule:
        """Attach the trigger's next-fire preview (UTC) and its local display string."""
        if schedule.state != "active":
            return schedule.model_copy(update={"next_run_at": None, "next_run_local": None})
        fire = self.triggers.next_fire_time(schedule.cron, schedule.timezone, after=self.clock())
        return schedule.model_copy(update={
            "next_run_at": fire,
            "next_run_local": None if fire is None
            else fire.astimezone(ZoneInfo(schedule.timezone)).isoformat(),
        })

    async def create(self, body: ScheduleCreate, principal: TrustedPrincipal) -> Schedule:
        owner = _owner(principal)
        body = ScheduleCreate.model_validate(body.model_dump())
        timezone = body.timezone or self.default_timezone
        now = self.clock()
        self.triggers.validate(body.cron, timezone, now=now)  # APScheduler CronTrigger.
        if body.task_ref is not None and not await self.repository.schedule_task_allowed(
            body.task_ref, body.domain_id, principal
        ):
            raise RfaError("not_found", "Task를 찾을 수 없습니다.")
        schedule = Schedule(
            schedule_id=new_id("schedule"), owner_id=owner, job_type=body.job_type,
            domain_id=body.domain_id, task_ref=body.task_ref, cron=" ".join(body.cron.split()),
            timezone=timezone, args=body.args, created_at=now, updated_at=now,
        )
        return self.present(await self.repository.create_schedule(schedule, principal))

    async def list(self, principal: TrustedPrincipal) -> list[Schedule]:
        _owner(principal)
        return [self.present(s) for s in await self.repository.list_schedules(principal)]

    async def get(self, schedule_id: str, principal: TrustedPrincipal) -> Schedule:
        _owner(principal)
        return self.present(await self.repository.get_schedule(schedule_id, principal))

    async def _transition(self, schedule_id: str, principal: TrustedPrincipal, state: str):
        _owner(principal)
        return self.present(await self.repository.transition_schedule(
            schedule_id, principal, to_state=state, at=self.clock()
        ))

    async def disable(self, schedule_id: str, principal: TrustedPrincipal) -> Schedule:
        return await self._transition(schedule_id, principal, "disabled")

    async def enable(self, schedule_id: str, principal: TrustedPrincipal) -> Schedule:
        return await self._transition(schedule_id, principal, "active")

    async def cancel(self, schedule_id: str, principal: TrustedPrincipal) -> Schedule:
        return await self._transition(schedule_id, principal, "cancelled")

    async def runs(self, schedule_id: str, principal: TrustedPrincipal) -> list[JobRun]:
        _owner(principal)
        return await self.repository.list_schedule_runs(schedule_id, principal)

    async def notifications(self, principal: TrustedPrincipal, *,
                            include_held: bool = False) -> list[Notification]:
        """Owner-only history. Held (stale) notifications appear only with include_held."""
        _owner(principal)
        return await self.repository.list_notifications(principal, include_held=include_held)


class ScheduledEffectHook(Protocol):
    """Hook for P0-021's generic effect ledger (not integrated on this branch).

    Called with the stable run key: phase "intent" right after the ledger claim, then one of
    "succeeded" / "skipped" / "denied" / "failed". The scheduled-run ledger stays authoritative
    for idempotency; the generic ledger may later mirror or reconcile these effects.
    """

    async def record(self, *, operation_key: str, owner_id: str, kind: str, phase: str,
                     result_ref: str | None = None) -> None: ...


class NoopScheduledEffectHook:
    async def record(self, **_: Any) -> None:
        return None


class ScheduleExecutor:
    """Runner-side execution of one fire. Only existing internal services are called."""

    def __init__(self, repository, *, resolve_principal: PrincipalResolver, candidates,
                 accumulator, clock: Callable[[], datetime] = _now,
                 effects: ScheduledEffectHook | None = None):
        self.repository, self.resolve_principal = repository, resolve_principal
        self.candidates, self.accumulator, self.clock = candidates, accumulator, clock
        self.effects = effects or NoopScheduledEffectHook()
        self.handlers = {
            "kb_refresh": self._kb_refresh,
            "candidate_scan": self._candidate_scan,
            "briefing": self._briefing,
        }
        # Jobs of one owner/domain share the candidate and derived-knowledge stores; the
        # single runner serializes them (candidate upserts are compare-and-set).
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    def _run(self, schedule: Schedule, fire_time: datetime, status: str,
             reason: str | None = None) -> JobRun:
        local = occurrence(fire_time, schedule.timezone)
        now = self.clock()
        return JobRun(
            run_key=run_key(schedule.schedule_id, local), schedule_id=schedule.schedule_id,
            owner_id=schedule.owner_id, job_type=schedule.job_type,
            scheduled_fire_time=fire_time, occurrence=local, status=status, reason=reason,
            started_at=now, finished_at=None if status == "running" else now,
        )

    async def _finish(self, run: JobRun, status: str, reason: str | None = None,
                      summary: dict[str, int] | None = None) -> JobRun:
        finished = await self.repository.finish_schedule_run(run.model_copy(update={
            "status": status, "reason": reason, "summary": summary or {},
            "finished_at": self.clock(),
        }))
        await self.effects.record(operation_key=run.run_key, owner_id=run.owner_id,
                                  kind=f"schedule:{run.job_type}", phase=finished.status,
                                  result_ref=run.run_key)
        return finished

    async def record_missed(self, schedule_id: str, fire_time: datetime,
                            reason: str) -> JobRun | None:
        """APScheduler reported a missed/max-instance fire: keep a skipped ledger row."""
        schedule = await self.repository.runner_schedule(schedule_id)
        if schedule is None:
            return None
        run, _ = await self.repository.claim_schedule_run(
            self._run(schedule, fire_time, "skipped", safe_reason(reason))
        )
        return run

    async def execute(self, schedule_id: str, fire_time: datetime) -> JobRun | None:
        schedule = await self.repository.runner_schedule(schedule_id)
        if schedule is None:
            return None  # Orphan job; the runner's sync removes it.
        run, created = await self.repository.claim_schedule_run(
            self._run(schedule, fire_time, "running")
        )
        if not created:
            return run  # Same fire/occurrence already claimed: no second run.
        await self.effects.record(operation_key=run.run_key, owner_id=run.owner_id,
                                  kind=f"schedule:{run.job_type}", phase="intent")
        if schedule.state != "active":
            return await self._finish(run, "skipped", f"schedule_{schedule.state}")
        try:
            # Execution-time recheck: identity and Task ownership are resolved again now.
            principal = await self.resolve_principal(schedule.owner_id)
            if (principal is None or not principal.authenticated
                    or principal.user_id != schedule.owner_id):
                return await self._finish(run, "denied", "owner_unverified")
            if schedule.task_ref is not None and not await self.repository.schedule_task_allowed(
                schedule.task_ref, schedule.domain_id, principal
            ):
                return await self._finish(run, "denied", "task_not_owned")
            lock = self._locks.setdefault((schedule.owner_id, schedule.domain_id.value),
                                          asyncio.Lock())
            async with lock:
                summary = await self._handle(schedule, principal, run)
        except RfaError as exc:
            return await self._finish(run, "failed", safe_reason(exc.code))
        except Exception:
            return await self._finish(run, "failed", "internal_error")
        return await self._finish(run, "succeeded", None, summary)

    async def _handle(self, schedule: Schedule, principal: TrustedPrincipal, run: JobRun):
        """One bounded retry when a concurrent writer (e.g. an API discover) won a CAS.

        Every handler is idempotent per run key: discovery re-reads current state, the
        notification is unique per run key and event consumption is insert-or-ignore.
        """
        try:
            return await self.handlers[schedule.job_type](schedule, principal, run)
        except RfaError as exc:
            if exc.code != "idempotency_conflict":
                raise
        summary = await self.handlers[schedule.job_type](schedule, principal, run)
        return summary | {"retried": 1}

    # -- allowlisted job handlers: internal organizing, candidates and briefings only ------
    async def _kb_refresh(self, schedule: Schedule, principal: TrustedPrincipal, run: JobRun):
        """Re-derive knowledge only for source revisions this schedule has not processed."""
        events = await self.repository.pending_source_events(
            principal, schedule.domain_id, schedule.schedule_id)
        if not events:
            return {"events": 0, "accepted": 0, "rejected": 0}
        report = await self.accumulator.refresh_changed(
            schedule.domain_id, principal, {event["source_id"] for event in events},
            origin_ref="schedule")
        await self.repository.consume_source_events(
            schedule.schedule_id, [event["event_id"] for event in events], run.run_key)
        accepted = sum(item.review_state == "accepted" for item in report.items)
        return {"events": len(events), "accepted": accepted,
                "rejected": len(report.items) - accepted}

    async def _hold_stale(self, principal: TrustedPrincipal) -> int:
        now = self.clock()
        return await self.repository.hold_stale_notifications(
            principal.user_id, before=now - NOTIFICATION_HOLD_AFTER, at=now)

    def _notification(self, schedule: Schedule, run: JobRun, kind: str,
                      ranked) -> Notification:
        return Notification(
            notification_id=new_id("notification"), owner_id=schedule.owner_id,
            schedule_id=schedule.schedule_id, run_key=run.run_key, kind=kind,
            items=notification_items(ranked), created_at=self.clock(),
        )

    async def _candidate_scan(self, schedule: Schedule, principal: TrustedPrincipal,
                              run: JobRun):
        """Propose only candidates backed by evidence the owner has not been notified of."""
        held = await self._hold_stale(principal)
        events = await self.repository.pending_source_events(
            principal, schedule.domain_id, schedule.schedule_id)
        if not events:
            return {"events": 0, "candidates": 0, "held": held, "notified": 0}
        found = await self.candidates.discover(schedule.domain_id, principal)
        # Rejected/superseded candidates are not listed by discover(); accepted/deferred are
        # owner decisions, not new proposals.
        fresh = rank([c for c in found if c.state == "proposed"], now=self.clock())
        notified = 0
        if fresh:
            stored = await self.repository.record_notification(
                self._notification(schedule, run, "candidates", fresh),
                evidence={e.candidate.candidate_id: evidence_key(e.candidate) for e in fresh},
                limit=schedule.args.max_items,
            )
            notified = 0 if stored is None else len(stored.items)
        await self.repository.consume_source_events(
            schedule.schedule_id, [event["event_id"] for event in events], run.run_key)
        return {"events": len(events), "candidates": len(found), "held": held,
                "notified": notified}

    async def _briefing(self, schedule: Schedule, principal: TrustedPrincipal, run: JobRun):
        """One owner-only briefing per fire from the latest allowed material."""
        held = await self._hold_stale(principal)
        found = await self.candidates.discover(schedule.domain_id, principal)
        ranked = rank(found, now=self.clock())[: schedule.args.max_items]
        stored = await self.repository.record_notification(
            self._notification(schedule, run, "briefing", ranked))
        return {"candidates": len(found), "items": len(stored.items), "held": held}
