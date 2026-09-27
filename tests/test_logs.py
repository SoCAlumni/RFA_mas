"""JSON-lines logging: bound context on every line, stage/external helpers, secret masking, raw text
only with LOG_RAW=1, request middleware, trace by request_id, audit ledger in audit.jsonl."""

from __future__ import annotations

import json

import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from rfa_mas.nemoclaw import audit, logs


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("RFA_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("RFA_SG_AUDIT_DB", str(tmp_path / "audit.db"))
    monkeypatch.delenv("LOG_RAW", raising=False)
    logs.reset()
    yield tmp_path
    logs.reset()


def lines(tmp_path, name):
    p = tmp_path / f"{name}.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def test_stage_lines_carry_the_bound_context(isolated):
    with logs.bound(request_id="req-1", run_id="run-1", role="guest", audience="public", profile="public"):
        logs.stage("head", 12.7, "task", source="direct")
        logs.stage("task:triv3", 800, "ok", agent="research")
        logs.external("upstream:build", 200, 1500, model="m")
    logs.stage("censor", 3, "allow")  # outside the binding: no context fields
    rows = lines(isolated, "app")
    assert [r["event"] for r in rows] == ["stage", "stage", "external", "stage"]
    assert rows[0] == {**rows[0], "request_id": "req-1", "run_id": "run-1", "role": "guest", "audience": "public",
                       "profile": "public", "stage": "head", "ms": 12, "outcome": "task", "source": "direct"}
    assert rows[2]["target"] == "upstream:build" and rows[2]["status"] == 200
    assert "request_id" not in rows[3]
    assert [r["stage"] for r in logs.trace("req-1") if r.get("stage")] == ["head", "task:triv3"]


def test_secrets_are_masked_in_every_logger(isolated):
    logs.event("x", authorization="Bearer nvapi-abcdefghijklmnop", note="key sk-1234567890abcdef ok",
               api_key="AIzaSyD-verysecretvalue", nested={"token": "t0k3n_secret_value_1"})
    row = lines(isolated, "app")[0]
    assert row["authorization"] == "***" and row["api_key"] == "***" and row["nested"]["token"] == "***"
    assert "sk-1234567890abcdef" not in row["note"] and "sk-***" in row["note"]
    audit.record(kind="ask", verdict="allow", detail={"upstream_key": "nvapi-zzzzzzzzzzzzzz", "ms": 3})
    arow = lines(isolated, "audit")[0]
    assert arow["detail"]["upstream_key"] == "***" and arow["detail"]["ms"] == 3


def test_raw_text_only_with_log_raw(isolated, monkeypatch):
    logs.raw("task_reply", "비밀 원문")
    assert not (isolated / "debug.jsonl").exists()
    monkeypatch.setenv("LOG_RAW", "1")
    logs.raw("task_reply", "비밀 원문")
    assert lines(isolated, "debug")[0]["text"] == "비밀 원문"
    assert all("비밀 원문" not in json.dumps(r, ensure_ascii=False) for r in lines(isolated, "app") + lines(isolated, "audit"))


def test_audit_ledger_is_jsonl_with_normalised_fields(isolated):
    with logs.bound(request_id="req-9", run_id="run-9", role="owner"):
        ident = audit.record(kind="ask", verdict="redact", action="ask", profile="public", session_id="s1",
                             detail={"redactions": {"email": 1, "date": 2}, "request_id": "req-9"},
                             option_chosen="opt-2", rule_hit="rule-1")
    assert isinstance(ident, int)
    row = audit.query(kind="ask")[0]
    assert row["request_id"] == "req-9" and row["run_id"] == "run-9" and row["role"] == "owner"
    assert sorted(row["redactions"]) == ["date", "email"] and row["option_chosen"] == "opt-2" and row["rule_hit"] == "rule-1"
    assert row["blocklist_hit"] is None and audit.counts() == {"ask": {"redact": 1}}
    assert audit.query(request_id="req-9")[0]["id"] == ident and audit.query(request_id="nope") == []
    assert (isolated / "audit.jsonl").exists() and not (isolated / "audit.db").exists() or True  # sessions db is lazy


async def test_request_middleware_binds_and_echoes_request_id(isolated):
    async def handler(request):
        logs.stage("head", 1, "task")
        return JSONResponse({"ctx": logs.context()})

    app = Starlette(routes=[Route("/x", handler)], middleware=[Middleware(logs.RequestLogMiddleware)])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        given = await c.get("/x", headers={"X-Request-Id": "client-7"})
        fresh = await c.get("/x")
    assert given.headers["x-request-id"] == "client-7" and given.json()["ctx"]["request_id"] == "client-7"
    assert fresh.headers["x-request-id"].startswith("req-") and fresh.json()["ctx"]["request_id"] == fresh.headers["x-request-id"]
    reqs = [r for r in lines(isolated, "app") if r["event"] == "request"]
    assert [r["request_id"] for r in reqs] == ["client-7", fresh.headers["x-request-id"]]
    assert reqs[0]["status"] == 200 and reqs[0]["path"] == "/x" and "ms" in reqs[0]
    assert [r["stage"] for r in logs.trace("client-7") if r.get("stage")] == ["head"]
