"""Channel API entry point + audit screen + broker admin (loopback only).

The channel (``internal`` / ``external``) is decided here, once per session, and propagated
as a signed marker in the message the assistant receives; every downstream hop (broker,
egress-proxy) reads the marker or the session store instead of re-deciding. The final reply
runs through the same censor function the proxy uses for that channel's profile.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from rfa_mas.nemoclaw import audit
from rfa_mas.nemoclaw.broker import Broker
from rfa_mas.nemoclaw.censor import CensorPipeline
from rfa_mas.nemoclaw.config import Assignments, Routing
from rfa_mas.nemoclaw.markers import make_marker, strip_markers
from rfa_mas.nemoclaw.runner import Runner, extract_json

SESSION_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
AUDIT_HTML = Path(__file__).with_name("audit.html")


@dataclass
class Entry:
    assignments: Assignments
    routing: Routing
    pipeline: CensorPipeline
    broker: Broker
    secret: bytes
    runner: Runner
    replay: bool = False

    def app(self) -> Starlette:
        return Starlette(routes=[
            Route("/healthz", self.healthz, methods=["GET"]),
            Route("/channel/{channel}/chat", self.chat, methods=["POST"]),
            Route("/audit/", self.audit_page, methods=["GET"]),
            Route("/audit/api/events", self.audit_events, methods=["GET"]),
            Route("/audit/api/summary", self.audit_summary, methods=["GET"]),
            Route("/broker/admin/drain/{agent}", self.drain, methods=["POST"]),
            Route("/broker/admin/undrain/{agent}", self.undrain, methods=["POST"]),
            Route("/broker/admin/state", self.broker_state, methods=["GET"]),
            Route("/broker/admin/ask", self.broker_ask, methods=["POST"]),
        ])

    async def healthz(self, request: Request) -> Response:
        return JSONResponse({"status": "ok", "service": "rfa-sg-entry", "channels": sorted(self.routing.channels),
                             "replay": self.replay})

    # ---- channel API --------------------------------------------------------------------------

    async def chat(self, request: Request) -> Response:
        channel = request.path_params["channel"]
        spec = self.routing.channels.get(channel)
        if spec is None:
            return JSONResponse({"code": "unknown_channel", "channels": sorted(self.routing.channels)}, status_code=404)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"code": "invalid_json"}, status_code=400)
        text = str(body.get("text") or "").strip()
        if not text or len(text) > 10000:
            return JSONResponse({"code": "text_required"}, status_code=422)
        sid = body.get("session_id") or f"s_{uuid.uuid4().hex[:12]}"
        if not SESSION_ID.match(str(sid)):
            return JSONResponse({"code": "invalid_session_id"}, status_code=422)
        started = time.monotonic()
        stored = audit.session_channel(sid)
        if stored is not None and stored != channel:
            return JSONResponse({"code": "session_channel_mismatch", "session_channel": stored}, status_code=409)
        audit.remember_session(sid, channel, spec.profile)
        marker = make_marker("channel", {"ch": channel, "sid": sid}, self.secret)
        message = f"{marker}\n{text}"
        if self.replay:
            raw_reply, status = f"(replay) assistant on {channel}: {text[:80]}", "ok"
        else:
            raw_reply, status = await asyncio.to_thread(self._assistant_turn, sid, message)
        result = await asyncio.to_thread(self.pipeline.run, raw_reply, spec.profile)
        reply = result.text if result.verdict != "block" else f"[RFA censor blocked the reply: {result.blocked_by}]"
        ms = int((time.monotonic() - started) * 1000)
        audit.record(kind="channel", verdict=result.verdict if status == "ok" else status, action="chat",
                     channel=channel, profile=spec.profile, sandbox=self.routing.entry.assistant_sandbox,
                     agent="assistant", session_id=sid,
                     detail={"ms": ms, "redactions": result.redactions, "stages": [s.id for s in result.stages],
                             "text_chars": len(text)})
        return JSONResponse({
            "session_id": sid, "channel": channel, "profile": spec.profile, "alias": spec.alias,
            "reply": reply, "verdict": result.verdict, "redactions": result.redactions,
            "censor": result.summary(), "status": status, "ms": ms,
        }, status_code=201 if status == "ok" else 502)

    def _assistant_turn(self, sid: str, message: str) -> tuple[str, str]:
        cfg = self.routing.entry
        timeout = cfg.hosted_turn_timeout_seconds
        result = self.runner.run(
            [self.assignments.host.nemoclaw_bin, cfg.assistant_sandbox, "agent", "--agent", cfg.assistant_agent,
             "--session-id", f"entry-{sid}", "--thinking", "off", "--json", "--timeout", str(timeout), "-m", message],
            timeout=timeout + 30,
        )
        try:
            data = extract_json(result.stdout)
            payloads = data.get("result", {}).get("payloads", [])
            text = "\n".join(p.get("text") or "" for p in payloads).strip()
        except (ValueError, AttributeError):
            text = ""
        if result.returncode == 124:
            return f"assistant turn timed out after {timeout}s", "timeout"
        if not result.ok and not text:
            return f"assistant turn failed (rc={result.returncode})", "error"
        return strip_markers(text) or "(empty reply)", "ok"

    # ---- audit screen -------------------------------------------------------------------------

    async def audit_page(self, request: Request) -> Response:
        return HTMLResponse(AUDIT_HTML.read_text(encoding="utf-8"))

    async def audit_events(self, request: Request) -> Response:
        limit = min(int(request.query_params.get("limit", "200")), 1000)
        kind = request.query_params.get("kind") or None
        session_id = request.query_params.get("session_id") or None
        return JSONResponse({"events": audit.query(limit=limit, kind=kind, session_id=session_id)})

    async def audit_summary(self, request: Request) -> Response:
        placement = self.assignments.placement()
        return JSONResponse({
            "counts": audit.counts(),
            "channels": {n: {"alias": c.alias, "profile": c.profile} for n, c in self.routing.channels.items()},
            "aliases": {n: {"backend": a.backend, "model": a.model, "censor": a.censor, "exposure": a.exposure}
                        for n, a in self.routing.aliases.items()},
            "agents": [{"agent": a, "sandbox": s, "kind": self.assignments.agents[a].kind,
                        "alias": self.assignments.agents[a].alias,
                        "groups": self.assignments.agents[a].groups or self.assignments.sandboxes[s].groups,
                        "draining": a in self.broker.draining}
                       for a, s in placement.items()],
            "mode": self.routing.proxy.default_mode,
        })

    # ---- broker admin (loopback) --------------------------------------------------------------

    async def drain(self, request: Request) -> Response:
        return JSONResponse(await self.broker.drain(request.path_params["agent"]))

    async def undrain(self, request: Request) -> Response:
        return JSONResponse(self.broker.undrain(request.path_params["agent"]))

    async def broker_state(self, request: Request) -> Response:
        return JSONResponse({"draining": sorted(self.broker.draining), "inflight": self.broker.inflight,
                             "agents": self.broker.list_agents()})

    async def broker_ask(self, request: Request) -> Response:
        """Operator-side direct ask (loopback), used by demos; the assistant uses MCP/REST."""
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"code": "invalid_json"}, status_code=400)
        result = await self.broker.ask(str(body.get("name", "")), str(body.get("query", "")),
                                       body.get("session_id"), body.get("caller_sandbox"))
        return JSONResponse(result, status_code=200 if result["ok"] else 409)


def dumps(data) -> str:
    return json.dumps(data, ensure_ascii=False)
