"""Broker gateway HTTP transport: agent/session addressing, streaming deltas → on_delta, CLI fallback,
and the CLI path still delivering on_delta once. No live gateway is touched (httpx MockTransport)."""

from __future__ import annotations

import json

import httpx
import pytest

from rfa_mas.nemoclaw import audit
from rfa_mas.nemoclaw import config as cfg
from rfa_mas.nemoclaw.broker import Broker, GatewayTransport
from rfa_mas.nemoclaw.markers import find_markers
from tests.test_broker import SECRET, TOKEN, TurnRunner, routing_with


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("RFA_SG_AUDIT_DB", str(tmp_path / "audit.db"))


def make(tmp_path, handler, *, stream=True, fallback=True, token=True, runner=None):
    (tmp_path / "gateway-rfa-main.token").write_text("gw-secret-token\n" if token else "", encoding="utf-8")
    routing = routing_with("gateway", gateways={"rfa-main": "http://gw.test"}, gateway_token_dir=str(tmp_path),
                           gateway_stream=stream, gateway_fallback_cli=fallback)
    transport = GatewayTransport(routing, root=tmp_path.parent, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    runner = runner or TurnRunner("cli reply")
    return Broker(cfg.load_assignments(), routing, SECRET, TOKEN, runner, transport=transport), runner


def sse(*deltas: str) -> str:
    lines = []
    for i, d in enumerate(deltas):
        lines.append("data: " + json.dumps({"id": "c", "object": "chat.completion.chunk",
                                             "choices": [{"index": 0, "delta": {"content": d} if i else {"role": "assistant", "content": d}}]}))
    lines.append("data: " + json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}))
    lines.append("data: [DONE]")
    return "\n\n".join(lines) + "\n\n"


async def test_streaming_turn_relays_deltas_and_addresses_agent_and_session(tmp_path):
    seen = {}
    deltas: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = {k: v for k, v in request.headers.items() if k.startswith("x-openclaw") or k == "authorization"}
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse("오늘은 ", "맑다."))

    b, runner = make(tmp_path, handler)
    audit.remember_session("s_ext", "external", "external")
    result = await b.ask("research", "근거 찾아줘", "s_ext", None, on_delta=deltas.append)
    assert result["ok"] and result["transport"] == "http" and result["route"] == "gateway" and result["reply"] == "오늘은 맑다."
    assert deltas == ["오늘은 ", "맑다."] and runner.calls == []  # no CLI turn
    assert seen["url"] == "http://gw.test/v1/chat/completions"
    assert seen["headers"]["authorization"] == "Bearer gw-secret-token"
    assert seen["headers"]["x-openclaw-session-key"] == "agent:research:broker-s_ext"
    assert seen["body"]["model"] == "openclaw/research" and seen["body"]["stream"] is True
    message = seen["body"]["messages"][0]["content"]
    marker = find_markers(message, SECRET)[0]
    assert marker.verified and marker.fields == {"ch": "external", "sid": "s_ext"} and message.endswith("근거 찾아줘")
    event = audit.query(kind="broker")[0]
    assert event["verdict"] == "ok" and event["action"] == "gateway" and event["detail"]["transport"] == "http"


async def test_non_stream_turn_and_async_on_delta(tmp_path):
    got: list[str] = []

    async def on_delta(text: str) -> None:
        got.append(text)

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is False
        return httpx.Response(200, json={"choices": [{"index": 0, "message": {"role": "assistant", "content": "전체 답"},
                                                      "finish_reason": "stop"}]})

    b, _ = make(tmp_path, handler, stream=False)
    result = await b.ask("summarizer", "요약", "s1", None, on_delta=on_delta)
    assert result["ok"] and result["reply"] == "전체 답" and got == ["전체 답"]


async def test_transport_error_falls_back_to_cli_and_audits(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    deltas: list[str] = []
    b, runner = make(tmp_path, handler)
    result = await b.ask("research", "근거", "s2", None, on_delta=deltas.append)
    assert result["ok"] and result["transport"] == "cli" and result["reply"] == "cli reply" and deltas == ["cli reply"]
    assert runner.calls and runner.calls[0][:5] == ["nemoclaw", "rfa-main", "agent", "--agent", "research"]
    kinds = [(e["verdict"], e["action"]) for e in audit.query(kind="broker")]
    assert ("fallback", "gateway-http") in kinds and kinds[0] == ("ok", "gateway")


async def test_transport_error_without_fallback_is_an_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    b, runner = make(tmp_path, handler, fallback=False)
    result = await b.ask("research", "근거", "s3", None)
    assert not result["ok"] and "gateway HTTP 401" in result["error"] and runner.calls == []


async def test_missing_token_or_url_uses_cli(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:  # must never be called
        raise AssertionError("gateway must not be called without a token")

    b, runner = make(tmp_path, handler, token=False)
    result = await b.ask("research", "근거", "s4", None)
    assert result["ok"] and result["transport"] == "cli" and runner.calls
    assert not b.transport.available("rfa-tasks-none")  # no URL declared for that sandbox


def test_default_routing_declares_the_gateway_transport():
    routing = cfg.load_routing()
    assert routing.broker.transport == "gateway" and routing.broker.gateways == {"rfa-main": "http://127.0.0.1:18790"}
    assert routing.broker.gateway_fallback_cli is True
