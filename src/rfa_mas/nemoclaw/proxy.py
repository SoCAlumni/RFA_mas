"""Egress-proxy: the single OpenAI-compatible inference provider every sandbox route points at.

The OpenShell gateway forwards ``inference.local`` here with the route model name as ``model``
and no sandbox identity, so the proxy attributes each request from signed in-band markers
(channel marker in user messages, agent marker in the system prompt), resolves a model alias
(``routing.yaml``), censors the request and the response for that alias's profile
(``censors.yaml``) and forwards to the alias backend (hosted build.nvidia.com; no local LLM).
The ``rfa-censor`` alias is hard-wired to bypass censoring so the censor LLM stage never recurses.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from rfa_mas.nemoclaw import audit, logs
from rfa_mas.nemoclaw.censor import CensorPipeline, CensorResult
from rfa_mas.nemoclaw.config import AUTO_MODE, Routing
from rfa_mas.nemoclaw.markers import find_markers

BLOCKED_TEXT = "[RFA censor blocked this content: {reason}. Rephrase without internal data.]"
UPSTREAM_TIMEOUT = 120.0


@dataclass
class Attribution:
    channel: str | None = None
    channel_alias: str | None = None
    agent: str | None = None
    sandbox: str | None = None
    agent_alias: str | None = None
    session_id: str | None = None
    tampered: int = 0
    notes: list[str] = field(default_factory=list)


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def attribute(messages: list[dict], routing: Routing, secret: bytes) -> Attribution:
    """Least-exposure attribution: copied markers can only downgrade; the bypass alias is
    honoured only from the system prompt (the censor sandbox's own workspace)."""
    attr = Attribution()
    agent_candidates: list[tuple[int, str, str, str]] = []
    channel_candidates: list[tuple[int, str, str, str | None]] = []
    for message in messages:
        role = message.get("role")
        for marker in find_markers(_text_of(message.get("content")), secret):
            if not marker.verified:
                attr.tampered += 1
                continue
            if marker.kind == "agent":
                alias = marker.fields.get("alias", "")
                if alias not in routing.aliases:
                    continue
                if alias == routing.bypass_alias and role != "system":
                    attr.tampered += 1
                    attr.notes.append("bypass marker outside system prompt ignored")
                    continue
                agent_candidates.append((routing.aliases[alias].exposure, alias,
                                         marker.fields.get("agent", "?"), marker.fields.get("sandbox", "?")))
            elif marker.kind == "channel" and role == "user":
                channel = marker.fields.get("ch", "")
                spec = routing.channels.get(channel)
                if spec is None:
                    continue
                channel_candidates.append((routing.aliases[spec.alias].exposure, spec.alias, channel,
                                           marker.fields.get("sid")))
    if agent_candidates:
        bypass = [c for c in agent_candidates if c[1] == routing.bypass_alias]
        chosen = bypass[0] if bypass else min(agent_candidates)
        attr.agent_alias, attr.agent, attr.sandbox = chosen[1], chosen[2], chosen[3]
    if channel_candidates:
        chosen = min(channel_candidates)
        attr.channel_alias, attr.channel, attr.session_id = chosen[1], chosen[2], chosen[3]
    return attr


class EgressProxy:
    def __init__(
        self,
        routing: Routing,
        pipeline: CensorPipeline,
        secret: bytes,
        proxy_key: str,
        backend_keys: dict[str, str],
        *,
        replay: bool = False,
        client: httpx.AsyncClient | None = None,
    ):
        self.routing = routing
        self.pipeline = pipeline
        self.secret = secret
        self.proxy_key = proxy_key
        self.backend_keys = backend_keys
        self.replay = replay
        self.client = client or httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT)
        self.last_mode = routing.proxy.default_mode
        self.requests_seen = 0
        self.app = Starlette(routes=[
            Route("/healthz", self.healthz, methods=["GET"]),
            Route("/v1/models", self.models, methods=["GET"]),
            Route("/v1/chat/completions", self.chat, methods=["POST"]),
        ])

    # ---- helpers ------------------------------------------------------------------------

    def _authorized(self, request: Request) -> bool:
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return False
        return hmac.compare_digest(header[7:].strip(), self.proxy_key)

    async def healthz(self, request: Request) -> JSONResponse:
        return JSONResponse({
            "status": "ok", "service": "rfa-egress-proxy", "mode": self.last_mode,
            "aliases": sorted(self.routing.aliases), "replay": self.replay, "requests": self.requests_seen,
        })

    async def models(self, request: Request) -> JSONResponse:
        if not self._authorized(request):
            return JSONResponse({"error": {"message": "unauthorized"}}, status_code=401)
        ids = [AUTO_MODE, *sorted(self.routing.aliases)]
        return JSONResponse({"object": "list", "data": [{"id": i, "object": "model", "owned_by": "rfa-sg"} for i in ids]})

    async def _run(self, text: str, profile: str, stages: tuple[str, ...] | None = None) -> CensorResult:
        """Censor stages may call a sandbox agent (seconds); keep them off the event loop."""
        return await asyncio.to_thread(self.pipeline.run, text, profile, stages)

    async def _censor_arguments(self, arguments: str, profile: str) -> tuple[str, int, bool]:
        """Regex-only pass over the string leaves of tool-call arguments (JSON-aware so escaped
        unicode from upstream cannot slip past the keyword rules)."""
        try:
            parsed = json.loads(arguments)
        except ValueError:
            result = await self._run(arguments, profile, ("regex",))
            return result.text, result.redacted_count, result.verdict == "block"
        redacted, blocked = 0, False

        async def walk(node):
            nonlocal redacted, blocked
            if isinstance(node, str):
                if not node:
                    return node
                result = await self._run(node, profile, ("regex",))
                if result.verdict == "block":
                    blocked = True
                redacted += result.redacted_count
                return result.text
            if isinstance(node, list):
                return [await walk(item) for item in node]
            if isinstance(node, dict):
                return {key: await walk(value) for key, value in node.items()}
            return node

        censored = await walk(parsed)
        return json.dumps(censored, ensure_ascii=False), redacted, blocked

    async def _censor_messages(self, messages: list[dict], profile: str) -> tuple[list[dict], list[CensorResult], CensorResult | None]:
        """Regex on every message; the LLM judge only on the newest non-system message. The system
        prompt is OpenClaw's own text and earlier turns were judged when they were the newest one, so
        one judge call per request is enough (2026-09-27, user decision: each hosted judge call takes
        1-9 s and hangs to the timeout about one time in four)."""
        out, reports = [], []
        newest = max((i for i, m in enumerate(messages) if m.get("role") != "system"), default=-1)
        for i, message in enumerate(messages):
            content = message.get("content")
            if isinstance(content, str) and content:
                result = await self._run(content, profile, None if i == newest else ("regex",))
                reports.append(result)
                if result.verdict == "block":
                    return out, reports, result
                message = {**message, "content": result.text}
            out.append(message)
        return out, reports, None

    async def _upstream(self, alias_name: str, body: dict, messages: list[dict]) -> tuple[int, dict]:
        alias = self.routing.aliases[alias_name]
        backend = self.routing.backends[alias.backend]
        if self.replay:
            text = f"(replay) {alias_name} answer to: {_text_of(messages[-1].get('content'))[:80]}"
            return 200, _completion(alias.model, text)
        headers = {"content-type": "application/json"}
        if backend.auth == "bearer":
            headers["authorization"] = f"Bearer {self.backend_keys.get(alias.backend, '')}"
        url = f"{backend.url.rstrip('/')}/chat/completions"
        payload = upstream_payload(body, alias.model, messages, backend.chat_template_kwargs, backend.extras,
                                   backend.tool_call_extras)
        timer = logs.Timer()
        try:
            response = await self.client.post(url, json=payload, headers=headers, timeout=UPSTREAM_TIMEOUT)
            logs.external(f"upstream:{alias.backend}", response.status_code, timer.ms, model=alias.model,
                          messages=len(messages))
        except httpx.HTTPError as exc:
            logs.external(f"upstream:{alias.backend}", type(exc).__name__, timer.ms, model=alias.model)
            return 502, {"error": {"message": f"upstream {alias.backend} unreachable: {type(exc).__name__}", "type": "upstream_error"}}
        if response.status_code != 200:
            # the provider's error body is diagnostic text, not user content: keep a masked, truncated copy
            logs.error("upstream_error", target=f"upstream:{alias.backend}", status=response.status_code,
                       model=alias.model, body=response.text[:600],
                       request_fields=sorted(payload), tools=len(payload.get("tools") or []),
                       roles=[m.get("role") for m in messages][-6:])
            return 502, {"error": {"message": f"upstream {alias.backend} returned HTTP {response.status_code}", "type": "upstream_error"}}
        try:
            data = response.json()
        except ValueError:
            return 502, {"error": {"message": "upstream returned non-JSON", "type": "upstream_error"}}
        return 200, normalize_completion(data, alias.model)

    # ---- main route ------------------------------------------------------------------------

    async def chat(self, request: Request):
        if not self._authorized(request):
            audit.record(kind="inference", verdict="unauthorized", action="reject",
                         detail={"reason": "missing or wrong bearer (OpenShell credential not injected?)"})
            return JSONResponse({"error": {"message": "unauthorized"}}, status_code=401)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": {"message": "invalid JSON"}}, status_code=400)
        started = time.monotonic()
        self.requests_seen += 1
        messages = [m for m in (body.get("messages") or []) if isinstance(m, dict)]
        mode = str(body.get("model") or self.routing.proxy.default_mode)
        self.last_mode = mode
        attr = attribute(messages, self.routing, self.secret)
        alias_name, reason = self.routing.resolve_alias(mode, attr.channel_alias, attr.agent_alias)
        alias = self.routing.aliases[alias_name]
        profile = alias.censor
        censoring = profile not in ("none", "bypass")
        stream = bool(body.get("stream"))
        base_detail = {
            "mode": mode, "alias": alias_name, "reason": reason, "backend": alias.backend,
            "messages": len(messages), "tampered_markers": attr.tampered, "stream": stream,
        }
        # request side
        if censoring:
            messages, request_reports, blocked = await self._censor_messages(messages, profile)
            base_detail["request_redactions"] = sum(r.redacted_count for r in request_reports)
            if blocked is not None:
                base_detail["blocked_by"] = blocked.blocked_by
                base_detail["censor"] = blocked.summary()["stages"]
                audit.record(kind="inference", verdict="block", action="request", channel=attr.channel,
                             profile=profile, sandbox=attr.sandbox, agent=attr.agent,
                             session_id=attr.session_id, detail={**base_detail, "ms": _ms(started)})
                return self._respond(mode, _completion(mode, BLOCKED_TEXT.format(reason=blocked.blocked_by)), stream)
        status, data = await self._upstream(alias_name, body, messages)
        if status != 200:
            audit.record(kind="inference", verdict="error", action="upstream", channel=attr.channel,
                         profile=profile, sandbox=attr.sandbox, agent=attr.agent, session_id=attr.session_id,
                         detail={**base_detail, "ms": _ms(started), "error": data.get("error", {}).get("message")})
            return JSONResponse(data, status_code=status)
        # response side
        verdict = "allow"
        response_redactions = 0
        if censoring:
            for choice in data.get("choices") or []:
                message = choice.get("message") or {}
                content = message.get("content")
                if isinstance(content, str) and content:
                    # hosted output is ingress: regex only here; the LLM judge runs once on the final reply
                    result = await self._run(content, profile, ("regex",))
                    response_redactions += result.redacted_count
                    if result.verdict == "block":
                        verdict = "block"
                        message["content"] = BLOCKED_TEXT.format(reason=result.blocked_by)
                        message.pop("tool_calls", None)
                        base_detail["blocked_by"] = result.blocked_by
                        base_detail["censor"] = result.summary()["stages"]
                        break
                    message["content"] = result.text
                    if result.verdict == "redact":
                        verdict = "redact"
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    args = function.get("arguments")
                    if isinstance(args, str) and args:
                        new_args, redacted, blocked = await self._censor_arguments(args, profile)
                        if blocked:
                            function["arguments"] = "{}"
                            verdict = "block"
                        else:
                            function["arguments"] = new_args
                            response_redactions += redacted
        data["model"] = mode
        audit.record(kind="inference", verdict=verdict, action="completion", channel=attr.channel,
                     profile=profile, sandbox=attr.sandbox, agent=attr.agent, session_id=attr.session_id,
                     detail={**base_detail, "response_redactions": response_redactions, "ms": _ms(started),
                             "upstream_model": alias.model})
        return self._respond(mode, data, stream)

    def _respond(self, mode: str, data: dict, stream: bool):
        if not stream:
            return JSONResponse(data)
        return StreamingResponse(_sse(data, mode), media_type="text/event-stream",
                                 headers={"cache-control": "no-cache"})


