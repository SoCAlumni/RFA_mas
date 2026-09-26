"""E2E-10 with the real app process: SIGKILL mid-role, restart, recovery, resume and cancel.

The server is the product's own `rfa api` entrypoint (uvicorn) started as a child process on
a free loopback port with a synthetic env file in a temp data dir. The kill is a real SIGKILL
while the database shows a team role in the running state; nothing is simulated in-process.
Model/review/runtime stay the controlled mock/local adapters.

A second SQLite process (harness.SecondSqliteReader, like the separate scheduler process)
keeps opening short read connections to the same database for the whole kill/restart
attempt, and a concurrent request burst runs beside it (the former
diagnose_sqlite_second_process.py defect, fixed by P0-005A, as a real check).
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from datetime import UTC, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any

import fixture_pack as pack
import harness as h
import httpx
import pytest

from rfa_mas.adapters.scheduler import ManualClock
from rfa_mas.bootstrap import build_scheduler_runner

SMALL20 = pack.set_members("small20")
REQUESTS = pack.read_json("scenarios.json")["scenarios"]["E2E-03"]["requests"]
BUDGET = h.CONFIG["budgets"]["team"]
PINNED = {
    "max_graph_steps": BUDGET["max_steps"],
    "max_tool_calls": BUDGET["max_tool_calls"],
    "tool_timeout_seconds": BUDGET["timeout_seconds"],
}
SERVER_PINNED = {
    "MAX_GRAPH_STEPS": str(BUDGET["max_steps"]),
    "MAX_TOOL_CALLS": str(BUDGET["max_tool_calls"]),
    "TOOL_TIMEOUT_SECONDS": str(BUDGET["timeout_seconds"]),
}
repeats = pytest.mark.parametrize("repeat", range(1, h.REPEATS + 1))
BURST = 20  # concurrent /v1/work requests beside the second SQLite process


def ms_since(start: float) -> float:
    return (perf_counter() - start) * 1000


def db_rows(path: Path, sql: str, *args: Any) -> list[tuple]:
    with contextlib.closing(sqlite3.connect(path)) as db:
        return db.execute(sql, args).fetchall()


def roles_of(path: Path, run_id: str) -> list[tuple]:
    return db_rows(
        path,
        "SELECT role, status FROM role_executions WHERE run_id=? ORDER BY started_at, role",
        run_id,
    )


KILL_DELAYS = (0.25, 0.12, 0.45)  # seconds after posting the two in-flight team runs


async def interrupt_two_team_runs(server: h.LocalServer, db: Path, bench, research):
    """Post two team runs in fresh sessions and SIGKILL the server after a short delay.

    The database is read only while the server is dead (a live second SQLite client can
    disturb the server's WAL). If no role was running at the kill, restart and retry with
    another delay; every attempt is reported.
    """
    tried = []
    for attempt, delay in enumerate(KILL_DELAYS, 1):
        async with httpx.AsyncClient(base_url=server.base_url, timeout=60) as client:
            pair = [(await client.post("/v1/sessions")).json()["session_id"] for _ in range(2)]
            inflight = [
                asyncio.create_task(client.post(f"/v1/sessions/{pair[0]}/work", json=bench)),
                asyncio.create_task(client.post(f"/v1/sessions/{pair[1]}/work", json=research)),
            ]
            await asyncio.sleep(delay)
            server.kill()
            await asyncio.gather(*inflight, return_exceptions=True)
        runs = [
            row[0]
            for row in db_rows(
                db,
                "SELECT run_id FROM runs WHERE session_id IN (?, ?) AND status='running' "
                "ORDER BY created_at",
                *pair,
            )
        ]
        running = db_rows(
            db,
            "SELECT run_id, role FROM role_executions WHERE status='running' "
            "AND run_id IN (SELECT run_id FROM runs WHERE session_id IN (?, ?))",
            *pair,
        )
        tried.append(
            {
                "attempt": attempt,
                "delay_s": delay,
                "interrupted": len(runs),
                "roles_running": len(running),
            }
        )
        if running and len(runs) == 2:
            return tried, runs, running
        server.start()
        await server.wait_ready()
    return tried, [], []


async def seed(root: Path) -> dict[str, h.Ingested]:
    async with h.open_stack(root, **PINNED) as stack:
        await h.ingest(stack, SMALL20)
        return dict(stack.sources)


@repeats
async def test_e2e10_real_app_kill_mid_role_restart_resume_and_cancel(
    tmp_path, recorder, network_guard, request, repeat
):
    network_guard.allow_loopback()
    root = (tmp_path / "data").resolve()
    db = root / "rfa.db"
    bench = h.team_body(REQUESTS["benchmark"], REQUESTS["benchmark"], ("benchmark_report",))
    research = h.team_body(REQUESTS["research"], REQUESTS["research"], ("research_report",))
    with recorder.attempt("E2E-10", "process-kill-restart", repeat=repeat) as a:
        await seed(root)
        reader = h.SecondSqliteReader(db)
        reader.start()
        request.addfinalizer(reader.stop)
        server = h.LocalServer(root, **SERVER_PINNED)
        server.start()
        try:
            first_ready_ms = await server.wait_ready()
            async with httpx.AsyncClient(base_url=server.base_url, timeout=60) as client:
                sessions = [
                    (await client.post("/v1/sessions")).json()["session_id"] for _ in range(2)
                ]
                query = await client.post(
                    f"/v1/sessions/{sessions[0]}/work", json=h.work_body("SDK 출시일이 언제야?")
                )
                done = await client.post(f"/v1/sessions/{sessions[1]}/work", json=bench)
                done_id = done.json()["run_id"]
                done_team = (await client.get(f"/v1/runs/{done_id}/team")).json()
                burst_ms: list[float] = []
                burst_codes: list[int | str] = []
                gate = asyncio.Semaphore(4)

                async def beside_reader() -> None:
                    async with gate:
                        start = perf_counter()
                        try:
                            response = await client.post(
                                "/v1/work", json=h.work_body("SDK 출시일이 언제야?")
                            )
                            burst_codes.append(response.status_code)
                        except httpx.HTTPError as exc:
                            burst_codes.append(type(exc).__name__)
                        burst_ms.append(ms_since(start))

                await asyncio.gather(*(beside_reader() for _ in range(BURST)))
                server_alive_after_burst = server.process.poll() is None
            a.check(
                "pre_kill_runs_completed",
                query.json()["status"] == "completed" and done.json()["status"] == "completed",
            )
            a.check(
                "concurrent_requests_beside_a_second_sqlite_process_all_succeed",
                reader.alive() and server_alive_after_burst and burst_codes == [201] * BURST,
            )
            tried, interrupted, kill_point = await interrupt_two_team_runs(
                server, db, bench, research
            )
        finally:
            server.kill()
        a.observe("kill_attempts", tried)
        a.check("sigkill_landed_while_a_role_was_running", bool(kill_point))
        mid_role = sorted({run for run, _ in kill_point})
        after_kill = {run: roles_of(db, run) for run in interrupted}
        threads = {
            run: db_rows(
                db,
                "SELECT s.thread_id FROM runs r JOIN sessions s "
                "ON s.session_id=r.session_id WHERE r.run_id=?",
                run,
            )[0][0]
            for run in interrupted
        }
        checkpoints = root / "rfa.db.checkpoints.sqlite"
        saved = {
            run: db_rows(checkpoints, "SELECT count(*) FROM checkpoints WHERE thread_id=?", thread)[
                0
            ][0]
            for run, thread in threads.items()
        }
        a.check(
            "checkpoint_saved_before_the_kill",
            bool(mid_role) and all(saved[run] > 0 for run in mid_role),
        )
        a.observe("kill_point_roles", sorted(role for _, role in kill_point))

        server.start()
        restart_start = perf_counter()
        try:
            restart_ready_ms = await server.wait_ready()
            reader_alive_across_restart = reader.alive()
            async with httpx.AsyncClient(base_url=server.base_url, timeout=60) as client:
                start = perf_counter()
                listed = {s["session_id"] for s in (await client.get("/v1/sessions")).json()}
                statuses = {
                    run: (await client.get(f"/v1/runs/{run}/status")).json()["status"]
                    for run in [*interrupted, done_id]
                }
                state_ms = ms_since(start)
                after_team = (await client.get(f"/v1/runs/{done_id}/team")).json()
                a.check("sessions_survive_the_restart", set(sessions) <= listed)
                a.check(
                    "interrupted_runs_shown_running_never_done",
                    all(statuses[run] == "running" for run in interrupted)
                    and statuses[done_id] == "completed",
                )
                a.check("completed_team_receipt_unchanged", after_team == done_team)
                resumed_run = mid_role[0]
                cancelled_run = next(run for run in interrupted if run != resumed_run)
                start = perf_counter()
                resumed = await client.post(
                    f"/v1/runs/{resumed_run}/resume", json={"event_id": f"e2e10-resume-{repeat}"}
                )
                resume_ms = ms_since(start)
                a.check(
                    "resume_of_the_interrupted_run_ends_without_success",
                    resumed.status_code == 200
                    and resumed.json()["status"] in {"failed", "outcome_unknown"},
                )
                start = perf_counter()
                cancelled = await client.post(f"/v1/runs/{cancelled_run}/cancel")
                cancel_ms = ms_since(start)
                again = await client.post(
                    f"/v1/runs/{cancelled_run}/resume",
                    json={"event_id": f"e2e10-after-cancel-{repeat}"},
                )
                record = (await client.get(f"/v1/runs/{cancelled_run}")).json()
                a.check(
                    "recovered_run_cancelled_durably",
                    cancelled.status_code == 200
                    and cancelled.json()["state"] == "cancelled"
                    and record["status"] == "cancelled"
                    and again.json()["status"] == "cancelled",
                )
                fresh = await client.post(
                    f"/v1/sessions/{sessions[0]}/work", json=h.work_body("SDK 설치 절차 알려줘")
                )
                a.check(
                    "other_session_served_after_recovery",
                    fresh.status_code == 201 and fresh.json()["status"] == "completed",
                )
        finally:
            code = server.terminate()
        reader_stats = reader.stop()
        a.check(
            "graceful_shutdown_on_sigterm",
            code in {0, -15} and "Application shutdown complete" in server.log_tail(),
        )
        faults = h.sqlite_fault_lines(server.log_tail(10_000_000))
        a.check(
            "second_sqlite_process_ran_through_kill_and_restart",
            reader_alive_across_restart
            and reader_stats["exit_code"] == 0
            and reader_stats["reads"] > 0,
        )
        a.check(
            "no_disk_io_error_or_sigbus_beside_the_second_process",
            faults == []
            and not any(
                marker in error
                for error in reader_stats["errors"]
                for marker in ("disk I/O", "malformed")
            ),
        )
        a.observe("second_sqlite_process", reader_stats)
        final = {run: roles_of(db, run) for run in interrupted}
        a.check(
            "interrupted_roles_became_unknown_and_were_never_rerun",
            all(
                [role for role, _ in final[run]] == [role for role, _ in after_kill[run]]
                and {status for _, status in final[run]} <= {"succeeded", "unknown", "cancelled"}
                and ("unknown" in {status for _, status in final[run]}) == (run in mid_role)
                for run in interrupted
            ),
        )
        a.check(
            "no_role_left_running",
            db_rows(db, "SELECT count(*) FROM role_executions WHERE status='running'") == [(0,)],
        )
        a.measure("first_start_to_ready", [first_ready_ms])
        a.measure(
            "work_beside_second_sqlite_process",
            burst_ms,
            note=f"{BURST} concurrent /v1/work (4 in flight) while another process reads",
        )
        a.measure(
            "restart_to_ready",
            [restart_ready_ms],
            note="process spawn after SIGKILL until /readyz 200",
        )
        a.measure(
            "ready_to_state_query",
            [state_ms],
            target_seconds=h.TARGETS["restart_health_to_state_query"],
        )
        a.measure("resume_interrupted_run", [resume_ms])
        a.measure(
            "cancel_recovered_run", [cancel_ms], target_seconds=h.TARGETS["supported_tool_cancel"]
        )
        a.observe("restart_wall_ms", round(ms_since(restart_start), 1))


@repeats
async def test_e2e10_scheduler_restart_coalesces_missed_fires_and_passes_two_fire_times(
    tmp_path, recorder, repeat
):
    root = tmp_path / "stack"
    fire = h.CLOCK.astimezone(UTC)
    clock = ManualClock(fire - timedelta(minutes=5))
    with recorder.attempt("E2E-10", "schedule-restart-coalescing", repeat=repeat) as a:
        async with h.open_stack(root, scheduler_enabled=True, **PINNED) as stack:
            a.bind(stack)
            await h.ingest(stack, ["R05", "R06"])
            async with stack.http() as client:
                created = (
                    await client.post(
                        "/v1/schedules",
                        json={
                            "job_type": "candidate_scan",
                            "domain_id": h.DOMAIN,
                            "cron": "0 9 * * *",
                            "timezone": "Asia/Seoul",
                        },
                    )
                ).json()
            runner = build_scheduler_runner(
                stack.settings, stack.container, clock=clock, sync_interval_seconds=None
            )
            await runner.start()
            clock.set(fire)
            await runner.tick()
            await runner.stop()  # the scheduler process stops
        clock.set(fire + timedelta(days=2, minutes=10))  # 10-02 and 10-03 09:00 missed
        async with h.open_stack(
            root, bind_owner=False, scheduler_enabled=True, **PINNED
        ) as restarted:
            runner = build_scheduler_runner(
                restarted.settings, restarted.container, clock=clock, sync_interval_seconds=None
            )
            await runner.start()
            try:
                start = perf_counter()
                await runner.tick()
                recover_ms = ms_since(start)
                await runner.tick()
                clock.set(fire + timedelta(days=3))  # the next regular fire time
                await runner.tick()
                await runner.tick()
            finally:
                await runner.stop()
            async with restarted.http() as client:
                listed = (await client.get("/v1/schedules")).json()
                runs = (await client.get(f"/v1/schedules/{created['schedule_id']}/runs")).json()
        fired = [r["scheduled_fire_time"] for r in runs]
        expected = [fire, fire + timedelta(days=2), fire + timedelta(days=3)]
        a.check(
            "schedule_survives_the_restart",
            [s["schedule_id"] for s in listed] == [created["schedule_id"]],
        )
        a.check(
            "missed_fires_coalesce_then_the_next_fire_runs_once",
            fired == [value.isoformat().replace("+00:00", "Z") for value in expected]
            and all(r["status"] == "succeeded" for r in runs),
        )
        a.measure("restart_to_coalesced_run", [recover_ms])
