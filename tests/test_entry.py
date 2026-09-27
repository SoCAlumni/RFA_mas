"""Channel API entry point: channel fixed per session, signed marker, final-reply censor, audit."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from rfa_mas.nemoclaw import audit
from rfa_mas.nemoclaw import config as cfg
from rfa_mas.nemoclaw.broker import Broker
from rfa_mas.nemoclaw.censor import CensorPipeline, StaticJudge
from rfa_mas.nemoclaw.entry import Entry
from rfa_mas.nemoclaw.markers import find_markers
from rfa_mas.nemoclaw.runner import CommandResult

NO_TEAMS = Path("/nonexistent/teams.yaml")  # static declaration only: teams.yaml is runtime state (POST /teams, task teams)

SECRET = b"e" * 48


class AssistantRunner:
    def __init__(self, reply: str):
        self.reply, self.calls = reply, []

    def run(self, argv, *, timeout=300, env=None, input_text=None, check=False):
        self.calls.append(list(argv))
        payload = {"status": "ok", "result": {"payloads": [{"text": self.reply}]}}
        return CommandResult(list(argv), 0, json.dumps(payload), "")


@pytest.fixture(autouse=True)
def audit_db(tmp_path, monkeypatch):
    monkeypatch.setenv("RFA_SG_AUDIT_DB", str(tmp_path / "audit.db"))


def make_entry(reply: str) -> tuple[Entry, AssistantRunner]:
    a, r, c = cfg.load_assignments(teams_path=NO_TEAMS), cfg.load_routing(), cfg.load_censors()
    runner = AssistantRunner(reply)
    pipeline = CensorPipeline(c, StaticJudge("allow"))
    broker = Broker(a, r, SECRET, "tok" * 10, runner)
    return Entry(a, r, pipeline, broker, SECRET, runner), runner


async def test_external_channel_plants_marker_and_censors_final_reply():
    entry, runner = make_entry("오로라 P95 12.5 ms, 예산 1,200,000 원 입니다.")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=entry.app()), base_url="http://entry") as c:
        r = await c.post("/channel/external/chat", json={"text": "오로라 요약해줘", "session_id": "s_ext_1"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["verdict"] == "redact" and "12.5" not in body["reply"] and "[REDACTED:project]" in body["reply"]
    assert body["profile"] == "external" and body["alias"] == "rfa-external"
    argv = runner.calls[0]
    assert argv[:5] == ["nemoclaw", "rfa-main", "agent", "--agent", "main"]
    marker = find_markers(argv[-1], SECRET)[0]
    assert marker.verified and marker.fields == {"ch": "external", "sid": "s_ext_1"}
    assert audit.session_channel("s_ext_1") == "external"
    event = audit.query(kind="channel")[0]
    assert event["channel"] == "external" and event["verdict"] == "redact" and event["session_id"] == "s_ext_1"


async def test_internal_channel_is_not_censored_and_session_channel_is_sticky():
    entry, _ = make_entry("오로라 P95 12.5 ms")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=entry.app()), base_url="http://entry") as c:
        r = await c.post("/channel/internal/chat", json={"text": "질문", "session_id": "s_int"})
        assert r.status_code == 201 and r.json()["reply"] == "오로라 P95 12.5 ms" and r.json()["verdict"] == "allow"
        mismatch = await c.post("/channel/external/chat", json={"text": "질문", "session_id": "s_int"})
        assert mismatch.status_code == 409 and mismatch.json()["code"] == "session_channel_mismatch"
        assert (await c.post("/channel/nope/chat", json={"text": "x"})).status_code == 404
        assert (await c.post("/channel/internal/chat", json={"text": ""})).status_code == 422


async def test_audit_endpoints_expose_summary_and_events():
    entry, _ = make_entry("ok")
    audit.record(kind="policy", verdict="ok", action="policy-add", sandbox="rfa-tasks-none")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=entry.app()), base_url="http://entry") as c:
        page = await c.get("/audit/")
        assert page.status_code == 200 and "감사 로그" in page.text
        events = (await c.get("/audit/api/events?kind=policy")).json()["events"]
        assert events[0]["action"] == "policy-add"
        summary = (await c.get("/audit/api/summary")).json()
        assert summary["mode"] == "rfa-auto" and {a["agent"] for a in summary["agents"]} >= {"assistant", "censor", "research"}
        state = (await c.get("/broker/admin/state")).json()
        assert state["draining"] == [] and len(state["agents"]) == 3
