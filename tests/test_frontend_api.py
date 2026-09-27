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


async def test_agents_lists_conversation_partners_without_the_censor(app):
    async with client(app) as c:
        r = await c.get("/agents")
    assert r.status_code == 200
    agents = {a["id"]: a for a in r.json()}
    assert "censor" not in agents and {"assistant", "research", "benchmark", "summarizer"} <= set(agents)
    assert agents["assistant"]["kind"] == "assistant" and agents["research"]["kind"] == "task"
    for a in agents.values():
        assert set(a) == {"id", "name", "color", "sandbox", "description", "kind"}
        assert a["color"].startswith("#") and len(a["color"]) == 7 and a["sandbox"] == "rfa-main"


async def test_me_is_owner_only_with_the_bearer(app):
    async with client(app) as c:
        guest = (await c.get("/me")).json()
        wrong = (await c.get("/me", headers={"Authorization": "Bearer nope"})).json()
        owner = (await c.get("/me", headers={"Authorization": "Bearer tok"})).json()
    assert guest == wrong == {"authenticated": False, "role": "guest", "isAdmin": False}
    assert owner == {"authenticated": True, "role": "owner", "isAdmin": True}


async def test_tasks_come_from_the_catalog_with_a_grade_label(app):
    async with client(app) as c:
        r = await c.get("/tasks")
    assert r.status_code == 200
    tasks = {t["id"]: t for t in r.json()}
    assert {"triv3", "quantization_research"} <= set(tasks)
    t = tasks["triv3"]
    assert t["agentId"] == "research" and t["sandbox"] == "rfa-main" and t["source"] == "catalog"
    assert t["gradeLabel"].startswith("L2 ") and "intranet-ro" in t["gradeLabel"]


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


async def test_chat_sse_owner_streams_stages_deltas_and_done_without_guard(app):
    async with client(app) as c:
        events = await read_sse(c, {"text": "오로라 TRIV3 벤치마크 최신 결과 요약해줘", "role": "owner"},
                                {"Authorization": "Bearer tok", "X-Request-Id": "req-sse-1"})
    types = [e["type"] for e in events]
    assert types[0] == "run.started" and types[-1] == "done" and "guard.final" not in types
    assert [e["data"]["stage"] for e in events if e["type"] == "stage"] == ["head", "task:triv3", "censor"]
    assert types.count("delta") >= 2
    assert [e["seq"] for e in events] == list(range(len(events))) and len({e["runId"] for e in events}) == 1
    assert set(events[0]) == {"type", "runId", "seq", "ts", "data"}
    started = events[0]["data"]
    assert started["role"] == "owner" and started["audience"] == "self" and started["profile"] == "internal"
    assert started["requestId"] == "req-sse-1"
    done = events[-1]["data"]
    assert done["refusal"] is None and done["task"]["id"] == "triv3" and done["censor"]["profile"] == "internal"
    assert "".join(e["data"]["text"] for e in events if e["type"] == "delta")  # the deltas are the censored answer


async def test_chat_sse_guest_gets_guard_final_and_public_masking(app):
    async with client(app) as c:
        events = await read_sse(c, {"text": "오로라 TRIV3 벤치마크 최신 결과 요약해줘", "role": "guest"})
    types = [e["type"] for e in events]
    assert events[0]["data"]["role"] == "guest" and events[0]["data"]["audience"] == "public"
    guard = next(e for e in events if e["type"] == "guard.final")["data"]
    assert guard["verdict"] == "redact" and guard["blocked"] is False and "percent-metric" in guard["redactions"]
    assert types.index("guard.final") < types.index("delta")
    text = "".join(e["data"]["text"] for e in events if e["type"] == "delta")
    assert "[REDACTED:" in text and "오로라" not in text


async def test_chat_sse_guest_cannot_claim_owner_and_no_task_is_a_refusal(app):
    async with client(app) as c:
        claimed = await read_sse(c, {"text": "점심 뭐 먹지", "role": "owner"})  # no bearer → guest
    assert claimed[0]["data"]["role"] == "guest"
    guard = next(e for e in claimed if e["type"] == "guard.final")["data"]
    assert guard["blocked"] is True and guard["reason"] == "no_task"
    assert claimed[-1]["type"] == "done" and claimed[-1]["data"]["refusal"]["code"] == "no_task"
    assert not [e for e in claimed if e["type"] == "delta"]


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