# OpenAI chat.completions request fields every supported provider (build.nvidia.com, Gemini's OpenAI
# endpoint) accepts. Anything else OpenClaw sends is dropped so both providers see the same call.
UPSTREAM_FIELDS = frozenset({
    "model", "messages", "tools", "tool_choice", "temperature", "top_p", "max_tokens", "max_completion_tokens",
    "stop", "n", "presence_penalty", "frequency_penalty", "response_format", "seed", "user", "parallel_tool_calls",
})


def upstream_payload(body: dict, model: str, messages: list[dict], chat_template_kwargs: dict | None = None,
                     extras: dict | None = None, tool_call_extras: dict | None = None) -> dict:
    """Provider-neutral request: allowlisted OpenAI fields, buffered (no stream), the alias model,
    plus the backend's own extras (NVIDIA ``chat_template_kwargs``, Gemini ``reasoning_effort``) and
    per-tool-call extras for replayed assistant calls that lack them (Gemini ``thought_signature``)."""
    payload = {k: v for k, v in body.items() if k in UPSTREAM_FIELDS}
    payload["model"] = model
    payload["messages"] = messages
    if tool_call_extras:
        payload["messages"] = [_with_tool_call_extras(m, tool_call_extras) for m in messages]
    payload["stream"] = False
    if chat_template_kwargs:
        payload["chat_template_kwargs"] = dict(chat_template_kwargs)
    if extras:
        payload.update(extras)
    return payload


