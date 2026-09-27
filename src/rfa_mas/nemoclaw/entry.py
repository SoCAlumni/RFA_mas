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
from starlette.routing import Mount, Route

from rfa_mas.nemoclaw import audit
from rfa_mas.nemoclaw.ask_api import AskService, create_ask_app
from rfa_mas.nemoclaw.ask_contract import AskRequest
from rfa_mas.nemoclaw.broker import Broker
from rfa_mas.nemoclaw.censor import CensorPipeline
from rfa_mas.nemoclaw.config import Assignments, Routing
from rfa_mas.nemoclaw.markers import make_marker, strip_markers
from rfa_mas.nemoclaw.runner import Runner, extract_json

import httpx
import yaml

SESSION_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
AUDIT_HTML = Path(__file__).with_name("audit.html")
SAMPLES_PATH = Path(__file__).resolve().parents[3] / "deploy" / "nemoclaw" / "samples.yaml"


@dataclass
class Entry:
    assignments: Assignments
    routing: Routing
    pipeline: CensorPipeline
    broker: Broker
    secret: bytes
    runner: Runner
    replay: bool = False
    proxy_key: str | None = None
    proxy_url: str = "http://127.0.0.1:8797/v1/chat/completions"
    ask_service: AskService | None = None
    team_service: object | None = None
    _sandboxes_cache: tuple[float, list[str]] = (0.0, [])

    def app(self) -> Starlette:
        routes = [
            Route("/healthz", self.healthz, methods=["GET"]),
            Route("/channel/{channel}/chat", self.chat, methods=["POST"]),
            Route("/audit/", self.audit_page, methods=["GET"]),
            Route("/audit/api/events", self.audit_events, methods=["GET"]),
            Route("/audit/api/summary", self.audit_summary, methods=["GET"]),
            Route("/audit/api/samples", self.samples, methods=["GET"]),
            Route("/audit/api/run", self.run_sample, methods=["POST"]),
            Route("/audit/api/sandboxes", self.sandboxes, methods=["GET"]),
            Route("/broker/admin/drain/{agent}", self.drain, methods=["POST"]),
            Route("/broker/admin/undrain/{agent}", self.undrain, methods=["POST"]),
            Route("/broker/admin/state", self.broker_state, methods=["GET"]),
            Route("/broker/admin/ask", self.broker_ask, methods=["POST"]),
        ]
        if self.ask_service is not None:  # /ask, /chat, /teams (+ /docs, /openapi.json) — matched after the routes above
            routes.append(Mount("/", app=create_ask_app(self.ask_service, self.team_service)))
        return Starlette(routes=routes)

    async def healthz(self, request: Request) -> Response:
        return JSONResponse({"status": "ok", "service": "rfa-sg-entry", "channels": sorted(self.routing.channels),
                             "replay": self.replay})

    # ---- channel API --------------------------------------------------------------------------

    async def chat(self, request: Request) -> Response:
        channel = request.path_params["channel"]
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"code": "invalid_json"}, status_code=400)
        status, payload = await self.run_channel(channel, str(body.get("text") or ""), body.get("session_id"))
        return JSONResponse(payload, status_code=status)

    async def run_channel(self, channel: str, text: str, session_id: str | None) -> tuple[int, dict]:
        """The channel API: fix the session's channel, plant the signed marker, run the assistant,
        censor the final reply with the channel profile (same function as the proxy)."""
        spec = self.routing.channels.get(channel)
        if spec is None:
            return 404, {"code": "unknown_channel", "channels": sorted(self.routing.channels)}
        text = text.strip()
        if not text or len(text) > 10000:
            return 422, {"code": "text_required"}
        sid = session_id or f"s_{uuid.uuid4().hex[:12]}"
        if not SESSION_ID.match(str(sid)):
            return 422, {"code": "invalid_session_id"}
        started = time.monotonic()
        stored = audit.session_channel(sid)
        if stored is not None and stored != channel:
            return 409, {"code": "session_channel_mismatch", "session_channel": stored}
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
        return (201 if status == "ok" else 502), {
            "session_id": sid, "channel": channel, "profile": spec.profile, "alias": spec.alias,
            "reply": reply, "verdict": result.verdict, "redactions": result.redactions,
            "censor": result.summary(), "status": status, "ms": ms, "target": "assistant",
            "sandbox": self.routing.entry.assistant_sandbox,
        }

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

    # ---- dashboard test runner ------------------------------------------------------------------

    def load_samples(self) -> list[dict]:
        try:
            data = yaml.safe_load(SAMPLES_PATH.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return []
        return [s for s in data.get("samples", []) if isinstance(s, dict) and s.get("id") and s.get("text")]

    async def samples(self, request: Request) -> Response:
        return JSONResponse({"samples": self.load_samples(), "targets": self.targets()})

    def targets(self) -> list[dict]:
        out = [{"id": "assistant", "label": "assistant (채널 API 전 구간)", "sandbox": self.routing.entry.assistant_sandbox}]
        for agent_id, spec in self.assignments.agents.items():
            if spec.kind == "task" and spec.delegatable:  # the censor is not a delegation target
                out.append({"id": f"agent:{agent_id}", "label": f"{agent_id} (브로커 직접)",
                            "sandbox": self.assignments.sandbox_for(agent_id)})
        out.append({"id": "proxy", "label": "proxy (egress-proxy 직접, 가장 빠름)", "sandbox": None})
        if self.ask_service is not None:
            out.append({"id": "ask:self", "label": "ask() 개인 채팅 (audience self → internal 검열)", "sandbox": None})
            out.append({"id": "ask:public", "label": "ask() 공개 (audience public → public 검열, /ask 와 동일)", "sandbox": None})
        return out

    async def sandboxes(self, request: Request) -> Response:
        """Registered sandboxes (cached 30 s) so the dashboard can grey out unavailable targets."""
        now = time.monotonic()
        stamp, names = self._sandboxes_cache
        if now - stamp > 30:
            names = await asyncio.to_thread(self._list_sandboxes)
            self._sandboxes_cache = (now, names)
        return JSONResponse({"sandboxes": names, "declared": self.assignments.ordered_sandboxes()})

    def _list_sandboxes(self) -> list[str]:
        result = self.runner.run([self.assignments.host.nemoclaw_bin, "list", "--json"], timeout=60)
        if not result.ok:
            return []
        try:
            return sorted(e["name"] for e in extract_json(result.stdout).get("sandboxes", []))
        except (ValueError, AttributeError, KeyError):
            return []

    async def run_sample(self, request: Request) -> Response:
        """Run one sample (or free text) against a target and return the reply plus the censor
        summary; every hop still records to the audit ledger like a real request."""
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"code": "invalid_json"}, status_code=400)
        sample = next((s for s in self.load_samples() if s["id"] == body.get("sample_id")), {})
        channel = str(body.get("channel") or sample.get("channel") or self.routing.entry.default_channel)
        target = str(body.get("target") or sample.get("target") or "assistant")
        text = str(body.get("text") or sample.get("text") or "").strip()
        sid = body.get("session_id") or f"ui_{uuid.uuid4().hex[:10]}"
        spec = self.routing.channels.get(channel)
        if spec is None or not text:
            return JSONResponse({"code": "channel_and_text_required"}, status_code=422)
        started = time.monotonic()
        if target == "assistant":
            status, payload = await self.run_channel(channel, text, sid)
            return JSONResponse(payload, status_code=200 if status == 201 else status)
        if target.startswith("agent:"):
            agent = target.split(":", 1)[1]
            audit.remember_session(sid, channel, spec.profile)
            result = await self.broker.ask(agent, text, sid, None)
            censored = await asyncio.to_thread(self.pipeline.run, str(result.get("reply", "")), spec.profile)
            reply = censored.text if censored.verdict != "block" else f"[RFA censor blocked the reply: {censored.blocked_by}]"
            audit.record(kind="channel", verdict=censored.verdict if result.get("ok") else "error", action="dashboard-agent",
                         channel=channel, profile=spec.profile, sandbox=result.get("sandbox"), agent=agent, session_id=sid,
                         detail={"ms": int((time.monotonic() - started) * 1000), "redactions": censored.redactions,
                                 "route": result.get("route")})
            return JSONResponse({**result, "reply": reply, "verdict": censored.verdict, "redactions": censored.redactions,
                                 "censor": censored.summary(), "channel": channel, "profile": spec.profile,
                                 "target": target, "ms": int((time.monotonic() - started) * 1000),
                                 "status": "ok" if result.get("ok") else "error"},
                                status_code=200 if result.get("ok") else 502)
        if target.startswith("ask:") and self.ask_service is not None:
            audience = target.split(":", 1)[1]
            if audience not in self.ask_service.deps.config.audiences:
                return JSONResponse({"code": "unknown_audience"}, status_code=422)
            req = AskRequest(request_id=f"ui-{uuid.uuid4().hex[:12]}", question=text, channel="web", audience=audience,
                             target=f"dashboard:{sid}")
            status, payload = await self.ask_service.submit(req)
            censor = payload.get("censor") or {}
            refusal = payload.get("refusal")
            reply = payload.get("knowledge") or (f"[refusal {refusal['code']}: {refusal['message']}]" if refusal else "")
            return JSONResponse({**payload, "reply": reply, "verdict": censor.get("verdict"), "channel": channel,
                                 "profile": censor.get("profile"), "target": target, "session_id": sid,
                                 "redactions": {r["reason"]: 1 for r in censor.get("redactions", [])},
                                 "ms": int((time.monotonic() - started) * 1000), "status": "ok" if status == 200 else "error"},
                                status_code=200)
        if target == "proxy":
            if not self.proxy_key:
                return JSONResponse({"code": "proxy_key_unavailable"}, status_code=503)
            audit.remember_session(sid, channel, spec.profile)
            marker = make_marker("channel", {"ch": channel, "sid": sid}, self.secret)
            payload = {"model": self.routing.proxy.default_mode, "max_tokens": 300,
                       "messages": [{"role": "user", "content": f"{marker}\n{text}"}]}
            try:
                async with httpx.AsyncClient(timeout=self.routing.entry.hosted_turn_timeout_seconds + 10) as client:
                    response = await client.post(self.proxy_url, json=payload,
                                                 headers={"Authorization": f"Bearer {self.proxy_key}"})
            except httpx.HTTPError as exc:
                return JSONResponse({"code": "proxy_unreachable", "error": type(exc).__name__}, status_code=502)
            ms = int((time.monotonic() - started) * 1000)
            if response.status_code != 200:
                return JSONResponse({"code": "proxy_error", "status": response.status_code, "body": response.text[:300],
                                     "ms": ms}, status_code=502)
            data = response.json()
            content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "")
            events = audit.query(kind="inference", session_id=sid, limit=1)
            event = events[0] if events else {}
            detail = event.get("detail", {})
            return JSONResponse({"reply": content, "verdict": event.get("verdict"), "channel": channel,
                                 "profile": spec.profile, "target": "proxy", "alias": detail.get("alias"),
                                 "backend": detail.get("backend"), "upstream_model": detail.get("upstream_model"),
                                 "redactions": {"request": detail.get("request_redactions", 0),
                                                "response": detail.get("response_redactions", 0)},
                                 "blocked_by": detail.get("blocked_by"), "session_id": sid, "ms": ms, "status": "ok"})
        return JSONResponse({"code": "unknown_target", "targets": [t["id"] for t in self.targets()]}, status_code=422)

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
