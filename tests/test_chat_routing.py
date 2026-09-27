"""Real local routing/HTTP streaming boundaries. Only synthetic data, no network."""

import asyncio
import json
import sqlite3

import httpx
import pytest
from test_chat_poc import send, stack

from rfa_mas.application.chat import ChatMessage
from rfa_mas.application.team_selector import SelectionRequest
from rfa_mas.contracts import DomainId, TrustedPrincipal
from rfa_mas.errors import RfaError
from rfa_mas.poc.catalog import SqliteTeamCatalog
from rfa_mas.poc.chat import LocalChat
from rfa_mas.poc.routing import LocalChatRouter


async def prepare(container, goal, key, domain=DomainId.TRIV3):
    owner = await container.repository.local_principal()
    return await container.team_factory.ensure(
        SelectionRequest(
            goal=goal,
            domain_id=domain,
            outputs=frozenset({"research_report"}),
            requested_pattern="research",
        ),
        owner,
        idempotency_key=key,
    )


async def test_existing_task_reused_for_query_and_note_not_spawned(tmp_path):
    async with stack(tmp_path) as (app, c):
        container = app.state.container
        original = await prepare(container, "아틀라스 research 자료 조사", "atlas")
        sid = (await c.post("/ui/api/sessions", json={})).json()["session_id"]
        note = await send(c, sid, "메모: 아틀라스 research의 마감은 10월 3일입니다.")
        assert note["route"]["task_id"] == original.task.task_id
        assert note["run_id"] is None and note["status"] == "stored"
        answer = await send(c, sid, "아틀라스 research 마감 알려줘", "question")
        assert answer["route"]["kind"] == "task"
        assert answer["route"]["team_id"] == original.task.team_id
        owner = await container.repository.local_principal()
        result = await container.service.team_result(answer["run_id"], owner)
        assert result.task_id == original.task.task_id and result.team_id == original.task.team_id
        assert len(await SqliteTeamCatalog(container.repository).list_for(owner)) == 1
        duplicate = await send(c, sid, "아틀라스 research 마감 알려줘", "question")
        assert duplicate["run_id"] == answer["run_id"]
        assert [e["stage"] for e in answer["stages"]] == [
            "understanding",
            "routing",
            "preparing",
            "completed",
        ]


async def test_no_match_ambiguity_domain_and_other_owner(tmp_path):
    async with stack(tmp_path) as (app, c):
        container = app.state.container
        await prepare(container, "아틀라스 research 자료 조사", "atlas")
        await prepare(container, "아틀라스 research 근거 비교", "atlas-other")
        catalog = SqliteTeamCatalog(container.repository)
        router = LocalChatRouter(
            container.repository, catalog, policy_version=container.policy.policy_version
        )
        route = await router.resolve(ChatMessage(text="아틀라스 자료 알려줘", message_id="a"))
        assert route["kind"] == "assistant" and route["reason"] == "ambiguous_tasks"
        for text in ("내일 날씨 알려줘", "벤치마크 결과 알려줘", "메모: 내 점심은 샐러드입니다."):
            assert (await router.resolve(ChatMessage(text=text, message_id="a")))[
                "kind"
            ] == "assistant"
        for text, domain in (
            ("양자화 자료 알려줘", "quantization_research"),
            ("TRIV3 자료 알려줘", "triv3"),
        ):
            route = await router.resolve(ChatMessage(text=text, message_id="a"))
            assert route["kind"] == "domain" and route["domain_id"] == domain
        assert (await router.resolve(ChatMessage(text="TRIV3 양자화 비교 알려줘", message_id="a")))[
            "kind"
        ] == "assistant"
        other = TrustedPrincipal(user_id="other-synthetic", authenticated=True)
        assert await catalog.list_for(other) == []
        with pytest.raises(RfaError):
            await catalog.list_for(TrustedPrincipal(user_id="anonymous", authenticated=False))


async def test_inactive_task_not_used_and_no_automatic_team_creation(tmp_path):
    async with stack(tmp_path) as (app, c):
        container = app.state.container
        existing = await prepare(container, "아틀라스 research 자료 조사", "atlas")
        # Fixture simulates an authoritative persisted paused lifecycle.
        paused = existing.model_copy(
            update={"task": existing.task.model_copy(update={"status": "paused"})}
        )
        with sqlite3.connect(container.repository.path) as db:
            db.execute(
                "UPDATE team_slots SET lifecycle_json=? WHERE task_id=?",
                (paused.model_dump_json(), existing.task.task_id),
            )
        sid = (await c.post("/ui/api/sessions", json={})).json()["session_id"]
        answer = await send(c, sid, "아틀라스 마감 알려줘")
        assert answer["route"]["kind"] == "assistant" and answer["run_id"] is None


