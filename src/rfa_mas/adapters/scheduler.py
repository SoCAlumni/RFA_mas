"""APScheduler 3.x adapters.

P0-022: cron/timezone validation and next-fire preview through `CronTrigger`.
P0-023: the single-owner runner (`rfa scheduler`): AsyncIOScheduler + SQLAlchemyJobStore
(SQLite), an exclusive owner lock, and a manual-clock test adapter.

Only the official 3.x API is used (add_job/CronTrigger/pause_job/resume_job/remove_job and
job events); this module contains no cron parser or calendar logic. The job store is a
separate private SQLite file that serializes only DISPATCHER_REF and a schedule ID.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import stat
from collections import Counter, defaultdict, deque
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.events import EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED, EVENT_JOB_SUBMITTED
from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from rfa_mas.application.scheduling import job_id, miss_policy
from rfa_mas.contracts import sha256_text
from rfa_mas.errors import RfaError

logger = logging.getLogger(__name__)

# Bounded frequency: at most one fire per 15 minutes per schedule (checked on APScheduler's
# own computed fire times, never by interpreting the expression locally).
MIN_INTERVAL = timedelta(minutes=15)
PROBE_FIRES = 6
DISPATCHER_REF = "rfa_mas.adapters.scheduler:dispatch_scheduled"
SYNC_REF = "rfa_mas.adapters.scheduler:sync_scheduled"
SYNC_JOB_ID = "rfa-sync"
JOB_PREFIX = "rfa-schedule:"


def _invalid() -> RfaError:
    return RfaError("invalid_schedule", "예약 시각 또는 시간대를 확인할 수 없습니다.")


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise _invalid() from None


class ApschedulerTriggers:
    """SchedulerPort implementation over APScheduler 3.x CronTrigger."""

    adapter_name = "apscheduler-3.11-cron"
    simulated = False

    def trigger(self, cron: str, timezone: str) -> CronTrigger:
        fields = cron.split()
        # APScheduler 3.x from_crontab numbers weekdays from Monday=0, unlike crontab
        # (Sunday=0). Require names (mon..sun) so "1" can never silently mean Tuesday.
        if len(fields) != 5 or any(char.isdigit() for char in fields[4]):
            raise _invalid()
        try:
            return CronTrigger.from_crontab(" ".join(fields), timezone=zone(timezone))
        except (ValueError, TypeError, KeyError):
            raise _invalid() from None

    def validate(self, cron: str, timezone: str, *, now: datetime | None = None) -> None:
        trigger = self.trigger(cron, timezone)
        current = (now or datetime.now(UTC)).astimezone(UTC)
        previous = None
        fires: list[datetime] = []
        for _ in range(PROBE_FIRES):
            fire = trigger.get_next_fire_time(previous, current)
            if fire is None:
                break
            fires.append(fire)
            previous = current = fire
        if not fires:
            raise _invalid()  # Never fires (e.g. 30 Feb).
        if any(later - earlier < MIN_INTERVAL for earlier, later in pairwise(fires)):
            raise RfaError("invalid_schedule", "예약 간격은 15분 이상이어야 합니다.")

    def next_fire_time(self, cron: str, timezone: str, *, after: datetime) -> datetime | None:
        fire = self.trigger(cron, timezone).get_next_fire_time(None, after.astimezone(UTC))
        return None if fire is None else fire.astimezone(UTC)


# -- P0-023 single-owner runner ----------------------------------------------------------------
_ACTIVE: SchedulerRunner | None = None


async def dispatch_scheduled(schedule_id: str) -> None:
    """The fixed top-level job function. The job store holds only this reference + the ID."""
    runner = _ACTIVE
    if runner is None:
        raise RuntimeError("scheduler runner is not active")
    await runner.dispatch(schedule_id)


async def sync_scheduled() -> None:
    """Library interval job that reflects API schedule intent into the job store."""
    if _ACTIVE is not None:
        await _ACTIVE.sync()


def definition_fingerprint(schedule, misfire_mode: str) -> str:
    """Changes only when the job definition changes; drives the intended upsert."""
    return "def:" + sha256_text(json.dumps([
        "rfa-job-v1", DISPATCHER_REF, schedule.cron, schedule.timezone, schedule.job_type,
        misfire_mode,
    ]))[:32]


def _locked() -> RfaError:
    return RfaError("scheduler_owner_locked", "다른 scheduler runner가 이미 실행 중입니다.")


class RunnerLock:
    """Exclusive same-host owner lock (flock) held for the runner process lifetime."""

    def __init__(self, path: Path):
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        import fcntl

        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise RfaError("configuration_error", "scheduler lock은 일반 파일이어야 합니다.")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise _locked() from None
        except BaseException:
            os.close(fd)
            raise
        os.fchmod(fd, 0o600)
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        import fcntl

        fd, self._fd = self._fd, None
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _private_sqlite(path: Path) -> None:
    """The job store is pickled: only a regular 0600 file owned by this user is accepted."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise RfaError("configuration_error", "scheduler job store를 열 수 없습니다.") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise RfaError("configuration_error", "scheduler job store 파일이 안전하지 않습니다.")
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


