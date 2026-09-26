"""Owner schedules (P0-022): intent storage, ownership checks and execution-time rechecks.

The API process stores schedule intent only. The dedicated `rfa scheduler` runner owns the
APScheduler job store and calls ScheduleExecutor for each fire. Jobs are a closed allowlist of
internal work (kb_refresh, candidate_scan, briefing). A job never approves, publishes, creates a
Task/team or calls an external channel, and it re-resolves the owner on every fire.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from rfa_mas.contracts import (
    JobRun,
    Schedule,
    ScheduleCreate,
    TrustedPrincipal,
    new_id,
    sha256_text,
)
from rfa_mas.errors import RfaError

DEFAULT_TIMEZONE = "Asia/Seoul"
JOB_TYPES = ("kb_refresh", "candidate_scan", "briefing")
PrincipalResolver = Callable[[str], Awaitable[TrustedPrincipal | None]]


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
            summary = await self.handlers[schedule.job_type](schedule, principal, run)
        except RfaError as exc:
            return await self._finish(run, "failed", safe_reason(exc.code))
        except Exception:
            return await self._finish(run, "failed", "internal_error")
        return await self._finish(run, "succeeded", None, summary)

    # -- allowlisted job handlers: internal organizing, candidates and briefings only ------
    async def _kb_refresh(self, schedule: Schedule, principal: TrustedPrincipal, run: JobRun):
        proposals = await self.accumulator.extract(schedule.domain_id, principal,
                                                   origin_ref="schedule")
        report = await self.accumulator.accumulate(schedule.domain_id, proposals, principal)
        accepted = sum(item.review_state == "accepted" for item in report.items)
        return {"proposals": len(proposals), "accepted": accepted,
                "rejected": len(report.items) - accepted}

    async def _candidate_scan(self, schedule: Schedule, principal: TrustedPrincipal,
                              run: JobRun):
        found = await self.candidates.discover(schedule.domain_id, principal)
        return {"candidates": len(found),
                "proposed": sum(c.state == "proposed" for c in found)}

    async def _briefing(self, schedule: Schedule, principal: TrustedPrincipal, run: JobRun):
        ranked = await self.candidates.ranked(schedule.domain_id, principal)
        return {"ranked": len(ranked), "listed": min(len(ranked), schedule.args.max_items)}
