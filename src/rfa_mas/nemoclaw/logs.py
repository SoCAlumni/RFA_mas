"""Structured logging (JSON lines) for the security-group layer.

One app logger → ``logs/app.jsonl``: every line carries the bound context (``request_id``, ``run_id``,
``role``, ``audience``, ``profile``) plus ``stage``/``ms``/``outcome`` for pipeline stages, so one
``/ask`` or ``/chat`` request can be reconstructed from the log alone (``make logs-trace REQUEST_ID=…``).
The audit logger → ``logs/audit.jsonl`` (see ``audit.py``) never receives raw or pre-censor text; raw
text goes to ``logs/debug.jsonl`` only when ``LOG_RAW=1``. A masking filter strips bearer tokens and
API keys from every logger. External calls log a summary (target, status, ms), never bodies.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
CONTEXT_FIELDS = ("request_id", "run_id", "role", "audience", "profile")
_context: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("rfa_log_context", default=None)

# credentials in any string value: bearer headers, NVIDIA / OpenAI / Google style keys, KEY=value pairs
_SECRET = re.compile(
    r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"
    r"|(nvapi-|sk-|AIza)[A-Za-z0-9._~+/=-]{8,}"
    r"|((?:api[_-]?key|token|secret|password|authorization)\s*[=:]\s*)[\"']?[A-Za-z0-9._~+/=-]{8,}"
)


def mask(value: Any) -> Any:
    """Mask credential-looking substrings in strings; recurse into dicts/lists."""
    if isinstance(value, str):
        return _SECRET.sub(lambda m: (m.group(1) or m.group(2) or m.group(3) or "") + "***", value)
    if isinstance(value, dict):
        return {k: ("***" if _is_secret_key(k) and v not in (None, "") else mask(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask(v) for v in value]
    return value


def _is_secret_key(key: Any) -> bool:
    k = str(key).lower()
    return k.endswith(("_key", "_token", "_secret", "password")) or k in ("authorization", "token", "api_key", "apikey")


# --------------------------------------------------------------------------- context


def bind(**fields: Any) -> None:
    """Bind context fields (request_id, run_id, role, audience, profile) for the current task/thread."""
    current = dict(_context.get() or {})
    for key, value in fields.items():
        if value is None:
            current.pop(key, None)
        else:
            current[key] = value
    _context.set(current)


def context() -> dict[str, Any]:
    return dict(_context.get() or {})


def reset() -> None:
    _context.set({})


class _Bound:
    def __init__(self, fields: dict[str, Any]):
        self.fields, self.token = fields, None

    def __enter__(self):
        self.token = _context.set({**(_context.get() or {}), **{k: v for k, v in self.fields.items() if v is not None}})
        return self

    def __exit__(self, *exc):
        if self.token is not None:
            _context.reset(self.token)


def bound(**fields: Any) -> _Bound:
    """``with logs.bound(request_id=…):`` — scoped binding (restored on exit)."""
    return _Bound(fields)


# --------------------------------------------------------------------------- files / loggers


def log_dir() -> Path:
    """``RFA_LOG_DIR`` → that; else next to ``RFA_SG_AUDIT_DB`` (test isolation); else ``<repo>/logs``."""
    if env := os.environ.get("RFA_LOG_DIR"):
        return Path(env)
    if db := os.environ.get("RFA_SG_AUDIT_DB"):
        return Path(db).parent
    return ROOT / "logs"


class JsonLinesHandler(logging.Handler):
    """Append one JSON object per record to ``path`` (re-resolved per record so tests can redirect)."""

    def __init__(self, name: str):
        super().__init__()
        self.name_ = name

    def emit(self, record: logging.LogRecord) -> None:
        try:
            path = log_dir() / f"{self.name_}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(_payload(record), ensure_ascii=False, default=str) + "\n")
        except Exception:  # logging must never break the request
            self.handleError(record)


def _payload(record: logging.LogRecord) -> dict:
    fields: dict = getattr(record, "fields", {}) or {}
    out = {"ts": round(record.created, 3), "level": record.levelname.lower(), "logger": record.name,
           "event": record.getMessage()}
    out.update({k: v for k, v in context().items() if k in CONTEXT_FIELDS})
    out.update(mask(fields))
    return out


_LOGGERS: dict[str, logging.Logger] = {}


def _logger(name: str, file: str, level: int = logging.INFO) -> logging.Logger:
    if name in _LOGGERS:
        return _LOGGERS[name]
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False
    if not any(isinstance(h, JsonLinesHandler) for h in logger.handlers):
        logger.addHandler(JsonLinesHandler(file))
    _LOGGERS[name] = logger
    return logger


def app() -> logging.Logger:
    return _logger("rfa.app", "app")


def audit_logger() -> logging.Logger:
    return _logger("rfa.audit", "audit")


def debug_logger() -> logging.Logger:
    return _logger("rfa.debug", "debug", logging.DEBUG)


def raw_enabled() -> bool:
    return os.environ.get("LOG_RAW", "") == "1"


# --------------------------------------------------------------------------- helpers


def event(name: str, level: int = logging.INFO, **fields: Any) -> None:
    app().log(level, name, extra={"fields": fields})


def stage(name: str, ms: int | float, outcome: str, **fields: Any) -> None:
    """One line per pipeline stage: ``stage=blocklist|rules|head|broker|task:<id>|censor|options|store``."""
    event("stage", stage=name, ms=int(ms), outcome=outcome, **fields)


def external(target: str, status: int | str | None, ms: int | float, **summary: Any) -> None:
    """External call summary (OpenClaw gateway, approval server, hosted LLM): no bodies."""
    event("external", target=target, status=status, ms=int(ms), **summary)


def error(name: str, **fields: Any) -> None:
    event(name, logging.ERROR, **fields)


def raw(kind: str, text: str, **fields: Any) -> None:
    """Pre-censor / raw text: debug logger only, and only when LOG_RAW=1."""
    if raw_enabled():
        debug_logger().debug("raw", extra={"fields": {"kind": kind, "text": text, **fields}})


class Timer:
    def __init__(self):
        self.started = time.monotonic()

    @property
    def ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)


# --------------------------------------------------------------------------- reading back


def read(name: str = "app", *, request_id: str | None = None, run_id: str | None = None, limit: int | None = None,
         **match: Any) -> list[dict]:
    """Read ``logs/<name>.jsonl`` (oldest first), filtered by request_id / run_id / any equal field."""
    path = log_dir() / f"{name}.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if request_id and rec.get("request_id") != request_id:
            continue
        if run_id and rec.get("run_id") != run_id:
            continue
        if any(rec.get(k) != v for k, v in match.items()):
            continue
        out.append(rec)
    return out[-limit:] if limit else out


def trace(request_id: str) -> list[dict]:
    """Every app + audit line of one request, in time order (``make logs-trace REQUEST_ID=…``)."""
    lines = read("app", request_id=request_id) + read("audit", request_id=request_id)
    return sorted(lines, key=lambda r: r.get("ts", 0))


# --------------------------------------------------------------------------- ASGI middleware


class RequestLogMiddleware:
    """Bind ``request_id`` (``X-Request-Id`` from the client, else generated) for the request, echo it in
    the response header and write one ``request`` line: method, path, status, ms."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        import uuid

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        request_id = headers.get("x-request-id") or f"req-{uuid.uuid4().hex[:12]}"
        timer = Timer()
        status_box = {"status": None}

        async def send_wrapped(message):
            if message["type"] == "http.response.start":
                status_box["status"] = message["status"]
                message.setdefault("headers", [])
                message["headers"] = [*message["headers"], (b"x-request-id", request_id.encode())]
            await send(message)

        with bound(request_id=request_id):
            try:
                await self.app(scope, receive, send_wrapped)
            except Exception as exc:
                error("request", method=scope.get("method"), path=scope.get("path"), status=500, ms=timer.ms,
                      exception=type(exc).__name__)
                raise
            event("request", method=scope.get("method"), path=scope.get("path"), status=status_box["status"],
                  ms=timer.ms)


__all__ = ["RequestLogMiddleware", "bind", "bound", "context", "reset", "event", "stage", "external", "error", "raw", "mask", "Timer",
           "app", "audit_logger", "debug_logger", "log_dir", "read", "trace", "raw_enabled", "CONTEXT_FIELDS"]
