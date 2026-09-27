"""Task-team overview tab: composition, linked KB, cited evidence. Synthetic data only."""

import sqlite3

from test_chat_poc import send, stack
from test_chat_routing import prepare

from rfa_mas.contracts import KnowledgeDelete


async def note(client, domain, title, content, key):
    response = await client.post(
        "/ui/api/notes",
        json={"domain_id": domain, "title": title, "content": content},
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 201, response.text
    return response.json()["document"]["source_id"]


async def test_overview_lists_owner_teams_with_composition_and_subject_matches(tmp_path):
    async with stack(tmp_path) as (app, c):
        container = app.state.container
        assert (await c.get("/ui/api/teams")).json() == []
        mine = await prepare(container, "아틀라스 research 자료 조사", "atlas")
        atlas = await note(c, "triv3", "아틀라스 개요", "아틀라스 프로젝트 합성 메모", "n-atlas")
        await note(c, "triv3", "무관한 메모", "다른 주제의 합성 메모", "n-other")
        foreign = await note(c, "quantization_research", "아틀라스 양자화", "다른 공간", "n-q")
        # A persisted Task owned by someone else must never appear (owner registry wins).
        with sqlite3.connect(container.repository.path) as db:
            db.execute(
                "INSERT INTO product_task_owners VALUES ('foreign-task','other-synthetic','triv3')"
            )
            db.execute(
                "INSERT INTO product_tasks SELECT 'foreign-task', task_json FROM product_tasks"
            )
            db.execute(
                "INSERT INTO team_slots SELECT 'foreign-task', 'foreign-team', generation, phase,"
                " request_fingerprint, lifecycle_json FROM team_slots"
            )
        teams = (await c.get("/ui/api/teams")).json()
        assert [t["task"]["task_id"] for t in teams] == [mine.task.task_id]
        view = teams[0]
        assert view["team"]["pattern"] == "research" and view["team"]["state"] == "ready"
        assert view["team"]["runtime_kind"] == "local" and view["team"]["mode"] == "local"
        assert [m["role"] for m in view["team"]["members"]] == [
            "supervisor",
            "source_scout",
            "evidence_reviewer",
        ]
        assert all(m["prepare"] == "prepared" for m in view["team"]["members"])
        assert view["team"]["budget"]["max_steps"] >= 1 and view["selectable"] is True
        assert view["runs"] == {"count": 0, "recent": []}
        assert view["linked_sources"] == [] and view["cited_sources"] == []
        # Only same-domain sources sharing a distinctive goal subject are suggested.
        assert [m["source_id"] for m in view["subject_matches"]] == [atlas]
        assert view["subject_matches"][0]["shared"] == ["아틀라스"]
        assert foreign not in [m["source_id"] for m in view["subject_matches"]]
        assert (await c.get("/ui/api/teams/foreign-task")).status_code == 404
        assert (await c.get("/ui/api/teams/missing")).status_code == 404


async def test_link_unlink_validation_and_cited_evidence(tmp_path):
    async with stack(tmp_path) as (app, c):
        container = app.state.container
        mine = await prepare(container, "아틀라스 research 자료 조사", "atlas")
        task_id = mine.task.task_id
        atlas = await note(c, "triv3", "아틀라스 개요", "아틀라스 프로젝트 합성 메모", "n-atlas")
        other = await note(c, "triv3", "무관한 메모", "다른 주제의 합성 메모", "n-other")
        foreign = await note(c, "quantization_research", "아틀라스 양자화", "다른 공간", "n-q")
        # Cross-domain, unknown, foreign task and CSRF-less requests are rejected.
        for source_id in (foreign, "source_missing"):
            rejected = await c.post(f"/ui/api/teams/{task_id}/kb", json={"source_ids": [source_id]})
            assert rejected.status_code == 409, rejected.text
            assert rejected.json()["details"]["upstream_code"] == "source_not_linkable"
        assert (
            await c.post("/ui/api/teams/foreign-task/kb", json={"source_ids": [atlas]})
        ).status_code == 404
        assert (
            await c.post(f"/ui/api/teams/{task_id}/kb", json={"source_ids": ["bad id"]})
        ).status_code == 404
        no_csrf = await c.post(
            f"/ui/api/teams/{task_id}/kb", json={"source_ids": [atlas]}, headers={"X-RFA-CSRF": ""}
        )
        assert no_csrf.status_code == 403
        linked = (
            await c.post(
                f"/ui/api/teams/{task_id}/kb",
                json={"source_ids": [atlas, other, atlas], "note": "seed"},
            )
        ).json()
        assert [s["source_id"] for s in linked["linked_sources"]] == [atlas, other]
        assert linked["linked_sources"][0]["title"] == "아틀라스 개요"
        assert linked["linked_sources"][0]["available"] is True
        assert linked["subject_matches"] == []  # linked sources leave the suggestion list
        # Re-linking is idempotent.
        again = (await c.post(f"/ui/api/teams/{task_id}/kb", json={"source_ids": [atlas]})).json()
        assert len(again["linked_sources"]) == 2
        # A task-routed run records the evidence its roles actually cited.
        sid = (await c.post("/ui/api/sessions", json={})).json()["session_id"]
        answer = await send(c, sid, "아틀라스 research 개요 알려줘", "ask")
        assert answer["route"]["task_id"] == task_id and answer["run_id"]
        view = (await c.get(f"/ui/api/teams/{task_id}")).json()
        assert view["runs"]["count"] == 1
        recent = view["runs"]["recent"][0]
        assert recent["run_id"] == answer["run_id"] and recent["simulated"] is True
        assert {r["role"] for r in recent["roles"]} == {
            "supervisor",
            "source_scout",
            "evidence_reviewer",
        }
        cited = {s["source_id"]: s for s in view["cited_sources"]}
        assert atlas in cited and cited[atlas]["title"] == "아틀라스 개요"
        assert "source_scout" in cited[atlas]["roles"]
        # Unlink, then a deleted source shows as unavailable instead of replaying stale data.
        after = (await c.delete(f"/ui/api/teams/{task_id}/kb/{other}")).json()
        assert [s["source_id"] for s in after["linked_sources"]] == [atlas]
        owner = await container.repository.local_principal()
        current = await container.knowledge.get(atlas, owner)
        await container.knowledge.delete(
            atlas,
            KnowledgeDelete(
                expected_revision=current.document.source_revision,
                mutation_id="delete-overview-fixture",
            ),
            owner,
        )
        gone = (await c.get(f"/ui/api/teams/{task_id}")).json()
        assert gone["linked_sources"][0] == gone["linked_sources"][0] | {
            "available": False,
            "title": None,
        }
