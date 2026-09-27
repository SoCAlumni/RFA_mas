"""Broker: the only path between sandboxes, exposed to the assistant as an MCP server.

``ask_task_agent(name, query, session_id)`` hides where the target runs: same sandbox
(OpenClaw ``sessions_spawn`` is also allowed natively by the manifest allowlist) or another
sandbox (OpenClaw gateway turn via ``nemoclaw <sb> agent --agent <id>``). The session's channel,
fixed at the API entry point, is re-planted as a signed marker so the egress-proxy applies the
same censor profile to the task agent's inference. Drain state gates relocations.

Transport: minimal MCP Streamable HTTP (JSON-RPC over POST, JSON responses) for NemoClaw's
managed ``mcp add``; the same tools are also served as a REST fallback (``sg-control-plane``).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from rfa_mas.nemoclaw import audit, logs
from rfa_mas.nemoclaw.config import Assignments, Routing
from rfa_mas.nemoclaw.markers import make_marker, strip_markers
from rfa_mas.nemoclaw.runner import Runner, extract_json

ROOT = Path(__file__).resolve().parents[3]
OnDelta = Callable[[str], Awaitable[None] | None]


class TransportError(RuntimeError):
    """The gateway HTTP turn could not be completed (transport, auth, non-200, bad stream)."""


class GatewayTransport:
    """Task-agent turn over HTTP to the sandbox OpenClaw gateway (OpenAI-compatible endpoint).

    Agent selection is ``model: openclaw/<agentId>`` (or ``x-openclaw-agent-id``); the session is pinned
    with ``x-openclaw-session-key: agent:<agentId>:broker-<sid>``; auth is the gateway token (shared
    secret → sender is owner). ``stream: true`` yields OpenAI ``chat.completion.chunk`` deltas which
    are relayed to ``on_delta`` and assembled into the reply. Measured 2026-09-28: summarizer turn
    4.2 s (CLI path 12 s), first token ≈2 s.
    """

    def __init__(self, routing: Routing, root: Path = ROOT, client: httpx.AsyncClient | None = None):
        self.routing = routing
        self.root = root
        self._client = client
        self._tokens: dict[str, str] = {}

    def url_for(self, sandbox: str) -> str | None:
        url = self.routing.broker.gateways.get(sandbox)
        return url.rstrip("/") if url else None

    def token_for(self, sandbox: str) -> str | None:
        if sandbox not in self._tokens:
            path = self.root / self.routing.broker.gateway_token_dir / f"gateway-{sandbox}.token"
            try:
                self._tokens[sandbox] = path.read_text(encoding="utf-8").strip()
            except OSError:
                return None
        return self._tokens[sandbox] or None

    def available(self, sandbox: str) -> bool:
        return self.url_for(sandbox) is not None and self.token_for(sandbox) is not None

    async def turn(self, decision: RouteDecision, sid: str, message: str, timeout_seconds: float,
                   on_delta: OnDelta | None = None) -> str:
        url, token = self.url_for(decision.sandbox), self.token_for(decision.sandbox)
        if not url or not token:
            raise TransportError(f"no gateway url/token for sandbox {decision.sandbox}")
        stream = bool(self.routing.broker.gateway_stream)  # on_delta works on both paths
        headers = {"authorization": f"Bearer {token}", "content-type": "application/json",
                   "x-openclaw-session-key": f"agent:{decision.openclaw_id}:broker-{sid}"}
        body = {"model": f"openclaw/{decision.openclaw_id}", "stream": stream,
                "messages": [{"role": "user", "content": message}]}
        client = self._client or httpx.AsyncClient(timeout=timeout_seconds + 15)
        timer = logs.Timer()
        first_ms: int | None = None
        try:
            if not stream:
                response = await client.post(f"{url}/v1/chat/completions", headers=headers, json=body)
                if response.status_code != 200:
                    raise TransportError(f"gateway HTTP {response.status_code}")
                data = response.json()
                text = str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
                if on_delta and text:
                    await _emit(on_delta, text)
            else:
                parts: list[str] = []
                async with client.stream("POST", f"{url}/v1/chat/completions", headers=headers, json=body) as response:
                    if response.status_code != 200:
                        raise TransportError(f"gateway HTTP {response.status_code}")
                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        payload = line[6:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            chunk = json.loads(payload)
                        except ValueError:
                            continue
                        delta = ((chunk.get("choices") or [{}])[0].get("delta") or {}).get("content")
                        if delta:
                            if first_ms is None:
                                first_ms = timer.ms
                            parts.append(delta)
                            if on_delta:
                                await _emit(on_delta, delta)
                text = "".join(parts)
        except httpx.HTTPError as exc:
            logs.external("openclaw-gateway:http", type(exc).__name__, timer.ms, sandbox=decision.sandbox,
                          agent=decision.openclaw_id, session_id=sid, stream=stream)
            raise TransportError(f"gateway transport: {type(exc).__name__}") from exc
        finally:
            if self._client is None:
                await client.aclose()
        logs.external("openclaw-gateway:http", 200, timer.ms, sandbox=decision.sandbox, agent=decision.openclaw_id,
                      session_id=sid, stream=stream, first_token_ms=first_ms, chars=len(text))
        return text


async def _emit(on_delta: OnDelta, text: str) -> None:
    result = on_delta(text)
    if asyncio.iscoroutine(result):
        await result

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "rfa-broker", "version": "1.0"}
TOOLS = [
    {
        "name": "ask_task_agent",
        "description": (
            "Delegate a question to a task agent by name. The broker applies the session's channel policy "
            "and routes: for an agent in another sandbox it returns the agent's reply; for an agent in your own "
            "sandbox it returns a `delegate` object — then call sessions_spawn with that agentId and message "
            "verbatim and relay the reply. Always pass the session_id from the routing marker in the user's message."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "task agent id (see list_task_agents)"},
                "query": {"type": "string", "description": "the question or task for that agent"},
                "session_id": {"type": "string", "description": "sid from the ⟦rfa-channel …⟧ marker"},
            },
            "required": ["name", "query"],
        },
    },
    {
        "name": "list_task_agents",
        "description": "List task agents with their description, security groups and sandbox.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


@dataclass
class RouteDecision:
    agent: str
    sandbox: str
    openclaw_id: str
    kind: str  # "local-spawn" | "gateway"
    alias: str


@dataclass
class Broker:
    assignments: Assignments
    routing: Routing
    secret: bytes
    token: str
    runner: Runner
    replay: bool = False
    draining: set[str] = field(default_factory=set)
    inflight: dict[str, int] = field(default_factory=dict)
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    transport: GatewayTransport | None = None   # built from routing.broker when transport == "gateway"

    def __post_init__(self):
        if self.transport is None and self.routing.broker.transport == "gateway":
            self.transport = GatewayTransport(self.routing)

    # ---- routing --------------------------------------------------------------------------

    def route(self, agent: str, caller_sandbox: str | None) -> RouteDecision:
        spec = self.assignments.agents.get(agent)
        if spec is None or spec.kind != "task" or not spec.delegatable:
            raise KeyError(f"unknown task agent {agent!r}")  # fixed agents and the censor are never targets
        sandbox = self.assignments.sandbox_for(agent)
        kind = "local-spawn" if caller_sandbox == sandbox else "gateway"
        return RouteDecision(agent, sandbox, agent, kind, spec.alias)

    def channel_for(self, session_id: str | None) -> str:
        """Channel fixed at the entry point; unknown/absent sessions get the least-exposed one."""
        stored = audit.session_channel(session_id) if session_id else None
        if stored in self.routing.channels:
            return stored
        return min(self.routing.channels, key=lambda c: self.routing.aliases[self.routing.channels[c].alias].exposure)

    def list_agents(self) -> list[dict]:
        placement = self.assignments.placement()
        out = []
        for agent_id, spec in self.assignments.agents.items():
            if spec.kind != "task" or not spec.delegatable:
                continue
            out.append({
                "name": agent_id, "description": spec.description, "groups": spec.groups,
                "sandbox": placement[agent_id], "alias": spec.alias,
                "draining": agent_id in self.draining,
            })
        return out

    # ---- execution ------------------------------------------------------------------------

    async def ask(self, agent: str, query: str, session_id: str | None, caller_sandbox: str | None,
                  on_delta: OnDelta | None = None) -> dict:
        """``on_delta`` (sync or async callable) receives reply text as it arrives on the gateway HTTP
        transport; on the CLI path it is called once with the whole reply."""
        started = time.monotonic()
        if agent in self.draining:
            audit.record(kind="broker", verdict="refused", action="ask", agent=agent, session_id=session_id,
                         detail={"reason": "draining"})
            return {"ok": False, "error": f"agent {agent} is draining (relocation in progress); retry later"}
        try:
            decision = self.route(agent, caller_sandbox)
        except KeyError as exc:
            audit.record(kind="broker", verdict="refused", action="ask", agent=agent, session_id=session_id,
                         detail={"reason": str(exc)})
            return {"ok": False, "error": str(exc)}
        channel = self.channel_for(session_id)
        sid = session_id or f"s_{uuid.uuid4().hex[:12]}"
        marker = make_marker("channel", {"ch": channel, "sid": sid}, self.secret)
        message = f"{marker}\n{query}"
        if decision.kind == "local-spawn":
            # Same sandbox: the caller spawns natively (OpenClaw sessions_spawn). Running another
            # `nemoclaw agent` from inside an agent turn would block on NemoClaw's host lock.
            ms = int((time.monotonic() - started) * 1000)
            audit.record(kind="broker", verdict="delegated", action="local-spawn", channel=channel,
                         profile=self.routing.channels[channel].profile, sandbox=decision.sandbox, agent=agent,
                         session_id=sid, detail={"caller_sandbox": caller_sandbox, "ms": ms, "alias": decision.alias})
            return {"ok": True, "agent": agent, "sandbox": decision.sandbox, "route": decision.kind, "channel": channel,
                    "session_id": sid, "reply": None, "ms": ms,
                    "delegate": {"tool": "sessions_spawn", "agentId": decision.openclaw_id, "message": message,
                                 "note": "same sandbox: call sessions_spawn with exactly this agentId and message "
                                         "(the first line is the routing marker), then relay its reply"}}
        self.inflight[agent] = self.inflight.get(agent, 0) + 1
        transport_used = "cli"
        try:
            if self.replay:
                reply, status = f"(replay) {agent}@{decision.sandbox} would answer: {query[:60]}", "ok"
            elif self.transport is not None and self.transport.available(decision.sandbox):
                timeout = self.routing.broker.task_turn_timeout_seconds
                try:
                    text = await asyncio.wait_for(self.transport.turn(decision, sid, message, timeout, on_delta), timeout + 15)
                    reply, status, transport_used = strip_markers(text) or "(empty reply)", "ok", "http"
                except (TransportError, TimeoutError) as exc:
                    audit.record(kind="broker", verdict="fallback" if self.routing.broker.gateway_fallback_cli else "error",
                                 action="gateway-http", sandbox=decision.sandbox, agent=agent, session_id=sid,
                                 detail={"error": str(exc)[:200]})
                    if not self.routing.broker.gateway_fallback_cli:
                        reply, status = f"task agent turn failed (gateway http): {exc}", "error"
                    else:
                        reply, status = await asyncio.to_thread(self._turn, decision, sid, message)
                        if status == "ok" and on_delta:
                            await _emit(on_delta, reply)
            else:
                reply, status = await asyncio.to_thread(self._turn, decision, sid, message)
                if status == "ok" and on_delta:
                    await _emit(on_delta, reply)
        finally:
            self.inflight[agent] -= 1
        ms = int((time.monotonic() - started) * 1000)
        audit.record(kind="broker", verdict=status, action=decision.kind, channel=channel,
                     profile=self.routing.channels[channel].profile, sandbox=decision.sandbox, agent=agent,
                     session_id=sid, detail={"caller_sandbox": caller_sandbox, "ms": ms, "alias": decision.alias,
                                             "transport": transport_used})
        return {"ok": status == "ok", "agent": agent, "sandbox": decision.sandbox, "route": decision.kind,
                "transport": transport_used, "channel": channel, "session_id": sid, "reply": reply, "ms": ms,
                **({"error": reply} if status != "ok" else {})}

    def _turn(self, decision: RouteDecision, sid: str, message: str) -> tuple[str, str]:
        timeout = self.routing.broker.task_turn_timeout_seconds
        timer = logs.Timer()
        result = self.runner.run(
            [self.assignments.host.nemoclaw_bin, decision.sandbox, "agent", "--agent", decision.openclaw_id,
             "--session-id", f"broker-{sid}", "--thinking", "off", "--json", "--timeout", str(timeout),
             "-m", message],
            timeout=timeout + 30,
        )
        logs.external("openclaw-gateway:agent", result.returncode, timer.ms, sandbox=decision.sandbox,
                      agent=decision.openclaw_id, session_id=sid)
        try:
            data = extract_json(result.stdout)
            payloads = data.get("result", {}).get("payloads", [])
            text = "\n".join(p.get("text") or "" for p in payloads).strip()
        except (ValueError, AttributeError):
            text = ""
        if not result.ok and not text:
            tail = " ".join((result.stderr or result.stdout).strip().split())[-240:]
            return f"task agent turn failed (rc={result.returncode}): {tail}", "error"
        return strip_markers(text) or "(empty reply)", "ok"

    # ---- drain (relocation) -----------------------------------------------------------------

    async def drain(self, agent: str, timeout: float = 60.0) -> dict:
        self.draining.add(agent)
        deadline = time.monotonic() + timeout
        while self.inflight.get(agent, 0) > 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        drained = self.inflight.get(agent, 0) == 0
        audit.record(kind="relocation", verdict="drained" if drained else "timeout", action="drain", agent=agent,
                     detail={"inflight": self.inflight.get(agent, 0)})
        return {"agent": agent, "drained": drained, "inflight": self.inflight.get(agent, 0)}

    def undrain(self, agent: str) -> dict:
        self.draining.discard(agent)
        return {"agent": agent, "draining": False}

    # ---- HTTP: auth -------------------------------------------------------------------------

    def _authorized(self, request: Request) -> bool:
        header = request.headers.get("authorization", "")
        return header.lower().startswith("bearer ") and hmac.compare_digest(header[7:].strip(), self.token)

    # ---- HTTP: MCP Streamable HTTP ----------------------------------------------------------

    async def mcp(self, request: Request) -> Response:
        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if request.method == "GET":
            return Response(status_code=405, headers={"Allow": "POST"})
        if request.method == "DELETE":
            return Response(status_code=204)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse(_rpc_error(None, -32700, "parse error"), status_code=400)
        caller = self.routing.entry.assistant_sandbox
        if isinstance(body, list):
            responses = [r for r in [await self._dispatch(m, caller) for m in body] if r is not None]
            if not responses:
                return Response(status_code=202)
            return JSONResponse(responses, headers={"Mcp-Session-Id": self.session_id})
        response = await self._dispatch(body, caller)
        if response is None:
            return Response(status_code=202, headers={"Mcp-Session-Id": self.session_id})
        return JSONResponse(response, headers={"Mcp-Session-Id": self.session_id})

    async def _dispatch(self, message: dict, caller: str) -> dict | None:
        if not isinstance(message, dict):
            return _rpc_error(None, -32600, "invalid request")
        method = message.get("method")
        ident = message.get("id")
        params = message.get("params") or {}
        if method == "initialize":
            requested = params.get("protocolVersion") or PROTOCOL_VERSION
            return _rpc_result(ident, {"protocolVersion": requested, "capabilities": {"tools": {"listChanged": False}},
                                       "serverInfo": SERVER_INFO})
        if method in ("notifications/initialized", "notifications/cancelled", "notifications/progress"):
            return None
        if method == "ping":
            return _rpc_result(ident, {})
        if method == "tools/list":
            return _rpc_result(ident, {"tools": TOOLS})
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if name == "list_task_agents":
                return _rpc_result(ident, _tool_text(json.dumps(self.list_agents(), ensure_ascii=False)))
            if name == "ask_task_agent":
                result = await self.ask(str(arguments.get("name", "")), str(arguments.get("query", "")),
                                        arguments.get("session_id"), caller)
                return _rpc_result(ident, _tool_text(json.dumps(result, ensure_ascii=False), is_error=not result["ok"]))
            return _rpc_error(ident, -32602, f"unknown tool {name!r}")
        if ident is None:
            return None
        return _rpc_error(ident, -32601, f"method not found: {method}")

    # ---- HTTP: REST fallback ----------------------------------------------------------------

    async def rest_agents(self, request: Request) -> Response:
        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse({"agents": self.list_agents()})

    async def rest_ask(self, request: Request) -> Response:
        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        result = await self.ask(str(body.get("name", "")), str(body.get("query", "")), body.get("session_id"),
                                self.routing.entry.assistant_sandbox)
        return JSONResponse(result, status_code=200 if result["ok"] else 409)


def _rpc_result(ident, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def _rpc_error(ident, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}


def _tool_text(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}
