"""E2E-10 steady-state load: n=100 controlled requests against the real app process.

Runs the product's `rfa api` entrypoint as a child process on loopback (mock/local
adapters, synthetic data) and measures client-observed latency per request. p50/p95 come
from n=100; the fault/restart interval is measured separately in test_process_recovery.py.
Queue time is not observable at the API and is reported as null, never 0.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from datetime import datetime
from time import perf_counter

import fixture_pack as pack
import harness as h
import httpx

SMALL20 = pack.set_members("small20")
LOAD = h.CONFIG["sampling"]["load_requests"]
BUDGET = h.CONFIG["budgets"]
REQUESTS = pack.read_json("scenarios.json")["scenarios"]["E2E-03"]["requests"]
SERVER_PINNED = {
    "MAX_GRAPH_STEPS": str(BUDGET["team"]["max_steps"]),
    "MAX_TOOL_CALLS": str(BUDGET["team"]["max_tool_calls"]),
    "TOOL_TIMEOUT_SECONDS": str(BUDGET["team"]["timeout_seconds"]),
}
QUESTIONS = [q["question"] for q in pack.golden() if q["answerable"]]
CONCURRENCY = 4


def ms_since(start: float) -> float:
    return (perf_counter() - start) * 1000


def max_overlap(intervals: list[tuple[datetime, datetime]]) -> int:
    points = sorted(
        [(s, 1) for s, _ in intervals] + [(e, -1) for _, e in intervals],
        key=lambda item: (item[0], item[1]),
    )
    active = peak = 0
    for _, delta in points:
        active += delta
        peak = max(peak, active)
    return peak


def plan(index: int) -> tuple[str, str]:
    """Deterministic mix: 50% grounded answers, 50% reads."""
    slot = index % 10
    if slot < 5:
        return "answer", QUESTIONS[index % len(QUESTIONS)]
    return {5: "sessions", 6: "sessions", 7: "sources", 8: "run_status", 9: "session"}[slot], ""


async def test_e2e10_steady_load_n100_and_global_worker_cap(tmp_path, recorder, network_guard):
    network_guard.allow_loopback()
    root = (tmp_path / "data").resolve()
    with recorder.attempt("E2E-10", "steady-load-n100") as a:
        async with h.open_stack(root) as stack:
            await h.ingest(stack, SMALL20)
        server = h.LocalServer(root, **SERVER_PINNED)
        server.start()
        samples: list[dict[str, float]] = []
        try:
            await server.wait_ready()
            limits = httpx.Limits(max_connections=CONCURRENCY * 2)
            async with httpx.AsyncClient(
                base_url=server.base_url, timeout=30, limits=limits
            ) as client:
                session = (await client.post("/v1/sessions")).json()["session_id"]
                seeded = (
                    await client.post(
                        f"/v1/sessions/{session}/work", json=h.work_body(QUESTIONS[0])
                    )
                ).json()
                for question in QUESTIONS[:5]:  # warm-up, excluded from the sample
                    await client.post("/v1/work", json=h.work_body(question))
                idle = server.resources()
                results: list[dict] = []
                gate = asyncio.Semaphore(CONCURRENCY)
                stop = asyncio.Event()

                async def sample() -> None:
                    while not stop.is_set():
                        if (value := server.resources()) is not None:
                            samples.append(value)
                        await asyncio.sleep(0.2)

                async def one(index: int) -> None:
                    kind, question = plan(index)
                    async with gate:
                        start = perf_counter()
                        try:
                            if kind == "answer":
                                response = await client.post("/v1/work", json=h.work_body(question))
                            elif kind == "sessions":
                                response = await client.get("/v1/sessions")
                            elif kind == "sources":
                                response = await client.get(
                                    h.NOTE_ROUTE, params={"domain_id": h.DOMAIN}
                                )
                            elif kind == "run_status":
                                response = await client.get(f"/v1/runs/{seeded['run_id']}/status")
                            else:
                                response = await client.get(f"/v1/sessions/{session}")
                            outcome = response.status_code
                        except httpx.TimeoutException:
                            outcome = "timeout"
                        except httpx.HTTPError:
                            outcome = "transport_error"
                        results.append({"kind": kind, "ms": ms_since(start), "outcome": outcome})

                sampler = asyncio.create_task(sample())
                start = perf_counter()
                await asyncio.gather(*(one(i) for i in range(LOAD)))
                wall_ms = ms_since(start)
                stop.set()
                await sampler
                residual = server.resources()

                # Separate phase: two Task teams plus two chats at once (global worker cap).
                sessions = [
                    (await client.post("/v1/sessions")).json()["session_id"] for _ in range(4)
                ]
                bench = h.team_body(
                    REQUESTS["benchmark"], REQUESTS["benchmark"], ("benchmark_report",)
                )
                research = h.team_body(
                    REQUESTS["research"], REQUESTS["research"], ("research_report",)
                )
                start = perf_counter()
                mixed = await asyncio.gather(
                    client.post(f"/v1/sessions/{sessions[0]}/work", json=bench),
                    client.post(f"/v1/sessions/{sessions[1]}/work", json=research),
                    client.post(f"/v1/sessions/{sessions[2]}/work", json=h.work_body(QUESTIONS[1])),
                    client.post(f"/v1/sessions/{sessions[3]}/work", json=h.work_body(QUESTIONS[2])),
                )
                mixed_ms = ms_since(start)
                team_runs = [mixed[0].json()["run_id"], mixed[1].json()["run_id"]]
        finally:
            code = server.terminate()
        # Read the durable role receipts only after the server stopped.
        with contextlib.closing(sqlite3.connect(root / "rfa.db")) as db:
            rows = db.execute(
                "SELECT run_id, started_at, finished_at FROM role_executions WHERE run_id IN "
                "(?, ?)",
                team_runs,
            ).fetchall()
        intervals = [
            (datetime.fromisoformat(s), datetime.fromisoformat(f)) for _, s, f in rows if s and f
        ]
        outcomes = [r["outcome"] for r in results]
        ok = sum(isinstance(o, int) and 200 <= o < 300 for o in outcomes)
        rejected = sum(o in {409, 429, 503} for o in outcomes)
        a.check("all_100_requests_answered_2xx", len(results) == LOAD and ok == LOAD)
        a.check(
            "mixed_teams_and_chats_complete",
            [r.status_code for r in mixed] == [201] * 4
            and all(r.json()["status"] == "completed" for r in mixed),
        )
        a.check(
            "global_active_workers_within_cap",
            len(intervals) == len(rows) > 0
            and max_overlap(intervals) <= BUDGET["load_total_active_workers"],
        )
        a.check("server_shut_down_cleanly", code in {0, -15})
        a.measure(
            "request_latency_all",
            [r["ms"] for r in results],
            target_seconds=h.TARGETS["ack_first_progress"],
            statistic="p95",
            note="full synchronous completion, stricter than the ACK/first-progress target",
        )
        for kind in sorted({r["kind"] for r in results}):
            a.measure(f"request_latency_{kind}", [r["ms"] for r in results if r["kind"] == kind])
        # /v1/work shows nothing before completion, so its completion is the first progress
        # the user sees: the ACK/first-progress target applies to the answer subset as well.
        a.measure(
            "request_latency_answer_vs_ack_target",
            [r["ms"] for r in results if r["kind"] == "answer"],
            target_seconds=h.TARGETS["ack_first_progress"],
            statistic="p95",
            note="grounded answers (mock model) under concurrency 4; no async ACK exists",
        )
        a.measure(
            "two_teams_two_chats_concurrent", [mixed_ms], target_seconds=h.TARGETS["small_team_run"]
        )
        a.observe(
            "load",
            {
                "n": LOAD,
                "concurrency": CONCURRENCY,
                "success": ok,
                "error": sum(isinstance(o, int) and o >= 500 for o in outcomes),
                "timeout": outcomes.count("timeout"),
                "transport_error": outcomes.count("transport_error"),
                "rejected": rejected,
                "throughput_rps": round(LOAD / (wall_ms / 1000), 2),
                "wall_ms": round(wall_ms, 1),
                "queue_ms": None,
                "tokens": None,
            },
        )
        a.observe(
            "server_resources",
            {
                "idle": idle,
                "rss_peak_mb": max((s["rss_mb"] for s in samples), default=None),
                "cpu_peak_pct": max((s["cpu_pct"] for s in samples), default=None),
                "residual_after_load": residual,
                "samples": len(samples),
            },
        )
        a.observe(
            "global_worker_overlap",
            {
                "max_active_roles": max_overlap(intervals),
                "cap": BUDGET["load_total_active_workers"],
                "role_receipts": len(rows),
            },
        )
