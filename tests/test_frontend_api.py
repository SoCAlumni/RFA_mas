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
    frontend = build_frontend_services(assignments=lambda: assignments, ask_deps=deps, token="tok",
                                       store=Store(tmp_path / "frontend.db"))
    return create_ask_app(AskService(deps, "tok"), frontend=frontend)


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
    assert {"/agents", "/me", "/tasks", "/ask", "/chat", "/teams"} <= set(committed["paths"])
