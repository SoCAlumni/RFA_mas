"""APScheduler 3.x adapters.

P0-022: cron/timezone validation and next-fire preview through `CronTrigger`.
Only the official 3.x API is used; this module contains no cron parser or calendar logic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger

from rfa_mas.errors import RfaError

# Bounded frequency: at most one fire per 15 minutes per schedule (checked on APScheduler's
# own computed fire times, never by interpreting the expression locally).
MIN_INTERVAL = timedelta(minutes=15)
PROBE_FIRES = 6


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
