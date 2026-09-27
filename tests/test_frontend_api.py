"""Front-end contract groundwork: /agents, /me, /tasks (routes → services → store), the SSE envelope
schema, and docs/openapi.yaml being current."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from rfa_mas.nemoclaw import config as cfg
from rfa_mas.nemoclaw.ask import build_fake_deps
from rfa_mas.nemoclaw.ask_api import AskService, create_ask_app
from rfa_mas.nemoclaw.schemas import SseEnvelope
from rfa_mas.nemoclaw.services import build_frontend_services
from rfa_mas.nemoclaw.store import Store

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("RFA_SG_AUDIT_DB", str(tmp_path / "audit.db"))
    deps = build_fake_deps(cfg.load_ask(), cfg.load_censors(), tmp_path / "learned.yaml")
    assignments = cfg.load_assignments(teams_path=tmp_path / "no-teams.yaml")  # static declaration only
    service = AskService(deps, "tok")
    frontend = build_frontend_services(assignments=lambda: assignments, ask_deps=deps, token="tok",
                                       store=Store(tmp_path / "frontend.db"), ask_service=service)
    return create_ask_app(service, frontend=frontend)


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://entry")


async def test_agents_are_the_assistant_plus_one_agent_per_task(app):
    async with client(app) as c:
        r = await c.get("/agents")
    assert r.status_code == 200
    agents = {a["id"]: a for a in r.json()}
    assert set(agents) == {"assistant", "triv3", "quantization_research"}  # D-10: one task = one partner, no censor
    a = agents["assistant"]
    assert a["kind"] == "assistant" and a["name"] == "비서 에이전트" and a["icon"] == "star" and len(a["suggestions"]) == 4
    t = agents["triv3"]
    assert t["kind"] == "task" and t["taskId"] == "triv3" and t["taskName"] == "TRIV3 벤치마크 (오로라)"
    assert t["name"] == "TRIV3 벤치마크 에이전트" and t["desk"] == "triv3-desk" and t["status"] == "running"
    assert t["color"] == {"bg": "#d5eede", "fg": "#14532d"} and t["initials"] == "TR" and t["itemCount"] == 0
    assert t["suggestions"] and not any(k in t for k in ("level", "grade", "sandbox"))  # grades are per request


async def test_me_is_owner_only_with_the_bearer(app):
    async with client(app) as c:
        guest = (await c.get("/me")).json()
        wrong = (await c.get("/me", headers={"Authorization": "Bearer nope"})).json()
        owner = (await c.get("/me", headers={"Authorization": "Bearer tok"})).json()
    assert guest == wrong == {"authenticated": False, "role": "guest", "isAdmin": False, "name": "게스트"}
    assert owner == {"authenticated": True, "role": "owner", "isAdmin": True, "name": "소유자"}


async def test_tasks_list_the_catalog_for_the_left_nav(app):
    async with client(app) as c:
        r = await c.get("/tasks")
        one = await c.get("/tasks/triv3")
        missing = await c.get("/tasks/nope")
    assert r.status_code == 200
    tasks = {t["id"]: t for t in r.json()}
    assert list(tasks) == ["triv3", "quantization_research"]
    t = tasks["triv3"]
    assert t["agentId"] == "triv3" and t["worker"] == "research" and t["desk"] == "triv3-desk"
    assert t["status"] == "ready" and t["source"] == "catalog" and t["sandbox"] == "rfa-main"
    assert t["securityLabel"].startswith("L2 ") and "gradeLabel" not in t
    assert one.json() == t
    assert missing.status_code == 404 and missing.json()["code"] == "unknown_task"


def test_presentation_rules_match_the_ui():
    from rfa_mas.nemoclaw.services.presentation import (
        clean_tags,
        default_suggestions,
        desk_for,
        initials_for,
    )

    assert desk_for("Jira 이슈 대응 에이전트", "x") == "jira-desk" and desk_for("회계 에이전트", "task-1") == "task-1-desk"
    assert initials_for("Jira 이슈 대응 에이전트") == "JI" and initials_for("GitHub Issue 에이전트") == "GI"
    assert initials_for("회계 담당 에이전트") == "회계"
    assert clean_tags(["#jira", "버그, #jira", " ", "a,b,c,d,e,f,g,h,i"]) == ["jira", "버그", "a", "b", "c", "d", "e", "f"]
    assert default_suggestions("Jira 이슈", [])[1] == "Jira 이슈에서 결재가 필요한 안건 있어?"
    assert default_suggestions("Jira 이슈", ["jira"])[1] == "jira 관련 문의가 있었어?"


def test_store_migrates_an_old_tasks_table(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, name TEXT NOT NULL, agent_id TEXT NOT NULL, sandbox TEXT, "
                 "grade_label TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL)")
    conn.execute("INSERT INTO tasks VALUES ('x', 'X', 'a', NULL, '', 1, 1)")
    conn.commit()
    conn.close()
    store = Store(path)
    row = store.one("SELECT * FROM tasks WHERE id='x'")
    assert row["status"] == "ready" and row["tags"] == "[]" and row["agent_name"] == ""


async def read_sse(c, body, headers=None):
    events = []
    async with c.stream("POST", "/chat", json=body, headers=headers or {}) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        buf = ""
        async for chunk in r.aiter_text():
            buf += chunk
            while "\n\n" in buf:
                block, buf = buf.split("\n\n", 1)
                data = [ln[6:] for ln in block.splitlines() if ln.startswith("data: ")][0]
                events.append(json.loads(data))
    return events


OWNER = {"Authorization": "Bearer tok"}
TRIV3_Q = "오로라 TRIV3 벤치마크 최신 결과 요약해줘"


def of(events, kind):
    return [e["data"] for e in events if e["type"] == kind]


def answer(events):
    return "".join(d["text"] for d in of(events, "answer.delta"))


async def test_chat_owner_single_agent_streams_the_ui_protocol(app):
    async with client(app) as c:
        events = await read_sse(c, {"text": TRIV3_Q, "role": "owner"}, {**OWNER, "X-Request-Id": "req-sse-1"})
    types = [e["type"] for e in events]
    assert types[:4] == ["run.start", "assistant.delta", "agents.search", "agents.select"]
    assert types[-1] == "run.end" and types.index("guard.start") < types.index("guard.end") < types.index("answer.delta")
    assert [e["seq"] for e in events] == list(range(len(events))) and len({e["runId"] for e in events}) == 1
    assert set(events[0]) == {"type", "runId", "seq", "ts", "data"}
    start = events[0]["data"]
    assert start["role"] == "owner" and start["level"] == "company" and start["requestId"] == "req-sse-1"
    assert of(events, "assistant.delta")[0]["text"] == "TRIV3 벤치마크 에이전트에게 확인하겠습니다."
    search = of(events, "agents.search")[0]
    assert [c["agentId"] for c in search["candidates"]] == ["triv3"] and search["candidates"][0]["score"] >= 0.9
    assert [s["agentId"] for s in of(events, "agents.select")[0]["selected"]] == ["triv3"]
    end = of(events, "delegate.end")[0]
    assert end["callId"] == "c1" and end["status"] == "ok" and end["durationMs"] >= 0 and end["refs"] == []
    assert "응답 수신" not in end["summary"]  # the owner sees the reply's first sentence
    assert of(events, "guard.start")[0] == {"level": "company"} and of(events, "guard.end")[0]["status"] in ("pass", "redacted")
    assert answer(events) and of(events, "run.end")[0]["status"] == "done"


async def test_chat_guest_is_graded_public_and_sees_no_raw_progress(app):
    async with client(app) as c:
        events = await read_sse(c, {"text": TRIV3_Q, "role": "guest"})
    assert events[0]["data"]["role"] == "guest" and of(events, "guard.start")[0] == {"level": "public"}
    guard = of(events, "guard.end")[0]
    assert guard["status"] == "redacted" and guard["redactions"] >= 1 and "(" not in guard["note"]  # no rule names
    assert of(events, "delegate.end")[0]["summary"].startswith("응답 수신 · ")  # D-14: fixed phrases only
    assert all(d["text"] in ("담당 에이전트에 위임", "근거 없음", "실패") or d["text"].startswith("응답 수신 · ")
               for d in of(events, "delegate.log"))
    text = answer(events)
    assert "[REDACTED:" in text and "오로라" not in text


async def test_chat_delegates_to_every_agent_over_the_threshold_and_merges(app):
    async with client(app) as c:
        events = await read_sse(c, {"text": "오로라 TRIV3 벤치마크와 네뷸라 INT4 양자화 결과를 같이 정리해줘", "role": "owner"}, OWNER)
    selected = [s["agentId"] for s in of(events, "agents.select")[0]["selected"]]
    assert sorted(selected) == ["quantization_research", "triv3"]
    assert sorted(d["callId"] for d in of(events, "delegate.start")) == ["c1", "c2"]
    assert of(events, "assistant.delta")[0]["text"].startswith("담당자 2명(")
    text = answer(events)
    assert "[TRIV3 벤치마크 에이전트]" in text and "[양자화 리서치 에이전트]" in text


async def test_chat_without_a_fitting_agent_answers_directly_and_does_not_invent(app):
    async with client(app) as c:
        events = await read_sse(c, {"text": "지난 분기 회식비 정산 내역 찾아줘", "role": "owner"}, OWNER)
    assert of(events, "agents.search")[0]["candidates"] == [] and of(events, "agents.select")[0]["selected"] == []
    assert not of(events, "delegate.start")
    assert of(events, "assistant.delta")[0]["text"] == "이 질문을 맡은 담당자가 없어 직접 답하겠습니다."
    assert "알 수 없습니다" in answer(events) and of(events, "run.end")[0]["status"] == "done"


async def test_chat_pinned_agent_skips_ranking(app):
    async with client(app) as c:
        events = await read_sse(c, {"text": "최신 결과 알려줘", "role": "owner", "agentId": "quantization_research"}, OWNER)
    cand = of(events, "agents.search")[0]["candidates"]
    assert cand == [{"agentId": "quantization_research", "reason": "지정한 담당자", "score": 1.0}]
    assert of(events, "delegate.start")[0]["agentId"] == "quantization_research"
    async with client(app) as c:
        bad = await read_sse(c, {"text": "안녕", "role": "owner", "agentId": "nope"}, OWNER)
    end = of(bad, "run.end")[0]
    assert end["status"] == "error" and end["code"] == "unknown_agent" and end["error"] == "없는 담당자입니다."


async def test_chat_agent_without_evidence_is_none_and_the_answer_says_unknown(app):
    from rfa_mas.nemoclaw.ask import TaskReply

    class NoEvidence:
        async def ask(self, agent, query, session_id, channel, task_id):
            return TaskReply("관련 근거를 찾을 수 없습니다.", True, {})

    app.state.frontend.chat.ask_service.deps.tasks = NoEvidence()
    async with client(app) as c:
        events = await read_sse(c, {"text": TRIV3_Q, "role": "owner"}, OWNER)
    assert of(events, "delegate.end")[0]["status"] == "none"
    assert "근거를 찾지 못했습니다" in answer(events) and "알 수 없습니다" in answer(events)


async def test_chat_empty_gateway_reply_is_a_failed_call_not_an_answer(app):
    from rfa_mas.nemoclaw.ask import TaskReply

    class Empty:
        async def ask(self, agent, query, session_id, channel, task_id):
            return TaskReply("No response from OpenClaw.", True, {"route": "gateway"})

    app.state.frontend.chat.ask_service.deps.tasks = Empty()
    async with client(app) as c:
        events = await read_sse(c, {"text": TRIV3_Q, "role": "owner"}, OWNER)
    assert of(events, "delegate.end")[0]["status"] == "error"
    text = answer(events)
    assert "No response" not in text and "응답을 받지 못했습니다" in text and "알 수 없습니다" in text


async def test_chat_guest_cannot_claim_owner(app):
    async with client(app) as c:
        claimed = await read_sse(c, {"text": "점심 뭐 먹지", "role": "owner"})  # no bearer → guest
    assert claimed[0]["data"]["role"] == "guest" and of(claimed, "guard.start")[0]["level"] == "public"


async def test_owner_conversations_are_stored_and_reopened(app):
    async with client(app) as c:
        created = (await c.post("/conversations", json={"agentId": "triv3"}, headers=OWNER))
        assert created.status_code == 201
        cid = created.json()["id"]
        assert created.json()["persisted"] is True and created.json()["title"] == "새 대화"
        await read_sse(c, {"text": TRIV3_Q, "role": "owner", "conversationId": cid, "agentId": "triv3"}, OWNER)
        listed = (await c.get("/conversations", params={"agentId": "triv3"}, headers=OWNER)).json()
        detail = (await c.get(f"/conversations/{cid}", headers=OWNER)).json()
        guest_list = (await c.get("/conversations")).json()
        guest_get = await c.get(f"/conversations/{cid}")
        guest_new = (await c.post("/conversations", json={"agentId": "assistant"})).json()
        unknown = await c.post("/conversations", json={"agentId": "nope"}, headers=OWNER)
    assert [x["id"] for x in listed] == [cid] and listed[0]["title"] == TRIV3_Q and listed[0]["count"] == 1
    assert listed[0]["busy"] is False and listed[0]["preview"]
    user, turn = detail["messages"]
    assert user["role"] == "user" and user["text"] == TRIV3_Q
    assert turn["role"] == "assistant" and turn["status"] == "done" and turn["answer"]
    assert turn["preface"] and turn["search"]["candidates"][0]["agentId"] == "triv3"
    assert turn["calls"][0]["status"] == "ok" and turn["calls"][0]["logs"] and turn["guard"]["level"] == "company"
    assert guest_list == [] and guest_get.status_code == 404
    assert guest_new["persisted"] is False and unknown.status_code == 404


async def test_chat_refuses_a_second_run_in_the_same_conversation(app):
    app.state.frontend.chat.running["c_busy"] = "run-x"
    async with client(app) as c:
        r = await c.post("/chat", json={"text": "안녕", "role": "owner", "conversationId": "c_busy"}, headers=OWNER)
        listed = await c.post("/conversations", json={"agentId": "assistant"}, headers=OWNER)
    assert r.status_code == 409 and r.json()["code"] == "conversation_busy"
    assert listed.status_code == 201


async def test_closing_the_stream_stops_the_run_and_stores_a_stopped_turn(app):
    svc = app.state.frontend
    cid = svc.conversations.create("triv3", "owner")["id"]
    stream = svc.chat.stream(text=TRIV3_Q, role="owner", conversation_id=cid, agent_id=None, history=[],
                             authorization="Bearer tok")
    kinds = [(await anext(stream))[0] for _ in range(3)]
    assert kinds == ["run.start", "assistant.delta", "agents.search"] and svc.chat.is_running(cid)
    await stream.aclose()  # what Starlette does when the client disconnects (the UI's 중지)
    turn = svc.conversations.get(cid)["messages"][-1]
    assert turn["status"] == "stopped" and not svc.chat.is_running(cid)


def test_turn_builder_matches_the_ui_reducer_on_stop():
    from rfa_mas.nemoclaw.services.conversations import TurnBuilder

    b = TurnBuilder("a_1")
    for kind, data in [("run.start", {"runId": "r"}), ("assistant.delta", {"text": "확인하겠습니다."}),
                       ("delegate.start", {"callId": "c1", "agentId": "triv3", "task": "q"}),
                       ("delegate.log", {"callId": "c1", "text": "위임"}), ("guard.start", {"level": "public"})]:
        b.apply(kind, data)
    turn = b.end("stopped", 1200)
    assert turn["status"] == "stopped" and turn["phase"] == "idle" and turn["calls"][0]["status"] == "error"
    assert turn["guard"] is None and turn["calls"][0]["logs"] == ["위임"] and turn["durationMs"] == 1200


def test_sentence_chunks_keep_numbers_and_newlines_together():
    from rfa_mas.nemoclaw.services.chat import sentences

    text = "지연 842 ms, 정확도 88.5 %. 합격 기준(지연 0.7초 이하) 미달!\n### 2. 결과\n* A2: 27.3 % 감소"
    chunks = sentences(text)
    assert "".join(chunks) == text
    assert chunks[0] == "지연 842 ms, 정확도 88.5 %." and chunks[1] == " 합격 기준(지연 0.7초 이하) 미달!\n"
    assert chunks[2] == "### 2. 결과\n" and chunks[3] == "* A2: 27.3 % 감소"


def test_store_creates_the_seven_tables(tmp_path):
    store = Store(tmp_path / "fe.db")
    names = {r["name"] for r in store.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"conversations", "messages", "threads", "instructions", "options", "rules", "blocklist", "tasks"} <= names


def test_sse_envelope_shape():
    env = SseEnvelope(type="delta", runId="run-1", seq=3, ts=1.5, data={"text": "안녕"})
    assert set(env.model_dump()) == {"type", "runId", "seq", "ts", "data"}
    with pytest.raises(ValueError):
        SseEnvelope(type="delta", runId="run-1", seq=-1, ts=1.5)


def test_committed_frontend_openapi_matches_generated():
    out = ROOT / ".local" / "openapi-check"
    subprocess.run([sys.executable, str(ROOT / "scripts" / "export_openapi.py"), "frontend", "--out", str(out / "api")],
                   capture_output=True, text=True, cwd=ROOT, check=True)
    generated = json.loads((out / "openapi.json").read_text(encoding="utf-8"))
    committed = json.loads((ROOT / "docs" / "openapi.json").read_text(encoding="utf-8"))
    assert generated == committed, "run `make openapi` and commit docs/openapi.*"
    assert {"/agents", "/me", "/tasks", "/ask", "/chat", "/chat/sync", "/teams"} <= set(committed["paths"])
    assert "text/event-stream" in committed["paths"]["/chat"]["post"]["responses"]["200"]["content"]


# ---- 태스크 추가 (POST /tasks, D-6) ---------------------------------------------------------------


@pytest.fixture
def team_app(tmp_path, monkeypatch):
    from rfa_mas.nemoclaw.teams import KeywordPatterner, TeamService

    monkeypatch.setenv("RFA_SG_AUDIT_DB", str(tmp_path / "audit.db"))
    teams = TeamService(roles=cfg.load_roles(), routing=cfg.load_routing(), ask_cfg=cfg.load_ask(),
                        patterner=KeywordPatterner(), teams_path=tmp_path / "teams.yaml",
                        manifests_dir=tmp_path / "agents", fake=True, secret=b"s" * 48)
    deps = build_fake_deps(cfg.load_ask(), cfg.load_censors(), tmp_path / "learned.yaml")
    deps.catalog = teams.catalog_tasks
    service = AskService(deps, "tok")
    frontend = build_frontend_services(assignments=teams.assignments, ask_deps=deps, token="tok",
                                       store=Store(tmp_path / "frontend.db"), ask_service=service, teams=teams)
    return create_ask_app(service, teams, frontend=frontend)


async def read_task_sse(c, body, headers=OWNER):
    events = []
    async with c.stream("POST", "/tasks", json=body, headers=headers) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        async for line in r.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


async def test_add_task_streams_analysis_design_and_spawning(team_app):
    async with client(team_app) as c:
        events = await read_task_sse(c, {"name": "Jira 이슈", "description": "Jira 이슈 문의에 사내 문서 근거를 찾아 답한다",
                                         "tags": ["#jira", "버그", "jira"]})
        tasks = (await c.get("/tasks")).json()
        agents = {a["id"]: a for a in (await c.get("/agents")).json()}
        one = (await c.get("/tasks/jira")).json()
    types = [e["type"] for e in events]
    assert types[0] == "task.start" and types[-1] == "task.done"
    stages = [(d["stage"], d["status"]) for d in of(events, "task.stage")]
    assert stages == [("analyze", "running"), ("analyze", "done"), ("design", "running"), ("design", "done"),
                      ("spawn", "running"), ("spawn", "done")]
    analyze = of(events, "task.stage")[1]["detail"]
    assert {"id": "intranet_evidence", "label": cfg.load_roles().capabilities["intranet_evidence"]} in analyze["capabilities"]
    design = of(events, "task.stage")[3]["detail"]
    assert design["supervisor"] == "t-jira-sup" and "verifier" in design["roles"] and design["sandbox"] == "rfa-main"
    assert any(d["text"].startswith("teams.yaml 에") for d in of(events, "task.log"))
    done = of(events, "task.done")[0]
    assert done["task"]["id"] == "jira" and done["task"]["status"] == "ready" and done["task"]["desk"] == "jira-desk"
    agent = done["agent"]
    assert agent["name"] == "Jira 이슈 대응 에이전트" and agent["initials"] == "JI" and agent["icon"] == "generic"
    assert agent["tags"] == ["jira", "버그"] and agent["suggestions"][1] == "jira 관련 문의가 있었어?"
    assert agent["description"] == "Jira 이슈 문의에 사내 문서 근거를 찾아 답한다" and agent["color"] == {"bg": "#e4dcf7", "fg": "#4c2a8a"}
    assert [t["id"] for t in tasks][-1] == "jira" and tasks[-1]["worker"] == "t-jira-sup" and tasks[-1]["source"] == "team"
    assert "jira" in agents and agents["jira"]["status"] == "running" and one["status"] == "ready"


async def test_a_new_task_is_found_by_its_tag_and_by_pinning(team_app):
    async with client(team_app) as c:
        await read_task_sse(c, {"name": "회계 정산", "agentName": "Ledger 정산 에이전트", "tags": ["정산"]})
        tagged = await read_sse(c, {"text": "이번 달 정산 문의 정리해줘", "role": "owner"}, OWNER)
        pinned = await read_sse(c, {"text": "요즘 들어온 요청 있어?", "role": "owner", "agentId": "ledger"}, OWNER)
    assert [s["agentId"] for s in of(tagged, "agents.select")[0]["selected"]] == ["ledger"]
    assert of(pinned, "delegate.start")[0]["agentId"] == "ledger" and of(pinned, "run.end")[0]["status"] == "done"


async def test_add_task_is_owner_only_and_checks_names(team_app):
    async with client(team_app) as c:
        guest = await c.post("/tasks", json={"name": "Jira 이슈"})
        empty = await c.post("/tasks", json={"name": "  "}, headers=OWNER)
        taken = await c.post("/tasks", json={"name": "새 태스크", "agentName": "비서 에이전트"}, headers=OWNER)
        catalog = await c.post("/tasks", json={"name": "TRIV3 벤치마크 (오로라)"}, headers=OWNER)
    assert guest.status_code == 403 and guest.json() == {
        "code": "owner_only", "message": "게스트는 태스크와 에이전트를 추가할 수 없습니다. 소유자에게 요청하세요."}
    assert empty.status_code == 422 and empty.json()["message"] == "태스크명을 적어 주세요."
    assert taken.status_code == 409 and taken.json()["message"] == "같은 이름의 태스크나 에이전트가 이미 있습니다."
    assert catalog.status_code == 409


async def test_add_task_without_a_capability_uses_the_default_role_and_failure_is_reported(team_app):
    async with client(team_app) as c:
        events = await read_task_sse(c, {"name": "점심 메뉴"})
    assert of(events, "task.done") and any("기본 역량" in d["text"] for d in of(events, "task.log"))

    svc = team_app.state.frontend

    async def broken(**kwargs):
        kwargs["progress"]("stage", {"stage": "analyze", "status": "running"})
        return 422, {"code": "no_role_for_requirement", "detail": "no role"}

    svc.task_create.teams.create = broken
    async with client(team_app) as c:
        failed = await read_task_sse(c, {"name": "Broken 태스크"})
        after = (await c.get("/tasks/broken")).json()
        listed = [t["id"] for t in (await c.get("/tasks")).json()]
    err = of(failed, "task.error")[0]
    assert err["code"] == "no_role_for_requirement" and err["stage"] == "analyze" and err["message"]
    assert after["status"] == "failed" and "broken" not in listed


def test_task_ids_prefer_latin_tokens_and_stay_unique():
    from rfa_mas.nemoclaw.services.task_create import task_id_for

    assert task_id_for("Jira 이슈", "x", set()) == "jira" and task_id_for("Jira 이슈", "x", {"jira"}) == "jira-2"
    assert task_id_for("회계 정산", "Ledger 정산 에이전트", set()) == "ledger"
    assert task_id_for("회계", "회계 에이전트", set()).startswith("task-")
