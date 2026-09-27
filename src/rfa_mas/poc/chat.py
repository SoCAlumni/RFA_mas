"""Local chat ledger + fixed core HTTP consumer. No model/key/approval authority.

Only user text and result references persist here. Answers are re-read through the
core's current-ACL presentation API, never replayed from a stale answer cache.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx

from rfa_mas.application.chat import ChatMessage, chat_intent
from rfa_mas.contracts import (
    Audience,
    DirectWorkRequest,
    DraftTarget,
    KnowledgeProvenance,
    KnowledgeWrite,
    sha256_text,
)
from rfa_mas.errors import RfaError


class LocalChat:
    def __init__(self, path: Path, core: httpx.AsyncClient):
        self.path, self.core = path, core
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS chat_turns (
                session_id TEXT NOT NULL, message_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                text TEXT NOT NULL, domain_id TEXT NOT NULL, intent TEXT NOT NULL,
                status TEXT NOT NULL, source_id TEXT, run_id TEXT, created_at TEXT NOT NULL,
                PRIMARY KEY(session_id, message_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS chat_execution_sessions (
                execution_session_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL)""")
            # An interrupted write is never automatically repeated on restart.
            db.execute("UPDATE chat_turns SET status='outcome_unknown' WHERE status='pending'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    async def call(self, method, path, body=None):
        try:
            response = await self.core.request(method, path, json=body)
        except httpx.HTTPError:
            raise RfaError(
                "outcome_unknown", "연결이 끊겼습니다. 자동 재실행하지 않습니다."
            ) from None
        if response.status_code >= 400:
            code = "not_found" if response.status_code == 404 else "chat_core_rejected"
            raise RfaError(code, "요청을 처리할 수 없습니다. 대화와 자료 접근 권한을 확인하세요.")
        return response.json()

    async def sessions(self):
        sessions = await self.call("GET", "/v1/sessions")
        with self.connect() as db:
            children = {
                r[0] for r in db.execute("SELECT execution_session_id FROM chat_execution_sessions")
            }
            turns = db.execute(
                "SELECT session_id,text,created_at FROM chat_turns ORDER BY rowid"
            ).fetchall()
        titles, updated = {}, {}
        for row in turns:
            titles.setdefault(row["session_id"], row["text"][:60])
            updated[row["session_id"]] = row["created_at"]
        return [
            s
            | {
                "title": titles.get(s["session_id"], "새 대화"),
                "updated_at": updated.get(s["session_id"], s["updated_at"]),
            }
            for s in sessions
            if s["session_id"] not in children
        ]

    async def history(self, session_id):
        # Every read/write authorizes the real session at the core, before ledger access.
        detail = await self.call("GET", f"/v1/sessions/{session_id}")
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM chat_turns WHERE session_id=? ORDER BY rowid", (session_id,)
            ).fetchall()
        # Preserve earlier form-UI conversations without migrating or copying results.
        # The core session API has already applied current ACL presentation to each run.
        legacy = []
        for record in detail.get("runs", []):
            run = record.get("result")
            draft = (run or {}).get("draft")
            query = next(
                (
                    m["content"]
                    for m in detail.get("messages", [])
                    if m["run_id"] == record["run_id"] and m["role"] == "user"
                ),
                "이전 실행",
            )
            legacy.append(
                {
                    "message_id": "legacy-" + record["run_id"],
                    "text": query,
                    "intent": "external_draft"
                    if draft and draft["audience"] == "public"
                    else "query",
                    "status": "answered",
                    "created_at": record["created_at"],
                    "domain_id": record["domain_id"],
                    "source_id": None,
                    "run_id": record["run_id"],
                    "run": run,
                    "reply": draft["content"] if draft else "현재 표시할 수 있는 응답이 없습니다.",
                }
            )
        return legacy + [await self.present(dict(row)) for row in rows]

    async def send(self, session_id, body: ChatMessage):
        await self.call("GET", f"/v1/sessions/{session_id}")
        fingerprint = sha256_text(body.model_dump_json())
        intent = chat_intent(body.text)
        domain = (
            "quantization_research"
            if any(t in body.text.lower() for t in ("양자화", "quantization"))
            else body.domain_id.value
        )
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM chat_turns WHERE session_id=? AND message_id=?",
                (session_id, body.message_id),
            ).fetchone()
            if old is not None:
                if old["fingerprint"] != fingerprint:
                    raise RfaError("idempotency_conflict", "같은 메시지 ID의 내용이 달라졌습니다.")
            else:
                if db.execute(
                    "SELECT 1 FROM chat_turns WHERE session_id=? AND status='pending'",
                    (session_id,),
                ).fetchone():
                    raise RfaError("chat_busy", "이 대화의 이전 메시지를 처리하고 있습니다.")
                db.execute(
                    "INSERT INTO chat_turns VALUES (?,?,?,?,?,?,'pending',NULL,NULL,?)",
                    (
                        session_id,
                        body.message_id,
                        fingerprint,
                        body.text,
                        domain,
                        intent,
                        datetime.now(UTC).isoformat(),
                    ),
                )
        if old is not None:
            return await self.present(dict(old))
        source_id = run_id = None
        try:
            if intent == "store_note":
                key = sha256_text(json.dumps([session_id, body.message_id]))
                write = KnowledgeWrite(
                    domain_id=domain,
                    provenance=KnowledgeProvenance(
                        provider="note", namespace="poc-chat", external_id=f"chat-{key[:32]}"
                    ),
                    provider_revision="1",
                    title=body.text.splitlines()[0][:80],
                    content=body.text,
                )
                stored = await self.call(
                    "POST", "/v1/knowledge/sources", write.model_dump(mode="json")
                )
                source_id, status = stored["document"]["source_id"], "stored"
            elif intent in {"query", "external_draft"}:
                # Conversation != execution thread. An unapproved draft must not
                # prevent a later question, nor be auto-approved to unlock a thread.
                execution = await self.call("POST", "/v1/sessions", {})
                execution_id = execution["session_id"]
                with self.connect() as db:
                    db.execute(
                        "INSERT INTO chat_execution_sessions VALUES (?,?)",
                        (execution_id, session_id),
                    )
                work = DirectWorkRequest(
                    query=body.text,
                    session_id=execution_id,
                    domain_id=domain,
                    request_id=f"chat-{sha256_text(session_id + body.message_id)[:32]}",
                    target=DraftTarget(
                        audience=Audience.PUBLIC if intent == "external_draft" else Audience.PRIVATE
                    ),
                )
                run = await self.call(
                    "POST", f"/v1/sessions/{execution_id}/work", work.model_dump(mode="json")
                )
                run_id, status = run["run_id"], "answered"
            else:
                status = "clarify"
        except Exception:
            with self.connect() as db:
                db.execute(
                    "UPDATE chat_turns SET status='outcome_unknown' "
                    "WHERE session_id=? AND message_id=?",
                    (session_id, body.message_id),
                )
            raise
        with self.connect() as db:
            db.execute(
                "UPDATE chat_turns SET status=?,source_id=?,run_id=? "
                "WHERE session_id=? AND message_id=?",
                (status, source_id, run_id, session_id, body.message_id),
            )
            row = dict(
                db.execute(
                    "SELECT * FROM chat_turns WHERE session_id=? AND message_id=?",
                    (session_id, body.message_id),
                ).fetchone()
            )
        return await self.present(row)

    async def present(self, row):
        result = {
            k: row[k]
            for k in (
                "message_id",
                "text",
                "intent",
                "status",
                "created_at",
                "domain_id",
                "source_id",
                "run_id",
            )
        }
        result.update(reply="", run=None)
        if row["status"] == "stored":
            try:
                await self.call("GET", f"/v1/knowledge/sources/{row['source_id']}")
                result["reply"] = "내 KB에 비공개로 저장했어요. 나중에 이 내용에 관해 물어보세요."
            except RfaError:
                result["reply"] = "이전에 저장한 자료가 변경되었거나 더 이상 접근할 수 없어요."
        elif row["run_id"]:
            run = (await self.call("GET", f"/v1/runs/{row['run_id']}"))["result"]
            result["run"] = run
            if run and run.get("draft"):
                result["reply"] = run["draft"]["content"]
            else:
                result["reply"] = (
                    "현재 허용된 자료로 답변을 만들 수 없어요. 자료나 질문을 확인해 주세요."
                )
        elif row["status"] in {"pending", "outcome_unknown"}:
            result["reply"] = (
                "처리 결과를 아직 확정할 수 없어요. KB와 실행 기록을 확인해 주세요. "
                "자동으로 재실행하지 않습니다."
            )
        else:
            result["reply"] = (
                "무엇을 도와드릴까요? 정보를 남기려면 ‘메모: …’, "
                "찾으려면 ‘… 알려줘’처럼 말씀해 주세요. "
                "삭제·예약·외부 전송은 이 채팅에서 실행하지 않아요."
            )
        return result
