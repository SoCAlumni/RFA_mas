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
from dataclasses import dataclass, field

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from rfa_mas.nemoclaw import audit
from rfa_mas.nemoclaw.config import Assignments, Routing
from rfa_mas.nemoclaw.markers import make_marker, strip_markers
from rfa_mas.nemoclaw.runner import Runner, extract_json

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

    async def ask(self, agent: str, query: str, session_id: str | None, caller_sandbox: str | None) -> dict:
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
        try:
            if self.replay:
                reply, status = f"(replay) {agent}@{decision.sandbox} would answer: {query[:60]}", "ok"
            else:
                reply, status = await asyncio.to_thread(self._turn, decision, sid, message)
        finally:
            self.inflight[agent] -= 1
        ms = int((time.monotonic() - started) * 1000)
        audit.record(kind="broker", verdict=status, action=decision.kind, channel=channel,
                     profile=self.routing.channels[channel].profile, sandbox=decision.sandbox, agent=agent,
                     session_id=sid, detail={"caller_sandbox": caller_sandbox, "ms": ms, "alias": decision.alias})
        return {"ok": status == "ok", "agent": agent, "sandbox": decision.sandbox, "route": decision.kind,
                "channel": channel, "session_id": sid, "reply": reply, "ms": ms,
                **({"error": reply} if status != "ok" else {})}

    def _turn(self, decision: RouteDecision, sid: str, message: str) -> tuple[str, str]:
        timeout = self.routing.broker.task_turn_timeout_seconds
        result = self.runner.run(
            [self.assignments.host.nemoclaw_bin, decision.sandbox, "agent", "--agent", decision.openclaw_id,
             "--session-id", f"broker-{sid}", "--thinking", "off", "--json", "--timeout", str(timeout),
             "-m", message],
            timeout=timeout + 30,
        )
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