def _with_tool_call_extras(message: dict, extras: dict) -> dict:
    if message.get("role") != "assistant" or not message.get("tool_calls"):
        return message
    calls = []
    for call in message["tool_calls"]:
        if isinstance(call, dict) and not any(k in call for k in extras):
            call = {**call, **extras}
        calls.append(call)
    return {**message, "tool_calls": calls}


def normalize_completion(data: dict, model: str) -> dict:
    """Provider-neutral response: OpenAI ``chat.completion`` shape with string ``content`` on every
    choice, ``finish_reason`` and ``usage`` always present, provider-specific extras dropped."""
    choices = []
    for i, choice in enumerate(data.get("choices") or []):
        message = dict(choice.get("message") or {})
        content = message.get("content")
        if isinstance(content, list):  # content-part arrays → concatenated text
            content = "".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
        out = {"role": message.get("role") or "assistant", "content": content if isinstance(content, str) else ""}
        calls = []
        for j, call in enumerate(message.get("tool_calls") or []):
            function = call.get("function") or {}
            arguments = function.get("arguments", "")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False)
            normalized = {"id": call.get("id") or f"call_{j}", "type": "function",
                          "function": {"name": function.get("name") or "", "arguments": arguments}}
            if call.get("extra_content"):  # provider signature (Gemini thought_signature): keep for clients that echo it
                normalized["extra_content"] = call["extra_content"]
            calls.append(normalized)
        if calls:
            out["tool_calls"] = calls
        choices.append({"index": choice.get("index", i), "message": out,
                        "finish_reason": choice.get("finish_reason") or ("tool_calls" if calls else "stop")})
    usage = data.get("usage") or {}
    return {
        "id": data.get("id") or f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion",
        "created": data.get("created") or int(time.time()), "model": model, "choices": choices,
        "usage": {"prompt_tokens": int(usage.get("prompt_tokens") or 0),
                  "completion_tokens": int(usage.get("completion_tokens") or 0),
                  "total_tokens": int(usage.get("total_tokens") or 0)},
    }