class ManualClock:
    """Deterministic test clock (UTC). Never used by `rfa scheduler`."""

    def __init__(self, start: datetime):
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        self._now = value.astimezone(UTC)

    def advance(self, delta: timedelta) -> None:
        self._now += delta


@contextlib.contextmanager
def apscheduler_clock(clock: ManualClock) -> Iterator[None]:
    """Test adapter: drive the real APScheduler 3.x engine from a manual clock.

    3.x reads `datetime.now` in schedulers.base (due jobs, next run) and executors.base
    (misfire grace). Those two module references are pointed at the manual clock while a
    manual runner is active; all scheduling decisions remain APScheduler's own code.
    """
    import apscheduler.executors.base as executors_base
    import apscheduler.schedulers.base as schedulers_base

    class ClockDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            value = clock.now()
            return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)

    saved = (schedulers_base.datetime, executors_base.datetime)
    schedulers_base.datetime = executors_base.datetime = ClockDatetime
    try:
        yield
    finally:
        schedulers_base.datetime, executors_base.datetime = saved


class SchedulerRunner:
    """AsyncIOScheduler + SQLAlchemyJobStore owned by exactly one process.

    Startup: owner lock -> recover interrupted ledger rows -> start paused -> reconcile the
    job store with stored schedule intent (no reset of persisted next_run_time) -> resume.
    APScheduler then coalesces missed fires and applies misfire_grace_time per job type.
    """

    def __init__(self, *, jobstore_path: Path, repository, executor, triggers=None,
                 misfire_policy: str = "job_type_default",
                 sync_interval_seconds: float | None = 30.0,
                 clock: ManualClock | None = None,
                 now: Callable[[], datetime] | None = None):
        self.jobstore_path = jobstore_path
        self.lock = RunnerLock(Path(str(jobstore_path) + ".owner.lock"))
        self.repository, self.executor = repository, executor
        self.triggers = triggers or ApschedulerTriggers()
        self.misfire_policy = misfire_policy
        self.sync_interval_seconds = sync_interval_seconds
        self.clock = clock
        self.now = now or (clock.now if clock else (lambda: datetime.now(UTC)))
        self.scheduler: AsyncIOScheduler | None = None
        # Fire times reported by EVENT_JOB_SUBMITTED, consumed by the dispatcher.
        self._pending: defaultdict[str, deque[datetime]] = defaultdict(deque)
        self._schedule_of: dict[str, str] = {}
        self._job_of: dict[str, str] = {}
        self._tasks: set[asyncio.Task] = set()
        self._clock_patch: contextlib.AbstractContextManager | None = None
        self.last_sync: dict[str, int] = {}

    async def start(self) -> None:
        global _ACTIVE
        self.lock.acquire()
        try:
            if _ACTIVE is not None:
                raise _locked()
            _private_sqlite(self.jobstore_path)
            if self.clock is not None:
                self._clock_patch = apscheduler_clock(self.clock)
                self._clock_patch.__enter__()
            scheduler = AsyncIOScheduler(
                jobstores={
                    "default": SQLAlchemyJobStore(url=f"sqlite:///{self.jobstore_path}"),
                    "runtime": MemoryJobStore(),
                },
                executors={"default": AsyncIOExecutor()},
                job_defaults={"coalesce": True, "max_instances": 1},
                timezone=UTC,
            )
            scheduler.add_listener(self._on_submitted, EVENT_JOB_SUBMITTED)
            scheduler.add_listener(self._on_missed, EVENT_JOB_MISSED)
            scheduler.add_listener(self._on_max_instances, EVENT_JOB_MAX_INSTANCES)
            self.scheduler = scheduler
            _ACTIVE = self
            await self.repository.recover_interrupted_schedule_runs(self.now())
            scheduler.start(paused=True)
            self.last_sync = await self.sync()
            if self.sync_interval_seconds:
                scheduler.add_job(SYNC_REF, IntervalTrigger(seconds=self.sync_interval_seconds),
                                  id=SYNC_JOB_ID, jobstore="runtime", coalesce=True,
                                  max_instances=1, misfire_grace_time=None)
            scheduler.resume()
        except BaseException:
            await self.stop()
            raise

    async def stop(self, *, grace_seconds: float = 30.0) -> None:
        global _ACTIVE
        scheduler, self.scheduler = self.scheduler, None
        if scheduler is not None and scheduler.running:
            scheduler.pause()
            if self._tasks:  # Let in-flight fires finish; interrupted ones become unknown.
                await asyncio.wait(set(self._tasks), timeout=grace_seconds)
            scheduler.shutdown(wait=False)
            await asyncio.sleep(0)  # AsyncIOScheduler.shutdown runs on the event loop.
        if _ACTIVE is self:
            _ACTIVE = None
        if self._clock_patch is not None:
            self._clock_patch.__exit__(None, None, None)
            self._clock_patch = None
        self.lock.release()

    def jobs(self):
        return [] if self.scheduler is None else self.scheduler.get_jobs(jobstore="default")

    async def sync(self) -> dict[str, int]:
        """Reconcile stored intent into the job store. replace_existing only on real change."""
        scheduler = self.scheduler
        if scheduler is None:
            return {}
        schedules = await self.repository.runner_schedules()
        existing = {job.id: job for job in scheduler.get_jobs(jobstore="default")}
        counts: Counter[str] = Counter()
        known: set[str] = set()
        for schedule in schedules:
            jid = job_id(schedule.owner_id, schedule.schedule_id)
            known.add(jid)
            self._schedule_of[jid], self._job_of[schedule.schedule_id] = (
                schedule.schedule_id, jid)
            job = existing.get(jid)
            if schedule.state == "cancelled":
                if job is not None:
                    scheduler.remove_job(jid, jobstore="default")
                    counts["removed"] += 1
                continue
            fingerprint = definition_fingerprint(schedule, self.misfire_policy)
            if job is None and schedule.state != "active":
                continue  # Added when enabled; missed fires while disabled are not run.
            if job is None or job.name != fingerprint:
                try:
                    trigger = self.triggers.trigger(schedule.cron, schedule.timezone)
                except RfaError:
                    counts["invalid"] += 1
                    continue
                policy = miss_policy(schedule.job_type, self.misfire_policy)
                scheduler.add_job(
                    DISPATCHER_REF, trigger, args=[schedule.schedule_id], id=jid,
                    name=fingerprint, jobstore="default",
                    replace_existing=job is not None,  # The intended upsert only.
                    coalesce=policy.coalesce, max_instances=policy.max_instances,
                    misfire_grace_time=policy.misfire_grace_seconds,
                )
                counts["replaced" if job is not None else "added"] += 1
                if schedule.state == "disabled":
                    scheduler.pause_job(jid, jobstore="default")
            elif schedule.state == "disabled" and job.next_run_time is not None:
                scheduler.pause_job(jid, jobstore="default")
                counts["paused"] += 1
            elif schedule.state == "active" and job.next_run_time is None:
                scheduler.resume_job(jid, jobstore="default")
                counts["resumed"] += 1
            else:
                counts["unchanged"] += 1  # Persisted next_run_time is left as is.
        for jid in existing.keys() - known:
            scheduler.remove_job(jid, jobstore="default")
            counts["orphans_removed"] += 1
        return dict(counts)

    # -- APScheduler events -> ledger ----------------------------------------------------
    def _on_submitted(self, event) -> None:
        if event.job_id.startswith(JOB_PREFIX):
            self._pending[event.job_id].extend(event.scheduled_run_times)

    def _forget(self, jid: str, fire_time: datetime) -> None:
        with contextlib.suppress(ValueError):
            self._pending[jid].remove(fire_time)

    def _spawn(self, coroutine) -> None:
        task = asyncio.get_running_loop().create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _on_missed(self, event) -> None:
        schedule_id = self._schedule_of.get(event.job_id)
        self._forget(event.job_id, event.scheduled_run_time)
        if schedule_id is not None:
            self._spawn(self.executor.record_missed(
                schedule_id, event.scheduled_run_time.astimezone(UTC), "misfire"))

    def _on_max_instances(self, event) -> None:
        schedule_id = self._schedule_of.get(event.job_id)
        if schedule_id is not None:
            for fire_time in event.scheduled_run_times:
                self._spawn(self.executor.record_missed(
                    schedule_id, fire_time.astimezone(UTC), "max_instances"))

    async def dispatch(self, schedule_id: str) -> None:
        jid = self._job_of.get(schedule_id)
        queue = self._pending.get(jid) if jid else None
        if not queue:
            logger.warning("scheduled fire without a submitted run time; not executed")
            return
        fire_time = queue.popleft()
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            await self.executor.execute(schedule_id, fire_time.astimezone(UTC))
        finally:
            if task is not None:
                self._tasks.discard(task)

    # -- deterministic driving (manual clock) ---------------------------------------------
    async def drain(self, *, limit_seconds: float = 10.0) -> None:
        """Wait until every submitted fire finished or was recorded as missed."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + limit_seconds
        idle_rounds = 0
        while idle_rounds < 3:
            await asyncio.sleep(0)
            if self._tasks:
                idle_rounds = 0
                await asyncio.wait(set(self._tasks), timeout=max(deadline - loop.time(), 0))
            elif any(self._pending.values()):
                idle_rounds = 0
            else:
                idle_rounds += 1
            if loop.time() > deadline:
                raise TimeoutError("scheduled fires did not settle")

    async def tick(self) -> None:
        """Process due jobs at the current (manual) time, then wait for them to settle."""
        if self.scheduler is None:
            raise RfaError("configuration_error", "scheduler runner is not started")
        self.scheduler.wakeup()
        await self.drain()
