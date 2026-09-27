"""Demo seed: synthetic KB + two Task teams + links, idempotent through the UI API only."""

import pytest
from test_chat_poc import send, stack
from test_chat_routing import prepare

from rfa_mas.poc.seed import DEFAULT_FIXTURE, load_fixture, run, seed


async def test_seed_creates_two_linked_task_teams_and_is_idempotent(tmp_path):
    fixture = load_fixture(DEFAULT_FIXTURE)
    async with stack(tmp_path) as (app, c):
        first = await seed(c, fixture)
        assert first["notes_before"] == 0 and len(first["notes"]) == len(fixture["notes"]) == 14
        # Team runs may add derived knowledge on top of the seeded notes.
        assert first["notes_after"] >= 14
        assert all(n["audience"] == "private" for n in first["notes"])
        by_key = {t["key"]: t for t in first["tasks"]}
        aurora = by_key["poc-demo-seed-v2-task-aurora"]
        nebula = by_key["poc-demo-seed-v2-task-nebula"]
        assert aurora["created"] and aurora["pattern"] == "benchmark"
        assert aurora["simulated"] is True
        assert aurora["members"] == [
            "supervisor",
            "paper_scout",
            "experiment_runner",
            "result_analyst",
        ]
        assert nebula["created"] and nebula["pattern"] == "research"
        assert nebula["members"] == ["supervisor", "source_scout", "evidence_reviewer"]
        assert aurora["team_state"] == nebula["team_state"] == "ready"
        # Explicit fixture links plus same-domain subject matches, never cross-domain.
        teams = {t["task"]["task_id"]: t for t in (await c.get("/ui/api/teams")).json()}
        assert set(teams) == {aurora["task_id"], nebula["task_id"]}
        for entry in (aurora, nebula):
            view = teams[entry["task_id"]]
            assert len(view["linked_sources"]) == entry["linked_sources"] >= 6
            assert {s["domain_id"] for s in view["linked_sources"]} == {view["task"]["domain_id"]}
            assert view["subject_matches"] == []
            assert view["runs"]["count"] == 1 and view["cited_sources"]
        # The benchmark team compared the two verified logs and excluded the tentative one.
        container = app.state.container
        owner = await container.repository.local_principal()
        result = await container.service.team_result(aurora["run_id"], owner)
        analyst = result.findings["result_analyst"]
        pairs = [(item["baseline"], item["candidate"]) for item in analyst["comparisons"]]
        assert pairs == [("A1", "B2")]
        assert analyst["comparisons"][0]["latency_change_pct"] < 0
        assert analyst["unverified"] == ["C3"]
        reviewed = (await container.service.team_result(nebula["run_id"], owner)).findings
        states = {r["epistemic_state"] for r in reviewed["evidence_reviewer"]["reviewed"]}
        assert states == {"cited", "tentative"}
        # Second run: no new notes, no new Task teams, same links.
        second = await seed(c, fixture)
        assert second["notes_after"] == first["notes_after"]
        assert not any(t["created"] for t in second["tasks"])
        assert {t["task_id"] for t in second["tasks"]} == {aurora["task_id"], nebula["task_id"]}
        assert len((await c.get("/ui/api/teams")).json()) == 2
        # Later related questions route to the seeded Task teams automatically.
        sid = (await c.post("/ui/api/sessions", json={})).json()["session_id"]
        asked = await send(c, sid, "오로라 벤치마크 회귀 기준 알려줘", "q1")
        assert asked["route"]["kind"] == "task" and asked["route"]["task_id"] == aurora["task_id"]
        asked = await send(c, sid, "네뷸라 실험 계획 알려줘", "q2")
        assert asked["route"]["kind"] == "task" and asked["route"]["task_id"] == nebula["task_id"]


async def test_seed_reuses_owner_task_that_already_covers_the_subject(tmp_path):
    fixture = load_fixture(DEFAULT_FIXTURE)
    async with stack(tmp_path) as (app, c):
        existing = await prepare(
            app.state.container, "오로라 지연 벤치마크 결과 research", "aurora-existing"
        )
        report = await seed(c, fixture)
        aurora = next(t for t in report["tasks"] if t["key"].endswith("aurora"))
        assert aurora["created"] is False
        assert aurora["reused_via"] == "existing_task_subject_match"
        assert aurora["task_id"] == existing.task.task_id and aurora["linked_sources"] >= 6
        nebula = next(t for t in report["tasks"] if t["key"].endswith("nebula"))
        assert nebula["created"] is True and nebula["pattern"] == "research"
        teams = {t["task"]["task_id"]: t for t in (await c.get("/ui/api/teams")).json()}
        assert set(teams) == {existing.task.task_id, nebula["task_id"]}
        assert teams[nebula["task_id"]]["task"]["domain_id"] == "quantization_research"


async def test_seed_cli_rejects_non_loopback(tmp_path):
    with pytest.raises(ValueError):
        await run("http://example.com:8780", DEFAULT_FIXTURE)
