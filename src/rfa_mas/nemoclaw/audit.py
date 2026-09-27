"""Audit ledger shared by the proxy, broker, channel entry, ask() and controller.

Events are JSON lines in ``logs/audit.jsonl`` (the audit screen reads this file; there is no other
store). One line per decision: kind, channel, profile, sandbox/agent, verdict, action, detail plus the
normalised censor fields ``redactions[]``, ``option_chosen``, ``rule_hit``, ``blocklist_hit`` and the
bound ``request_id``/``run_id``. Text bodies are never stored — only counts, rule ids, opaque ids and
durations. Session → channel bindings (state, not audit) stay in a small SQLite file.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from rfa_mas.nemoclaw import logs

_DEFAULT = Path(__file__).resolve().parents[3] / ".local" / "sg" / "audit.db"
_lock = threading.Lock()
_seq = 0
KINDS = ("inference", "broker", "channel", "policy", "request", "approval", "relocation", "censor", "ask", "team",
         "chat", "options", "rules", "blocklist", "tasks")


def db_path() -> Path:
    return Path(os.environ.get("RFA_SG_AUDIT_DB") or _DEFAULT)


def ledger_path() -> Path:
    return logs.log_dir() / "audit.jsonl"


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, channel TEXT NOT NULL,"
        " profile TEXT NOT NULL, created REAL NOT NULL)"
    )
    return conn


def remember_session(session_id: str, channel: str, profile: str) -> None:
    """The channel API entry point fixes a session's channel once; later callers only read it."""
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO sessions (session_id, channel, profile, created) VALUES (?,?,?,?)",
                (session_id, channel, profile, time.time()),
            )
            conn.commit()
        finally:
            conn.close()


def session_channel(session_id: str) -> str | None:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT channel FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        finally:
            conn.close()
    return row[0] if row else None


def _redaction_reasons(detail: dict) -> list[str]:
    red = detail.get("redactions")
    if isinstance(red, dict):
        return [str(k) for k, n in red.items() if k not in ("request", "response") and n]
    if isinstance(red, list):
        return [str(r.get("reason") if isinstance(r, dict) else r) for r in red]
    return []


def record(
    *,
    kind: str,
    verdict: str | None,
    action: str | None = None,
    channel: str | None = None,
    profile: str | None = None,
    sandbox: str | None = None,
    agent: str | None = None,
    session_id: str | None = None,
    detail: dict | None = None,
    option_chosen: str | None = None,
    rule_hit: str | None = None,
    blocklist_hit: str | None = None,
) -> int:
    global _seq
    if kind not in KINDS:
        raise ValueError(f"unknown audit kind {kind}")
    detail = dict(detail or {})
    ctx = logs.context()
    with _lock:
        _seq += 1
        ident = int(time.time_ns() // 1000)  # microsecond stamp doubles as a monotonic id within a process
        row = {
            "id": ident, "ts": time.time(), "kind": kind, "channel": channel, "profile": profile, "sandbox": sandbox,
            "agent": agent, "session_id": session_id, "verdict": verdict, "action": action,
            "request_id": ctx.get("request_id") or detail.get("request_id"), "run_id": ctx.get("run_id"),
            "role": ctx.get("role"), "audience": ctx.get("audience") or detail.get("audience"),
            "redactions": _redaction_reasons(detail),
            "option_chosen": option_chosen, "rule_hit": rule_hit, "blocklist_hit": blocklist_hit,
            "detail": logs.mask(detail),
        }
        path = ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    return ident


def _rows() -> list[dict]:
    path = ledger_path()
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def query(
    *, limit: int = 100, kind: str | None = None, session_id: str | None = None, since: float | None = None,
    request_id: str | None = None,
) -> list[dict]:
    """Newest first (same shape the SQLite ledger returned, plus the new normalised fields)."""
    out = []
    for row in reversed(_rows()):
        if kind and row.get("kind") != kind:
            continue
        if session_id and row.get("session_id") != session_id:
            continue
        if since and row.get("ts", 0) < since:
            continue
        if request_id and row.get("request_id") != request_id:
            continue
        out.append(row)
        if len(out) >= limit:
            break
    return out


def counts(since: float | None = None) -> dict:
    out: dict = {}
    for row in _rows():
        if since and row.get("ts", 0) < since:
            continue
        out.setdefault(row["kind"], {})
        key = row.get("verdict") or "-"
        out[row["kind"]][key] = out[row["kind"]].get(key, 0) + 1
    return out
