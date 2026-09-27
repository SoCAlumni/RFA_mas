"""Broker: routing decisions, session channel propagation, drain, MCP JSON-RPC and REST surfaces."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route

from rfa_mas.nemoclaw import audit
from rfa_mas.nemoclaw import config as cfg
from rfa_mas.nemoclaw.broker import Broker
from rfa_mas.nemoclaw.markers import find_markers, make_marker
from rfa_mas.nemoclaw.runner import CommandResult

NO_TEAMS = Path("/nonexistent/teams.yaml")  # static declaration only: teams.yaml is runtime state (POST /teams, task teams)

SECRET = b"b" * 48
TOKEN = "broker-token-0123456789abcdef"


class TurnRunner:
    """Records nemoclaw agent invocations and answers with an OpenClaw-shaped JSON payload."""

    def __init__(self, reply: str = "task reply", rc: int = 0):
        self.reply, self.rc, self.calls = reply, rc, []

    def run(self, argv, *, timeout=300, env=None, input_text=None, check=False):
        self.calls.append(list(argv))
        payload = {"status": "ok", "result": {"payloads": [{"text": self.reply}]}}
        return CommandResult(list(argv), self.rc, "✓ Active gateway set\n" + json.dumps(payload), "")


@pytest.fixture(autouse=True)
def audit_db(tmp_path, monkeypatch):
    monkeypatch.setenv("RFA_SG_AUDIT_DB", str(tmp_path / "audit.db"))


def routing_with(transport: str = "cli", **broker_fields) -> cfg.Routing:
    """Unit tests never talk to a live gateway: the CLI transport is the default here."""
    routing = cfg.load_routing()
    routing.broker = routing.broker.model_copy(update={"transport": transport, **broker_fields})
    return routing


@pytest.fixture
def broker() -> tuple[Broker, TurnRunner]:
    runner = TurnRunner()
    return Broker(cfg.load_assignments(teams_path=NO_TEAMS), routing_with("cli"), SECRET, TOKEN, runner), runner


def test_route_decides_local_spawn_versus_gateway(broker):
    b, _ = broker
    assert b.route("research", "rfa-main").kind == "local-spawn"  # assistant and research share the default sandbox
    remote = b.route("research", "rfa-tasks-none")
    assert (remote.kind, remote.sandbox, remote.openclaw_id, remote.alias) == ("gateway", "rfa-main", "research", "rfa-external")
    with pytest.raises(KeyError):
        b.route("assistant", "rfa-main")  # fixed agents are not delegation targets
    with pytest.raises(KeyError):
        b.route("censor", "rfa-main")  # the censor shares the sandbox but is not delegatable
    assert "censor" not in {a["name"] for a in b.list_agents()}
    with pytest.raises(KeyError):
        b.route("nope", None)


def test_channel_comes_from_the_entry_session_store_and_defaults_to_least_exposed(broker):
    b, _ = broker
    assert b.channel_for(None) == "internal" and b.channel_for("unknown") == "internal"
    audit.remember_session("s_ext", "external", "external")
    assert b.channel_for("s_ext") == "external"


async def test_ask_plants_a_signed_channel_marker_and_targets_the_agent_sandbox(broker):
    b, runner = broker
    audit.remember_session("s_ext", "external", "external")
    # same sandbox as the caller: no nested nemoclaw CLI call; the caller spawns natively
    local = await b.ask("research", "근거 찾아줘", "s_ext", "rfa-main")
    assert local["ok"] and local["route"] == "local-spawn" and local["reply"] is None and runner.calls == []
    delegate = local["delegate"]
    assert delegate["tool"] == "sessions_spawn" and delegate["agentId"] == "research"
    marker = find_markers(delegate["message"], SECRET)[0]
    assert marker.verified and marker.fields == {"ch": "external", "sid": "s_ext"} and delegate["message"].endswith("근거 찾아줘")
    assert audit.query(kind="broker")[0]["verdict"] == "delegated"
    # another sandbox (or a host-side caller): the broker drives the target agent through the gateway CLI
    result = await b.ask("research", "근거 찾아줘", "s_ext", None)
    assert result["ok"] and result["route"] == "gateway" and result["reply"] == "task reply"
    argv = runner.calls[0]
    assert argv[:5] == ["nemoclaw", "rfa-main", "agent", "--agent", "research"]
    assert argv[argv.index("--session-id") + 1] == "broker-s_ext"
    marker = find_markers(argv[-1], SECRET)[0]
    assert marker.verified and marker.fields == {"ch": "external", "sid": "s_ext"} and argv[-1].endswith("근거 찾아줘")
    event = audit.query(kind="broker")[0]
    assert (event["channel"], event["agent"], event["sandbox"], event["action"]) == ("external", "research", "rfa-main", "gateway")


async def test_ask_refuses_unknown_and_draining_agents(broker):
    b, runner = broker
    assert (await b.ask("ghost", "q", None, None))["ok"] is False
    b.draining.add("research")
    refused = await b.ask("research", "q", None, None)
    assert refused["ok"] is False and "draining" in refused["error"] and runner.calls == []
    assert audit.query(kind="broker")[0]["verdict"] == "refused"


async def test_drain_waits_for_inflight_then_reports(broker):
    b, _ = broker
    b.inflight["benchmark"] = 0
    result = await b.drain("benchmark", timeout=0.5)
    assert result == {"agent": "benchmark", "drained": True, "inflight": 0} and "benchmark" in b.draining
    b.inflight["benchmark"] = 1
    slow = await b.drain("benchmark", timeout=0.3)
    assert slow["drained"] is False and audit.query(kind="relocation")[0]["verdict"] == "timeout"
    assert b.undrain("benchmark") == {"agent": "benchmark", "draining": False}


def app_for(b: Broker) -> httpx.AsyncClient:
    app = Starlette(routes=[Route("/mcp", b.mcp, methods=["GET", "POST", "DELETE"]),
                            Route("/broker/agents", b.rest_agents, methods=["GET"]),
                            Route("/broker/ask", b.rest_ask, methods=["POST"])])
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://broker")


async def test_mcp_streamable_http_surface(broker):
    b, runner = broker
    async with app_for(b) as c:
        assert (await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})).status_code == 401
        auth = {"Authorization": f"Bearer {TOKEN}"}
        init = await c.post("/mcp", headers=auth, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                                        "params": {"protocolVersion": "2025-03-26", "capabilities": {}}})
        assert init.status_code == 200 and init.headers["mcp-session-id"]
        assert init.json()["result"]["protocolVersion"] == "2025-03-26" and "tools" in init.json()["result"]["capabilities"]
        notified = await c.post("/mcp", headers=auth, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert notified.status_code == 202
        tools = await c.post("/mcp", headers=auth, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert [t["name"] for t in tools.json()["result"]["tools"]] == ["ask_task_agent", "list_task_agents"]
        listed = await c.post("/mcp", headers=auth, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                                          "params": {"name": "list_task_agents", "arguments": {}}})
        agents = json.loads(listed.json()["result"]["content"][0]["text"])
        assert {a["name"] for a in agents} == {"research", "benchmark", "summarizer"}
        asked = await c.post("/mcp", headers=auth, json={"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                                         "params": {"name": "ask_task_agent",
                                                                    "arguments": {"name": "summarizer", "query": "요약", "session_id": "s_9"}}})
        payload = json.loads(asked.json()["result"]["content"][0]["text"])
        assert payload["ok"] and payload["sandbox"] == "rfa-main" and asked.json()["result"]["isError"] is False
        assert payload["route"] == "local-spawn" and payload["delegate"]["agentId"] == "summarizer"  # MCP caller = assistant sandbox
        unknown = await c.post("/mcp", headers=auth, json={"jsonrpc": "2.0", "id": 5, "method": "resources/list"})
        assert unknown.json()["error"]["code"] == -32601
        assert (await c.get("/mcp", headers=auth)).status_code == 405
        assert (await c.delete("/mcp", headers=auth)).status_code == 204


async def test_rest_fallback_surface(broker):
    b, _ = broker
    async with app_for(b) as c:
        auth = {"Authorization": f"Bearer {TOKEN}"}
        assert (await c.get("/broker/agents")).status_code == 401
        assert len((await c.get("/broker/agents", headers=auth)).json()["agents"]) == 3
        ok = await c.post("/broker/ask", headers=auth, json={"name": "benchmark", "query": "지연"})
        assert ok.status_code == 200 and ok.json()["route"] == "local-spawn"  # REST fallback is also called from the assistant sandbox
        bad = await c.post("/broker/ask", headers=auth, json={"name": "nope", "query": "x"})
        assert bad.status_code == 409


# ----------------------------------------------------------------------------- team supervisors: wait for the final


class FakeWaiter:
    """Stands in for session_wait.SessionWaiter (the in-sandbox transcript poll)."""

    def __init__(self, outcome: dict):
        self.outcome, self.calls = outcome, []

    def wait_final(self, sandbox, agent_id, key, since_ms, timeout_s):
        self.calls.append((sandbox, agent_id, key, since_ms, timeout_s))
        return self.outcome


def team_broker(reply: str, waiter: FakeWaiter) -> tuple[Broker, TurnRunner]:
    runner = TurnRunner(reply=reply)
    return Broker(cfg.load_assignments(), routing_with("cli"), SECRET, TOKEN, runner, waiter=waiter), runner


async def test_supervisor_turn_waits_for_the_final_answer_after_its_yield():
    # the gateway/CLI turn ends at sessions_yield with no payload; the answer lands in the transcript later
    leaked = make_marker("agent", {"agent": "npu-sdk", "alias": "rfa-internal", "sandbox": "rfa-main"}, SECRET)
    waiter = FakeWaiter({"state": "final", "text": f"Conv3D 는 미지원 {leaked}\n근거: 없음\n검증: pass",
                         "turns": 4, "last": "text", "waited_ms": 21_000})
    b, runner = team_broker("No response from OpenClaw.", waiter)
    deltas: list[str] = []
    result = await b.ask("npu-sdk", "Conv3D 지원 여부?", "s_team", None, on_delta=deltas.append)
    assert result["ok"] and result["reply"].startswith("Conv3D 는 미지원") and "⟦" not in result["reply"]
    assert deltas == [result["reply"]]  # the turn streamed nothing; the final is emitted once
    sandbox, agent_id, key, since_ms, timeout_s = waiter.calls[0]
    assert (sandbox, agent_id, key) == ("rfa-main", "npu-sdk", "agent:npu-sdk:broker-s_team")
    assert since_ms > 1_700_000_000_000 and 10 <= timeout_s <= b.routing.broker.task_turn_timeout_seconds
    assert result["wait"]["wait_state"] == "final" and result["wait"]["wait_ms"] == 21_000
    assert audit.query(kind="broker")[0]["detail"]["wait_state"] == "final"


async def test_supervisor_wait_timeout_is_an_error_and_plain_agents_never_wait():
    waiter = FakeWaiter({"state": "timeout", "text": "", "last": "sessions_yield", "turns": 1, "waited_ms": 100})
    b, _ = team_broker("No response from OpenClaw.", waiter)
    result = await b.ask("npu-sdk", "q", "s_t2", None)
    assert result["ok"] is False and "timeout" in result["error"] and "sessions_yield" in result["error"]
    plain = await b.ask("research", "q", "s_t3", None)   # not a supervisor: answers within its turn
    assert plain["ok"] and plain["reply"] == "(empty reply)" and "wait" not in plain and len(waiter.calls) == 1


async def test_supervisor_reply_within_the_turn_is_kept_when_the_wait_confirms_it():
    waiter = FakeWaiter({"state": "final", "text": "이미 최종", "turns": 1, "last": "text", "waited_ms": 60})
    b, _ = team_broker("이미 최종", waiter)
    deltas: list[str] = []
    result = await b.ask("infer-opt", "q", "s_t4", None, on_delta=deltas.append)
    assert result["ok"] and result["reply"] == "이미 최종" and deltas == ["이미 최종"]
    # a provisional note before the yield is followed by the final, not replaced silently
    waiter2 = FakeWaiter({"state": "final", "text": "최종", "turns": 2, "last": "text", "waited_ms": 9_000})
    b2, _ = team_broker("확인 중입니다", waiter2)
    deltas2: list[str] = []
    result2 = await b2.ask("infer-opt", "q", "s_t5", None, on_delta=deltas2.append)
    assert result2["reply"] == "최종" and deltas2 == ["확인 중입니다", "\n\n최종"]
