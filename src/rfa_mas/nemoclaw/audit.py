"""Audit ledger shared by the proxy, broker, channel entry and controller (SQLite, host-only).

One row per decision: channel, profile, sandbox/agent, verdict, action, detail. Text bodies are
never stored — only counts, rule ids, opaque ids and durations.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

_DEFAULT = Path(__file__).resolve().parents[3] / ".local" / "sg" / "audit.db"
_lock = threading.Lock()
KINDS = ("inference", "broker", "channel", "policy", "request", "approval", "relocation", "censor")


def db_path() -> Path:
    return Path(os.environ.get("RFA_SG_AUDIT_DB") or _DEFAULT)


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS events ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL,"
        " channel TEXT, profile TEXT, sandbox TEXT, agent TEXT, session_id TEXT,"
        " verdict TEXT, action TEXT, detail TEXT)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS events_ts ON events(ts)")
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
) -> int:
    if kind not in KINDS:
        raise ValueError(f"unknown audit kind {kind}")
    payload = json.dumps(detail or {}, ensure_ascii=False, default=str)
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "INSERT INTO events (ts, kind, channel, profile, sandbox, agent, session_id, verdict,"
                " action, detail) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (time.time(), kind, channel, profile, sandbox, agent, session_id, verdict, action, payload),
            )
            conn.commit()
            return int(cur.lastrowid or 0)
        finally:
            conn.close()


def query(
    *, limit: int = 100, kind: str | None = None, session_id: str | None = None, since: float | None = None
) -> list[dict]:
    clauses, params = [], []
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    if session_id:
        clauses.append("session_id = ?")
        params.append(session_id)
    if since:
        clauses.append("ts >= ?")
        params.append(since)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                f"SELECT id, ts, kind, channel, profile, sandbox, agent, session_id, verdict, action, detail"
                f" FROM events{where} ORDER BY id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        finally:
            conn.close()
    out = []
    for row in rows:
        out.append(
            {
                "id": row[0], "ts": row[1], "kind": row[2], "channel": row[3], "profile": row[4],
                "sandbox": row[5], "agent": row[6], "session_id": row[7], "verdict": row[8],
                "action": row[9], "detail": json.loads(row[10] or "{}"),
            }
        )
    return out


def counts(since: float | None = None) -> dict:
    where = " WHERE ts >= ?" if since else ""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                f"SELECT kind, verdict, COUNT(*) FROM events{where} GROUP BY kind, verdict",
                ((since,) if since else ()),
            ).fetchall()
        finally:
            conn.close()
    out: dict = {}
    for kind, verdict, n in rows:
        out.setdefault(kind, {})[verdict or "-"] = n
    return out
