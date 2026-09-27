"""NemoClaw operating channel: an authenticated LAN gateway in front of the PoC (P1-008M).

The RFA core stays a host process; the NemoClaw/OpenShell sandbox agent reaches it only
through this gateway, which
- requires its own bearer key (a file the owner uploads into the sandbox, never argv/env),
- exposes one assistant entry (`POST /channel/chat`) that runs the same PoC chat pipeline
  as the UI (LLM intent/assignee reasoning, Task teams, KB, stage ledger) plus `/healthz`,
- forwards nothing else (OpenShell's L7 policy is the outer fence, this is the inner one).

Nothing here sandboxes the core itself, grants per-role identity, or bypasses the core's
own authorization: the sandbox agent acts as the installation owner on this channel.
"""

from __future__ import annotations

import hmac
import re
import secrets
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from rfa_mas.application.chat import ChatMessage
from rfa_mas.errors import RfaError

OPAQUE = r"^[A-Za-z0-9_][A-Za-z0-9_.:-]{0,159}$"
CHANNEL_ROUTES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GET", re.compile(r"^/healthz$")),
    ("POST", re.compile(r"^/channel/chat$")),
)
MAX_BODY = 64_000


class ChannelChat(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    text: str = Field(min_length=1, max_length=10000)
    session_id: str | None = Field(default=None, pattern=OPAQUE)
    message_id: str | None = Field(default=None, pattern=OPAQUE)


def load_or_create_key(path: Path) -> str:
    """Owner-held channel key (0600). Created once; never printed by the PoC."""
    path = path.expanduser()
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if not value:
            raise ValueError("channel_key_empty")
        return value
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    value = secrets.token_urlsafe(32)
    path.touch(mode=0o600)
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)
    return value


def route_allowed(method: str, path: str) -> bool:
    return any(method == m and pattern.match(path) for m, pattern in CHANNEL_ROUTES)


def _json(status: int, payload: dict) -> JSONResponse:
    return JSONResponse(payload, status_code=status)


class ChannelGateway:
    """ASGI app: bearer auth, then healthz (core) or the PoC chat pipeline."""

    def __init__(self, core_app, chat, key: str) -> None:
        self.core_app, self.chat = core_app, chat
        self._expected = f"Bearer {key}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await _json(404, {"code": "channel_unsupported"})(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        if not hmac.compare_digest(headers.get(b"authorization", b""), self._expected):
            await _json(
                401, {"code": "authentication_required", "message": "채널 인증이 필요합니다."}
            )(scope, receive, send)
            return
        method, path = scope.get("method", ""), scope.get("path", "")
        if not route_allowed(method, path):
            await _json(
                403,
                {
                    "code": "channel_route_denied",
                    "message": "이 채널에서 허용되지 않은 경로입니다.",
                },
            )(scope, receive, send)
            return
        if path == "/healthz":
            # Present the (already authenticated) channel request as a loopback peer.
            forwarded = {
                "headers": [
                    (n, v)
                    for n, v in scope.get("headers") or []
                    if n != b"authorization" and not n.startswith((b"forwarded", b"x-"))
                ],
                "client": ("127.0.0.1", 0),
            }
            await self.core_app({**scope, **forwarded}, receive, send)
            return
        response = await self.chat_turn(Request(scope, receive))
        await response(scope, receive, send)

    async def chat_turn(self, request: Request) -> JSONResponse:
        raw = await request.body()
        if len(raw) > MAX_BODY:
            return _json(413, {"code": "channel_body_too_large"})
        try:
            body = ChannelChat.model_validate_json(raw)
        except ValidationError:
            return _json(422, {"code": "invalid_request", "message": "text가 필요합니다."})
        try:
            session_id = body.session_id
            if session_id is None:
                session_id = (await self.chat.call("POST", "/v1/sessions", {}))["session_id"]
            message_id = body.message_id or "nemoclaw-" + secrets.token_urlsafe(12)
            turn = await self.chat.send(
                session_id, ChatMessage(text=body.text, message_id=message_id)
            )
        except RfaError as exc:
            status = {"not_found": 404, "outcome_unknown": 502}.get(exc.code, 409)
            return _json(status, {"code": exc.code, "message": exc.message})
        route = turn.get("route") or {}
        return _json(
            201,
            {
                "session_id": session_id,
                "message_id": message_id,
                "intent": turn.get("intent"),
                "status": turn.get("status"),
                "route": {k: route.get(k) for k in ("kind", "label", "reason", "task_id")},
                "stages": [s.get("label") for s in turn.get("stages", [])],
                "reply": turn.get("reply", ""),
                "run_id": turn.get("run_id"),
                "source_id": turn.get("source_id"),
                "answer_model": turn.get("answer_model"),
                "team": turn.get("team"),
                "channel": "nemoclaw",
            },
        )


__all__ = ["CHANNEL_ROUTES", "ChannelChat", "ChannelGateway", "load_or_create_key", "route_allowed"]
