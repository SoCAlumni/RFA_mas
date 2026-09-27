"""Chat conversations (D-7): the owner's are stored in SQLite; a guest has no identity, so a guest
conversation is only an id — the client keeps the turns and sends them back as ``history``.

A stored message is either ``{"text"}`` (user) or the finished assistant *turn* — the same shape the
front-end's reducer builds from the SSE events (``TurnBuilder`` mirrors it), so a reopened conversation
renders exactly like a live one."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable

from rfa_mas.nemoclaw.store import Store

TITLE_CHARS = 60
PREVIEW_CHARS = 120


def new_id() -> str:
    return f"c_{uuid.uuid4().hex[:12]}"


class TurnBuilder:
    """Server-side copy of the UI reducer: SSE events → one assistant turn."""

    def __init__(self, message_id: str):
        self.turn: dict = {"id": message_id, "role": "assistant", "status": "streaming", "phase": "understand",
                           "preface": "", "search": None, "selection": None, "calls": [], "guard": None, "answer": "",
                           "ts": time.time(), "runId": None, "durationMs": None, "error": None}

    def _call(self, call_id: str) -> dict | None:
        return next((c for c in self.turn["calls"] if c["callId"] == call_id), None)

    def apply(self, kind: str, data: dict) -> None:
        t = self.turn
        if kind == "run.start":
            t["runId"] = data.get("runId")
        elif kind == "assistant.delta":
            t["preface"] += data.get("text", "")
        elif kind == "agents.search":
            t["phase"], t["search"] = "search", {"query": data.get("query"), "candidates": data.get("candidates", [])}
        elif kind == "agents.select":
            t["selection"] = {"selected": data.get("selected", []), "skipped": data.get("skipped", [])}
        elif kind == "delegate.start":
            t["phase"] = "delegate"
            t["calls"].append({"callId": data["callId"], "agentId": data.get("agentId"), "task": data.get("task"),
                               "status": "running", "logs": [], "refs": [], "summary": None, "durationMs": None})
        elif kind == "delegate.log":
            if call := self._call(data.get("callId")):
                call["logs"].append(data.get("text", ""))
        elif kind == "delegate.end":
            if call := self._call(data.get("callId")):
                call.update(status=data.get("status"), summary=data.get("summary"), refs=data.get("refs") or [],
                            durationMs=data.get("durationMs"))
        elif kind == "guard.start":
            t["phase"], t["guard"] = "guard", {"level": data.get("level"), "status": "running"}
        elif kind == "guard.end":
            t["guard"] = {**(t["guard"] or {}), "status": data.get("status"), "note": data.get("note"),
                          "redactions": data.get("redactions")}
        elif kind == "answer.delta":
            t["phase"], t["answer"] = "answer", t["answer"] + data.get("text", "")
        elif kind == "run.end":
            self.end(data.get("status", "done"), data.get("durationMs"), data.get("error"))

    def end(self, status: str, duration_ms: int | None, error: str | None = None) -> dict:
        t = self.turn
        if t["status"] != "streaming":
            return t
        t.update(status=status, phase="idle", durationMs=duration_ms, error=error)
        if status != "done":
            for call in t["calls"]:
                if call["status"] == "running":
                    call["status"] = "error"
            if t["guard"] and t["guard"].get("status") == "running":
                t["guard"] = None
        return t


class ConversationService:
    def __init__(self, store: Store, busy: Callable[[str], bool] = lambda _cid: False):
        self.store, self.is_busy = store, busy

    # ---- reads ------------------------------------------------------------------------------

    def _summary(self, row: dict) -> dict:
        msgs = self.store.query("SELECT kind, body FROM messages WHERE conversation_id=? ORDER BY id", (row["id"],))
        last = self.store.loads(msgs[-1]["body"], {}) if msgs else {}
        preview = (last.get("answer") or last.get("text") or "")[:PREVIEW_CHARS]
        return {"id": row["id"], "agentId": row["agent_id"], "title": row["title"] or "새 대화", "preview": preview,
                "createdAt": row["created"], "updatedAt": row["updated"], "busy": self.is_busy(row["id"]),
                "count": sum(1 for m in msgs if m["kind"] == "user"), "persisted": True}

    def list(self, agent_id: str | None) -> list[dict]:
        sql, params = "SELECT * FROM conversations WHERE role='owner'", ()
        if agent_id:
            sql, params = sql + " AND agent_id=?", (agent_id,)
        return [self._summary(r) for r in self.store.query(sql + " ORDER BY updated DESC", params)]

    def get(self, conversation_id: str) -> dict | None:
        row = self.store.one("SELECT * FROM conversations WHERE id=? AND role='owner'", (conversation_id,))
        if row is None:
            return None
        messages = []
        for m in self.store.query("SELECT * FROM messages WHERE conversation_id=? ORDER BY id", (conversation_id,)):
            body = self.store.loads(m["body"], {})
            if m["kind"] == "user":
                messages.append({"id": f"u{m['id']}", "role": "user", "text": body.get("text", ""), "ts": m["created"]})
            else:
                messages.append({**body, "role": "assistant", "ts": m["created"]})
        return {**self._summary(row), "messages": messages}

    def history(self, conversation_id: str, limit: int = 20) -> list[dict]:
        """Earlier turns as ``{role, text}`` (user text, assistant answer) for routing context."""
        rows = self.store.query("SELECT kind, body FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?",
                                (conversation_id, limit))
        out = []
        for m in reversed(rows):
            body = self.store.loads(m["body"], {})
            text = body.get("text") if m["kind"] == "user" else body.get("answer")
            if text:
                out.append({"role": "user" if m["kind"] == "user" else "assistant", "text": text})
        return out

    def owner_of(self, conversation_id: str) -> dict | None:
        return self.store.one("SELECT * FROM conversations WHERE id=?", (conversation_id,))

    # ---- writes -----------------------------------------------------------------------------

    def create(self, agent_id: str, role: str) -> dict:
        cid, now = new_id(), self.store.now()
        if role != "owner":  # guest: an id only; the client keeps the turns (D-7)
            return {"id": cid, "agentId": agent_id, "title": "새 대화", "preview": "", "createdAt": now, "updatedAt": now,
                    "busy": False, "count": 0, "persisted": False}
        self.store.execute("INSERT INTO conversations (id, agent_id, role, title, created, updated) VALUES (?,?,?,?,?,?)",
                           (cid, agent_id, "owner", "", now, now))
        return self._summary(self.owner_of(cid))

    def ensure(self, conversation_id: str, agent_id: str) -> None:
        """An owner may start with a client-made id; store it on first use."""
        now = self.store.now()
        self.store.execute("INSERT OR IGNORE INTO conversations (id, agent_id, role, title, created, updated) "
                           "VALUES (?,?,?,?,?,?)", (conversation_id, agent_id, "owner", "", now, now))

    def add_user(self, conversation_id: str, text: str) -> None:
        now = self.store.now()
        self.store.execute("INSERT INTO messages (conversation_id, kind, role, body, created) VALUES (?,?,?,?,?)",
                           (conversation_id, "user", "owner", self.store.dumps({"text": text}), now))
        self.store.execute("UPDATE conversations SET updated=?, title=CASE WHEN title='' THEN ? ELSE title END WHERE id=?",
                           (now, " ".join(text.split())[:TITLE_CHARS], conversation_id))

    def add_turn(self, conversation_id: str, turn: dict) -> None:
        now = self.store.now()
        self.store.execute("INSERT INTO messages (conversation_id, kind, role, body, created) VALUES (?,?,?,?,?)",
                           (conversation_id, "assistant", "assistant", self.store.dumps(turn), now))
        self.store.execute("UPDATE conversations SET updated=? WHERE id=?", (now, conversation_id))
