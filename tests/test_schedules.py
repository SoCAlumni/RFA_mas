"""P0-022..P0-024 owner schedules, the single APScheduler runner and notifications.

Synthetic notes, local SQLite and explicit clocks only: no network, keys or real waiting.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import ValidationError

from rfa_mas.adapters.scheduler import ApschedulerTriggers
from rfa_mas.api.app import create_app, resolve_principal
from rfa_mas.application.candidates import CandidateService
from rfa_mas.application.scheduling import (
    ScheduleExecutor,
    ScheduleService,
    job_id,
    occurrence,
    run_key,
)
from rfa_mas.bootstrap import build_container
from rfa_mas.contracts import KnowledgeWrite, ScheduleCreate
from rfa_mas.errors import RfaError
from rfa_mas.settings import Settings

# 2026-10-01T08:30:00+09:00: half an hour before the E2E-04 fixture clock (09:00 KST).
CLOCK = datetime(2026, 9, 30, 23, 30, tzinfo=UTC)
NINE_KST = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
CANARY = "CANARY_1ON1_Q7"


class Clock:
    def __init__(self, value: datetime = CLOCK):
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def make_settings(tmp_path, **overrides) -> Settings:
    base = tmp_path.resolve()  # macOS /var -> /private/var: the trace dir rejects symlinks.
    return Settings(_env_file=None, database_url=f"sqlite:///{base / 'rfa.db'}",
                    trace_dir=base / "traces", **overrides)


def note(key: str, content: str, revision: str = "r1", expected: str | None = None,
         title: str | None = None) -> KnowledgeWrite:
    return KnowledgeWrite.model_validate({
        "domain_id": "triv3",
        "provenance": {"provider": "note", "namespace": "sched", "external_id": key},
        "provider_revision": revision, "expected_revision": expected,
        "title": title or f"note {key}", "content": content, "synthetic": True,
        "acl": {"audience": "owner"}})


def create(job_type: str = "briefing", cron: str = "0 9 * * *", **extra) -> ScheduleCreate:
    return ScheduleCreate.model_validate(
        {"job_type": job_type, "domain_id": "triv3", "cron": cron, **extra})


def rows(container, sql: str, *params):
    with sqlite3.connect(container.repository.path) as db:
        return db.execute(sql, params).fetchall()


class Resolver:
    """Execution-time identity source. Tests swap the answer to prove the recheck."""

    def __init__(self, principal):
        self.principal = principal

    async def __call__(self, owner_id: str):
        return self.principal


class CountingCandidates:
    def __init__(self, inner):
        self.inner, self.calls = inner, 0

    async def discover(self, domain_id, principal):
        self.calls += 1
        return await self.inner.discover(domain_id, principal)

    async def ranked(self, domain_id, principal):
        self.calls += 1
        return await self.inner.ranked(domain_id, principal)


@pytest.fixture
async def env(tmp_path):
    container = build_container(make_settings(tmp_path))
    await container.startup()
    owner = await container.repository.local_principal()
    clock = Clock()
    schedules = ScheduleService(container.repository, ApschedulerTriggers(), clock=clock)
    try:
        yield container, owner, schedules, clock
    finally:
        await container.shutdown()


def executor_for(container, owner, clock, resolver=None):
    candidates = CountingCandidates(CandidateService(
        container.repository, container.knowledge.accumulator, clock=clock))
    return ScheduleExecutor(container.repository, resolve_principal=resolver or Resolver(owner),
                            candidates=candidates, accumulator=container.knowledge.accumulator,
                            clock=clock), candidates


# -- P0-022 AC1: allowlisted job types, bounded arguments, no execution authority --------------
@pytest.mark.parametrize("change", [
    {"job_type": "shell"}, {"job_type": "publish"}, {"job_type": "approve"},
    {"job_type": "organize"}, {"command": "synthetic-command"}, {"callable": "os:system"},
    {"prompt": "run any tool you like"}, {"args": {"shell": "x"}}, {"args": {"prompt": "x"}},
    {"args": {"max_items": 0}}, {"args": {"max_items": 51}},
    {"cron": "0 9 * * *; synthetic-command"}, {"cron": "0 9 * * * `id`"},
    {"timezone": "../../etc/passwd"},
])
def test_schedule_request_rejects_non_allowlisted_jobs_and_unbounded_input(change):
    base = {"job_type": "briefing", "domain_id": "triv3", "cron": "0 9 * * *"}
    with pytest.raises(ValidationError):
        ScheduleCreate.model_validate({**base, **change})


async def test_only_three_internal_job_types_are_stored_even_below_the_api(env):
    container, owner, schedules, _ = env
    for job_type in ("kb_refresh", "candidate_scan", "briefing"):
        stored = await schedules.create(create(job_type), owner)
        assert stored.job_type == job_type and stored.args.max_items == 10
    with pytest.raises(sqlite3.IntegrityError):  # Defense in depth: DB CHECK constraint.
        rows(container, "INSERT INTO schedules VALUES ('s', ?, 'shell', 'triv3', 'active', 1, "
                        "'{}', '2026-10-01T00:00:00+00:00')", owner.user_id)


@pytest.mark.parametrize("cron", [
    "0 25 * * *",       # invalid hour (APScheduler rejects)
    "0 9 * * * 2026",   # six fields
    "0 9 30 2 *",       # never fires
    "*/5 * * * *",      # more often than every 15 minutes
    "0,10 9 * * *",     # 10-minute gap
    "0 9 * * 1",        # numeric weekday: APScheduler 3.x would read 1 as Tuesday
])
async def test_cron_is_validated_by_apscheduler_and_frequency_is_bounded(env, cron):
    _, owner, schedules, _ = env
    with pytest.raises(RfaError) as error:
        await schedules.create(create(cron=cron), owner)
    assert error.value.code == "invalid_schedule"


def test_trigger_next_fire_is_computed_by_apscheduler_from_an_explicit_now():
    triggers = ApschedulerTriggers()
    assert triggers.next_fire_time("0 9 * * *", "Asia/Seoul", after=CLOCK) == NINE_KST
    after_nine = NINE_KST + timedelta(minutes=1)
    assert triggers.next_fire_time("0 9 * * *", "Asia/Seoul", after=after_nine) == (
        NINE_KST + timedelta(days=1))
    assert triggers.next_fire_time("0 9 * * mon-fri", "Asia/Seoul", after=CLOCK).weekday() == 3
    triggers.validate("0 9 * * mon-fri", "Asia/Seoul", now=CLOCK)


# -- P0-022 AC2: user timezone stored, internal UTC, default display Asia/Seoul ----------------
async def test_timezone_is_stored_times_are_utc_and_default_display_is_seoul(env):
    container, owner, schedules, _ = env
    seoul = await schedules.create(create(), owner)
    assert seoul.timezone == "Asia/Seoul"
    assert seoul.created_at.utcoffset() == timedelta(0) and seoul.next_run_at == NINE_KST
    assert seoul.next_run_local == "2026-10-01T09:00:00+09:00"
    york = await schedules.create(create(timezone="America/New_York"), owner)
    assert york.timezone == "America/New_York"
    assert york.next_run_at == datetime(2026, 10, 1, 13, 0, tzinfo=UTC)  # 09:00 EDT
    assert york.next_run_local == "2026-10-01T09:00:00-04:00"
    stored = rows(container, "SELECT updated_at, schedule_json FROM schedules")
    assert all(updated.endswith("+00:00") and '"timezone"' in body for updated, body in stored)
    assert all('"created_at": "2026-09-30T23:30:00Z"' in body for _, body in stored)
    with pytest.raises(RfaError) as error:
        await schedules.create(create(timezone="Mars/Olympus_Mons"), owner)
    assert error.value.code == "invalid_schedule"


# -- P0-022 AC3: owner/membership checks on every operation --------------------------------
async def test_every_operation_is_owner_scoped_and_task_refs_must_be_owned(env):
    container, owner, schedules, _ = env
    mine = await schedules.create(create(), owner)
    stranger = owner.model_copy(update={"user_id": "owner_someone_else"})
    assert await schedules.list(stranger) == []
    for operation in (schedules.get, schedules.disable, schedules.enable, schedules.cancel,
                      schedules.runs):
        with pytest.raises(RfaError) as error:
            await operation(mine.schedule_id, stranger)
        assert error.value.code == "not_found"
    anonymous = owner.model_copy(update={"authenticated": False})
    with pytest.raises(RfaError) as error:
        await schedules.list(anonymous)
    assert error.value.code == "authentication_required"
    rows(container, "INSERT INTO product_task_owners VALUES ('task-mine', ?, 'triv3')",
         owner.user_id)
    rows(container, "INSERT INTO product_task_owners VALUES ('task-other', 'owner_x', 'triv3')")
    assert (await schedules.create(create(task_ref="task-mine"), owner)).task_ref == "task-mine"
    for task_ref, domain in (("task-other", "triv3"), ("task-missing", "triv3"),
                             ("task-mine", "quantization_research")):
        with pytest.raises(RfaError) as error:
            await schedules.create(create(task_ref=task_ref, domain_id=domain), owner)
        assert error.value.code == "not_found"
    assert [s.schedule_id for s in await schedules.list(owner)][0:1]
    assert all(s.owner_id == owner.user_id for s in await schedules.list(owner))


async def test_disable_enable_cancel_keep_history_and_cancel_is_terminal(env):
    _, owner, schedules, clock = env
    created = await schedules.create(create(), owner)
    clock.value += timedelta(minutes=1)
    disabled = await schedules.disable(created.schedule_id, owner)
    assert disabled.state == "disabled" and disabled.next_run_at is None
    assert (await schedules.disable(created.schedule_id, owner)).revision == 2  # Idempotent.
    enabled = await schedules.enable(created.schedule_id, owner)
    assert enabled.state == "active" and enabled.next_run_at is not None
    cancelled = await schedules.cancel(created.schedule_id, owner)
    assert (await schedules.cancel(created.schedule_id, owner)) == cancelled
    assert [(h.revision, h.action) for h in cancelled.history] == [
        (1, "created"), (2, "disabled"), (3, "enabled"), (4, "cancelled")]
    assert all(h.at.utcoffset() == timedelta(0) for h in cancelled.history)
    with pytest.raises(RfaError) as error:
        await schedules.enable(created.schedule_id, owner)
    assert error.value.code == "invalid_state_transition"


async def test_execution_rechecks_identity_task_ownership_and_state(env):
    container, owner, schedules, clock = env
    await container.knowledge.write(note("i17", "issue #17: checksum 확인 필요, 10월 2일 마감"),
                                    owner)
    resolver = Resolver(owner)
    executor, candidates = executor_for(container, owner, clock, resolver)
    scan = await schedules.create(create("candidate_scan"), owner)
    run = await executor.execute(scan.schedule_id, NINE_KST)
    assert run.status == "succeeded" and run.summary == {"candidates": 1, "proposed": 1}
    assert candidates.calls == 1
    # The identity is resolved again at fire time: a missing or different owner is denied.
    for index, principal in enumerate((None, owner.model_copy(update={"user_id": "owner_y"}),
                                       owner.model_copy(update={"authenticated": False}))):
        resolver.principal = principal
        denied = await executor.execute(scan.schedule_id, NINE_KST + timedelta(days=index + 1))
        assert (denied.status, denied.reason) == ("denied", "owner_unverified")
    assert candidates.calls == 1  # Nothing ran for denied fires.
    resolver.principal = owner
    rows(container, "INSERT INTO product_task_owners VALUES ('task-a', ?, 'triv3')",
         owner.user_id)
    task_scan = await schedules.create(create("briefing", task_ref="task-a"), owner)
    rows(container, "UPDATE product_task_owners SET owner_id='owner_z' WHERE task_id='task-a'")
    revoked = await executor.execute(task_scan.schedule_id, NINE_KST)
    assert (revoked.status, revoked.reason) == ("denied", "task_not_owned")
    await schedules.disable(scan.schedule_id, owner)
    skipped = await executor.execute(scan.schedule_id, NINE_KST + timedelta(days=9))
    assert (skipped.status, skipped.reason) == ("skipped", "schedule_disabled")
    await schedules.cancel(scan.schedule_id, owner)
    after_cancel = await executor.execute(scan.schedule_id, NINE_KST + timedelta(days=10))
    assert (after_cancel.status, after_cancel.reason) == ("skipped", "schedule_cancelled")
    assert candidates.calls == 1
    for stored in rows(container, "SELECT summary_json, reason FROM schedule_runs"):
        assert CANARY not in str(stored)


async def test_same_fire_and_dst_repeat_produce_one_ledger_run(env):
    container, owner, schedules, clock = env
    executor, candidates = executor_for(container, owner, clock)
    daily = await schedules.create(create("candidate_scan"), owner)
    first = await executor.execute(daily.schedule_id, NINE_KST)
    again = await executor.execute(daily.schedule_id, NINE_KST)
    assert again == first and candidates.calls == 1
    assert first.run_key == run_key(daily.schedule_id, occurrence(NINE_KST, "Asia/Seoul"))
    # APScheduler 3.x fires "30 1 * * *" at both 01:30 EDT and 01:30 EST on the fall-back day;
    # both map to the same local occurrence, so only one run exists.
    york = await schedules.create(create("candidate_scan", cron="30 1 * * *",
                                         timezone="America/New_York"), owner)
    triggers = ApschedulerTriggers().trigger("30 1 * * *", "America/New_York")
    edt = triggers.get_next_fire_time(None, datetime(2026, 11, 1, 4, 0, tzinfo=UTC))
    est = triggers.get_next_fire_time(edt, edt)
    assert (edt.astimezone(UTC).hour, est.astimezone(UTC).hour) == (5, 6)
    assert (await executor.execute(york.schedule_id, edt)).status == "succeeded"
    repeat = await executor.execute(york.schedule_id, est)
    assert repeat.scheduled_fire_time == edt.astimezone(UTC) and candidates.calls == 2
    assert len(rows(container, "SELECT 1 FROM schedule_runs WHERE schedule_id=?",
                    york.schedule_id)) == 1
    assert job_id(owner.user_id, york.schedule_id) != job_id("owner_other", york.schedule_id)


async def test_schedule_api_routes_are_owner_bound(env):
    container, owner, _, _ = env
    app = create_app(container=container)
    app.dependency_overrides[resolve_principal] = lambda: owner
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        made = await client.post("/v1/schedules", json={
            "job_type": "briefing", "domain_id": "triv3", "cron": "0 9 * * *"})
        assert made.status_code == 201 and made.json()["timezone"] == "Asia/Seoul"
        schedule_id = made.json()["schedule_id"]
        forbidden = await client.post("/v1/schedules", json={
            "job_type": "briefing", "domain_id": "triv3", "cron": "0 9 * * *",
            "command": "synthetic"})
        assert forbidden.status_code == 422
        bad = await client.post("/v1/schedules", json={
            "job_type": "briefing", "domain_id": "triv3", "cron": "0 99 * * *"})
        assert bad.status_code == 422 and bad.json()["code"] == "invalid_schedule"
        assert [s["schedule_id"] for s in (await client.get("/v1/schedules")).json()] == [
            schedule_id]
        for action, state in (("disable", "disabled"), ("enable", "active"),
                              ("cancel", "cancelled")):
            changed = await client.post(f"/v1/schedules/{schedule_id}/{action}")
            assert changed.status_code == 200 and changed.json()["state"] == state
        assert (await client.post(f"/v1/schedules/{schedule_id}/enable")).status_code == 409
        assert (await client.get(f"/v1/schedules/{schedule_id}/runs")).json() == []
        app.dependency_overrides[resolve_principal] = lambda: owner.model_copy(
            update={"user_id": "owner_stranger"})
        assert (await client.get(f"/v1/schedules/{schedule_id}")).status_code == 404
        assert (await client.get("/v1/schedules")).json() == []



# -- P0-022: the P1-004 "schedule" intent stores owner intent through ScheduleService --------
@pytest.mark.parametrize(("text", "job_type", "cron"), [
    ("매일 오전 9시에 TRIV3 할 일 알려줘", "candidate_scan", "0 9 * * *"),
    ("평일 오후 6시 반 TRIV3 브리핑 예약", "briefing", "30 18 * * mon-fri"),
    ("매주 월요일, 수요일 8:15 TRIV3 자료 정리 예약", "kb_refresh", "15 8 * * mon,wed"),
    ("TRIV3 브리핑 예약 cron 0 7 * * sat,sun", "briefing", "0 7 * * sat,sun"),
    ("아침마다 TRIV3 요약 알림", "briefing", "0 9 * * *"),
])
def test_schedule_wording_routes_to_one_allowlisted_job_deterministically(text, job_type, cron):
    from rfa_mas.application.graphs.supervisor import route_intent

    first = route_intent(text, domain_id=None, task_id=None, ingress="direct")
    assert first == route_intent(text, domain_id=None, task_id=None, ingress="direct")
    assert first["intent"] == "schedule" and first["supported"] is True
    assert first["schedule"] == {"job_type": job_type, "cron": cron, "domain_id": "triv3",
                                 "task_ref": None}


@pytest.mark.parametrize("text", [
    "매일 TRIV3 브리핑 예약",          # no time
    "TRIV3 9시 브리핑 예약",           # no recurrence
    "매일 오전 9시 TRIV3 예약",        # no allowlisted job
])
def test_schedule_without_details_asks_for_them_and_stores_nothing(text):
    from rfa_mas.application.graphs.supervisor import route_intent

    decision = route_intent(text, domain_id=None, task_id=None, ingress="direct")
    assert decision["intent"] == "schedule" and decision["supported"] is False
    assert decision["limitations"] == ("schedule_details_required",)
    assert decision["next_options"] and "schedule" not in decision


async def test_assistant_schedule_intent_creates_one_owner_schedule(env):
    from rfa_mas.contracts import AssistantRequest

    container, owner, _, _ = env
    service = container.service
    request = AssistantRequest(text="매일 오전 9시에 TRIV3 할 일 알려줘")
    made = await service.assist(request, owner, knowledge=container.knowledge,
                                schedules=container.schedules)
    again = await service.assist(request, owner, knowledge=container.knowledge,
                                 schedules=container.schedules)
    assert made.status == again.status == "scheduled" and made.run is None
    assert made.schedule.schedule_id == again.schedule.schedule_id  # no duplicate fires
    assert (made.schedule.job_type, made.schedule.cron, made.schedule.timezone) == (
        "candidate_scan", "0 9 * * *", "Asia/Seoul")
    assert made.schedule.owner_id == owner.user_id and made.schedule.state == "active"
    assert rows(container, "SELECT count(*) FROM schedules") == [(1,)]
    assert rows(container, "SELECT count(*) FROM runs") == [(0,)]  # intent only, no run
    vague = await service.assist(AssistantRequest(text="매일 TRIV3 브리핑 예약"), owner,
                                 knowledge=container.knowledge, schedules=container.schedules)
    assert vague.status == "unsupported" and vague.stop_reason == "schedule_details_required"
    channel = await service.assist(
        AssistantRequest(text="매일 오전 9시 TRIV3 브리핑 예약", ingress="internal"), owner,
        knowledge=container.knowledge, schedules=container.schedules)
    assert channel.status == "unsupported" and channel.decision.rule == "channel-scope"
    assert rows(container, "SELECT count(*) FROM schedules") == [(1,)]


async def test_assistant_route_passes_the_schedule_service(env):
    container, owner, _, _ = env
    app = create_app(container=container)
    app.dependency_overrides[resolve_principal] = lambda: owner
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        made = await client.post("/v1/assistant",
                                 json={"text": "평일 오후 6시 반 TRIV3 브리핑 예약"})
        assert made.status_code == 201 and made.json()["status"] == "scheduled"
        schedule_id = made.json()["schedule"]["schedule_id"]
        assert [s["schedule_id"] for s in (await client.get("/v1/schedules")).json()] == [
            schedule_id]