async def test_stream_frames_arrive_before_work_completes_and_csrf(tmp_path, monkeypatch):
    async with stack(tmp_path) as (app, c):
        sid = (await c.post("/ui/api/sessions", json={})).json()["session_id"]
        path = f"/ui/api/sessions/{sid}/chat/stream"
        body = {"text": "아틀라스 마감 알려줘", "message_id": "stream"}
        assert (await c.post(path, json=body, headers={"X-RFA-CSRF": ""})).status_code == 403
        assert (await c.post("/ui/api/sessions/missing/chat/stream", json=body)).status_code == 404
        entered, release, first_frame = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original = LocalChatRouter.find_refs

        async def slow(self, *args, **kwargs):
            entered.set()
            await release.wait()
            return await original(self, *args, **kwargs)

        monkeypatch.setattr(LocalChatRouter, "find_refs", slow)
        request = c.build_request("POST", path, json=body)
        messages = []
        consumed = False

        async def receive():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": request.content, "more_body": False}
            await asyncio.Event().wait()

        async def capture(message):
            messages.append(message)
            if message["type"] == "http.response.body" and message.get("body"):
                first_frame.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [(k.lower(), v) for k, v in request.headers.raw],
            "server": ("127.0.0.1", 8780),
            "client": ("127.0.0.1", 43210),
        }
        running = asyncio.create_task(app(scope, receive, capture))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            await asyncio.wait_for(first_frame.wait(), 3)
            assert not running.done()
            early = b"".join(m.get("body", b"") for m in messages)
            assert b'"stage": "understanding"' in early and b'"type": "result"' not in early
        finally:
            release.set()
        await asyncio.wait_for(running, 5)
        events = [
            json.loads(line) for line in b"".join(m.get("body", b"") for m in messages).splitlines()
        ]
        assert [e["stage"] for e in events if e["type"] == "stage"] == [
            "understanding",
            "routing",
            "preparing",
            "completed",
        ]
        assert events[-1]["type"] == "result"
        repeated = await c.post(path, json=body)
        again = [json.loads(line) for line in repeated.text.splitlines()]
        assert len(again) == 1 and again[0]["type"] == "result"
        assert (await c.get(f"/ui/api/sessions/{sid}/chat")).json()[0]["stages"] == events[:-1]


@pytest.mark.parametrize("failure", [False, True])
async def test_stream_disconnect_or_error_is_unknown_and_not_reexecuted(tmp_path, failure):
    entered, release = asyncio.Event(), asyncio.Event()

    class Router:
        calls = 0

        async def resolve(self, body):
            self.calls += 1
            entered.set()
            await release.wait()
            raise RuntimeError("SYNTHETIC_PRIVATE_ERROR_NOT_FOR_CLIENT")

    router = Router()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"session_id": "s"})),
        base_url="http://core",
    ) as core:
        chat = LocalChat(tmp_path / "chat.db", core, router)
        message = ChatMessage(text="질문 알려줘", message_id="m")
        events = await chat.stream("s", message)
        assert (await anext(events))["stage"] == "understanding"
        await asyncio.wait_for(entered.wait(), 1)
        if failure:
            release.set()
            error = await anext(events)
            assert error["type"] == "error" and "PRIVATE" not in json.dumps(error)
        await events.aclose()
        replay = await chat.send("s", message)
        assert replay["status"] == "outcome_unknown" and router.calls == 1


async def test_fallback_searches_both_domains_public_does_not_leak(tmp_path):
    async with stack(tmp_path) as (_, c):
        sid = (await c.post("/ui/api/sessions", json={})).json()["session_id"]
        await send(
            c,
            sid,
            "메모: 제피르 마감은 CANARY-ROUTE-PRIVATE입니다.",
            domain_id="quantization_research",
        )
        answer = await send(c, sid, "제피르 마감 알려줘", "q")
        assert answer["route"]["kind"] == "assistant" and "CANARY-ROUTE-PRIVATE" in answer["reply"]
        public = await send(c, sid, "제피르 공개 초안 만들어줘", "public")
        assert "CANARY-ROUTE-PRIVATE" not in public["reply"] and public["run_id"] is None


