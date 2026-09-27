from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_DEFAULT = Path(__file__).resolve().parents[4] / ".local" / "sg" / "frontend.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, agent_id TEXT, role TEXT, title TEXT,
    created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
    kind TEXT NOT NULL, role TEXT NOT NULL, body TEXT NOT NULL, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS messages_conv ON messages(conversation_id, id);
CREATE TABLE IF NOT EXISTS threads (id INTEGER PRIMARY KEY AUTOINCREMENT, approval_id TEXT NOT NULL,
    author TEXT NOT NULL, body TEXT NOT NULL, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS threads_approval ON threads(approval_id, id);
CREATE TABLE IF NOT EXISTS instructions (id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, scope TEXT NOT NULL,
    approval_ids TEXT NOT NULL DEFAULT '[]', text TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'manual',
    created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS instructions_agent ON instructions(agent_id);
CREATE TABLE IF NOT EXISTS options (approval_id TEXT NOT NULL, option_id TEXT NOT NULL, label TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '', text TEXT NOT NULL, risk_tag TEXT NOT NULL DEFAULT '',
    recommended INTEGER NOT NULL DEFAULT 0, question TEXT NOT NULL DEFAULT '',
    always_excluded TEXT NOT NULL DEFAULT '[]', chosen INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,
    PRIMARY KEY (approval_id, option_id));
CREATE TABLE IF NOT EXISTS rules (id TEXT PRIMARY KEY, pattern TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'keyword',
    knowledge TEXT NOT NULL, audience TEXT NOT NULL DEFAULT '*', enabled INTEGER NOT NULL DEFAULT 1,
    created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS blocklist (id TEXT PRIMARY KEY, requester TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, name TEXT NOT NULL, agent_id TEXT NOT NULL,
    sandbox TEXT, grade_label TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL);
"""


def store_path() -> Path:
    return Path(os.environ.get("RFA_FE_DB") or _DEFAULT)


class Store:
    """Thin SQLite wrapper: one connection per call, one process-wide lock, rows as dicts."""

    def __init__(self, path: Path | None = None):
        self.path = path or store_path()
        self._lock = threading.Lock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(SCHEMA)
                conn.commit()
            finally:
                conn.close()

    def execute(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(sql, params)
                conn.commit()
                return int(cur.lastrowid or cur.rowcount)
            finally:
                conn.close()

    def query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                return [dict(r) for r in conn.execute(sql, params).fetchall()]
            finally:
                conn.close()

    def one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    @staticmethod
    def now() -> float:
        return time.time()

    @staticmethod
    def dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def loads(value: str | None, default: Any = None) -> Any:
        if not value:
            return default
        try:
            return json.loads(value)
        except ValueError:
            return default