def _completion(model: str, text: str) -> dict:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion", "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


async def _sse(data: dict, model: str) -> AsyncIterator[bytes]:
    """Re-emit a buffered completion as OpenAI chat.completion.chunk events."""
    ident = data.get("id") or f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = data.get("created") or int(time.time())

    def chunk(delta: dict, finish: str | None = None, index: int = 0) -> bytes:
        payload = {"id": ident, "object": "chat.completion.chunk", "created": created, "model": model,
                   "choices": [{"index": index, "delta": delta, "finish_reason": finish}]}
        return b"data: " + json.dumps(payload, ensure_ascii=False).encode() + b"\n\n"

    for choice in data.get("choices") or []:
        index = choice.get("index", 0)
        message = choice.get("message") or {}
        yield chunk({"role": "assistant"}, index=index)
        content = message.get("content")
        if content:
            yield chunk({"content": content}, index=index)
        calls = message.get("tool_calls") or []
        if calls:
            yield chunk({"tool_calls": [
                {"index": i, "id": c.get("id"), "type": c.get("type", "function"),
                 "function": {"name": (c.get("function") or {}).get("name"),
                              "arguments": (c.get("function") or {}).get("arguments", "")}}
                for i, c in enumerate(calls)]}, index=index)
        yield chunk({}, finish=choice.get("finish_reason") or "stop", index=index)
    if data.get("usage"):
        usage = {"id": ident, "object": "chat.completion.chunk", "created": created, "model": model,
                 "choices": [], "usage": data["usage"]}
        yield b"data: " + json.dumps(usage).encode() + b"\n\n"
    yield b"data: [DONE]\n\n"


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