async def test_assignee_list_and_explicit_task_selection(tmp_path):
    async with stack(tmp_path) as (app, c):
        container = app.state.container
        mine = await prepare(container, "아틀라스 research 자료 조사", "atlas")
        # Fixture: a persisted Task/team owned by someone else (authoritative owner registry).
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
        listed = (await c.get("/ui/api/chat/assignees")).json()
        assert [t["task_id"] for t in listed] == [mine.task.task_id]
        assert listed[0]["pattern"] == "research" and listed[0]["selectable"] is True
        sid = (await c.post("/ui/api/sessions", json={})).json()["session_id"]
        # Explicit selection routes an otherwise unrelated question to that Task team.
        answer = await send(c, sid, "마감이 언제야?", "explicit", task_id=mine.task.task_id)
        assert answer["route"]["kind"] == "task" and answer["route"]["reason"] == "explicit_task"
        assert answer["route"]["team_id"] == mine.task.team_id and answer["run_id"]
        assert answer["team"]["task_id"] == mine.task.task_id
        for bad in ("foreign-task", "missing-task"):
            rejected = await c.post(
                f"/ui/api/sessions/{sid}/chat",
                json={"text": "마감이 언제야?", "message_id": "bad-" + bad, "task_id": bad},
            )
            assert rejected.status_code == 404, rejected.text
        history = (await c.get(f"/ui/api/sessions/{sid}/chat")).json()
        assert [t["message_id"] for t in history] == ["explicit"]  # rejected turns never persist


async def test_explicit_request_creates_one_task_team_then_reuses_it(tmp_path):
    async with stack(tmp_path) as (app, c):
        container = app.state.container
        owner = await container.repository.local_principal()
        catalog = SqliteTeamCatalog(container.repository)
        sid = (await c.post("/ui/api/sessions", json={})).json()["session_id"]
        assert (await c.get("/ui/api/chat/assignees")).json() == []
        # Plain notes/questions never create a Task team.
        await send(c, sid, "메모: 헬리오스 벤치마크 계획은 초안 상태입니다.", "note")
        asked = await send(c, sid, "헬리오스 벤치마크 계획 알려줘", "ask")
        assert asked["route"]["kind"] == "assistant" and await catalog.list_for(owner) == []
        created = await send(c, sid, "헬리오스 지연 벤치마크 결과를 검증해줘", "create")
        assert created["intent"] == "task_run" and created["route"]["kind"] == "new_task"
        assert created["route"]["reason"] == "explicit_task_request_no_match"
        teams = await catalog.list_for(owner)
        assert len(teams) == 1 and created["route"]["task_id"] == teams[0].task.task_id
        assert created["team"]["pattern"] == "benchmark" and created["team"]["simulated"] is True
        assert [e["stage"] for e in created["stages"]] == [
            "understanding",
            "routing",
            "preparing",
            "completed",
        ]
        # Same message again: idempotent replay, no second Task/team.
        replay = await send(c, sid, "헬리오스 지연 벤치마크 결과를 검증해줘", "create")
        assert replay["run_id"] == created["run_id"] and len(await catalog.list_for(owner)) == 1
        listed = (await c.get("/ui/api/chat/assignees")).json()
        assert listed[0]["task_id"] == teams[0].task.task_id and listed[0]["pattern"] == "benchmark"
        # A later related question is routed to the same Task team automatically.
        follow = await send(c, sid, "헬리오스 지연 결과 알려줘", "follow")
        assert follow["route"]["kind"] == "task"
        assert follow["route"]["task_id"] == teams[0].task.task_id
        result = await container.service.team_result(follow["run_id"], owner)
        assert result.team_id == teams[0].task.team_id
        assert len(await catalog.list_for(owner)) == 1
        # An explicit request that matches the existing Task reuses it (no new team).
        again = await send(c, sid, "헬리오스 지연 벤치마크를 다시 분석해줘", "again")
        assert again["route"]["kind"] == "task" and len(await catalog.list_for(owner)) == 1
        # A shared demo tag alone is not a subject: an unrelated request gets its own team.
        second = await send(
            c, sid, "[샘플] 양자화 INT4 논문 근거를 조사해줘 (합성 시연용)", "second"
        )
        assert second["route"]["kind"] == "new_task" and second["team"]["pattern"] == "research"
        assert second["route"]["domain_id"] == "quantization_research"
        assert len(await catalog.list_for(owner)) == 2
