"""Local chat ledger + fixed core HTTP consumer. No model/key/approval authority.

Only user text and result references persist here. Answers are re-read through the
core's current-ACL presentation API, never replayed from a stale answer cache.
"""

import asyncio
import json
import sqlite3
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path

import httpx

from rfa_mas.application.chat import ChatMessage, chat_intent
from rfa_mas.application.graphs.supervisor import BENCHMARK_TERMS, PATTERN_OUTPUTS
from rfa_mas.contracts import (
    Audience,
    DirectWorkRequest,
    DraftTarget,
    KnowledgeProvenance,
    KnowledgeWrite,
    TeamExecutionRequest,
    sha256_text,
)
from rfa_mas.errors import RfaError


class LocalChat:
    def __init__(self, path: Path, core: httpx.AsyncClient, router):
        self.path, self.core, self.router = path, core, router
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS chat_turns (
                session_id TEXT NOT NULL, message_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                text TEXT NOT NULL, domain_id TEXT NOT NULL, intent TEXT NOT NULL,
                status TEXT NOT NULL, source_id TEXT, run_id TEXT, created_at TEXT NOT NULL,
                PRIMARY KEY(session_id, message_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS chat_execution_sessions (
                execution_session_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL)""")
            columns = {r[1] for r in db.execute("PRAGMA table_info(chat_turns)")}
            for column, default in (("route_json", "{}"), ("refs_json", "[]")):
                if column not in columns:
                    db.execute(
                        f"ALTER TABLE chat_turns ADD COLUMN {column} "
                        f"TEXT NOT NULL DEFAULT '{default}'"
                    )
            db.execute("""CREATE TABLE IF NOT EXISTS chat_stages (
                session_id TEXT, message_id TEXT, sequence INTEGER, event_json TEXT,
                PRIMARY KEY(session_id,message_id,sequence))""")
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

    async def stream(self, session_id, body: ChatMessage):
        # Authenticate before returning a StreamingResponse / HTTP 200.
        await self.call("GET", f"/v1/sessions/{session_id}")

        async def events():
            queue = asyncio.Queue(maxsize=16)

            async def produce():
                try:
                    result = await self.send(session_id, body, emit=queue.put)
                    await queue.put({"type": "result", "result": result})
                except Exception:
                    await queue.put(
                        {
                            "type": "error",
                            "code": "chat_interrupted",
                            "message": "처리 결과를 확인해 주세요. 자동 재실행하지 않습니다.",
                        }
                    )
                finally:
                    await queue.put(None)

            task = asyncio.create_task(produce())
            try:
                while (event := await queue.get()) is not None:
                    yield event
            finally:
                if not task.done():
                    task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

        return events()

    async def send(self, session_id, body: ChatMessage, *, emit=None):
        await self.call("GET", f"/v1/sessions/{session_id}")
        if body.task_id:
            # Reject an unknown/foreign/unavailable explicit assignee before any ledger write.
            await self.router.resolve(body)
        fingerprint = sha256_text(body.model_dump_json())
        intent = chat_intent(body.text)
        # Storage bucket is not a worker assignment. Legacy DB's domain is NOT NULL.
        domain = body.domain_id.value if body.domain_id else "triv3"
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
                    "INSERT INTO chat_turns (session_id,message_id,fingerprint,text,domain_id,"
                    "intent,status,source_id,run_id,created_at) "
                    "VALUES (?,?,?,?,?,?,'pending',NULL,NULL,?)",
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

        async def stage(code, label):
            with self.connect() as db:
                sequence = (
                    db.execute(
                        "SELECT count(*) FROM chat_stages WHERE session_id=? AND message_id=?",
                        (session_id, body.message_id),
                    ).fetchone()[0]
                    + 1
                )
                event = {
                    "type": "stage",
                    "sequence": sequence,
                    "stage": code,
                    "label": label,
                    "at": datetime.now(UTC).isoformat(),
                }
                db.execute(
                    "INSERT INTO chat_stages VALUES (?,?,?,?)",
                    (session_id, body.message_id, sequence, json.dumps(event)),
                )
            if emit:
                await emit(event)
            return event

        source_id = run_id = None
        try:
            first = {"store_note": "입력 이해 중", "task_run": "요청 이해 중"}
            await stage("understanding", first.get(intent, "질문 이해 중"))
            route = await self.router.resolve(body)
            domain = route["domain_id"] or domain
            if intent == "task_run" and route["kind"] != "task":
                # Explicit research/benchmark request with no suitable existing Task team:
                # the core Supervisor creates exactly one durable Task + team for it.
                lowered = body.text.lower()
                route = {
                    "kind": "new_task",
                    "label": "새 Task 팀 구성",
                    "reason": "explicit_task_request_no_match",
                    "domain_id": domain,
                    "task_id": None,
                    "team_id": None,
                    "pattern": "benchmark"
                    if any(t in lowered for t in BENCHMARK_TERMS)
                    else "research",
                }
            with self.connect() as db:
                db.execute(
                    "UPDATE chat_turns SET route_json=?,domain_id=? "
                    "WHERE session_id=? AND message_id=?",
                    (json.dumps(route), domain, session_id, body.message_id),
                )
            label = route["label"]
            if route["kind"] == "assistant":
                label = "적합한 담당자 없음 또는 후보 모호 · " + label
            elif route["kind"] == "new_task":
                label = "적합한 기존 Task 팀 없음 · " + label
            await stage("routing", "담당자 확인: " + label)
            await stage(
                "preparing",
                {"store_note": "자료 저장 준비", "task_run": "Task 팀 실행 준비"}.get(
                    intent, "답변 준비"
                ),
            )
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
            elif intent in {"query", "external_draft"} and route["kind"] == "assistant":
                refs = await self.router.find_refs(body.text, public=intent == "external_draft")
                with self.connect() as db:
                    db.execute(
                        "UPDATE chat_turns SET refs_json=? WHERE session_id=? AND message_id=?",
                        (json.dumps(refs), session_id, body.message_id),
                    )
                status = "answered"
            elif intent in {"query", "external_draft", "task_run"}:
                # Conversation != execution thread. An unapproved draft must not
                # prevent a later question, nor be auto-approved to unlock a thread.
                execution = await self.call("POST", "/v1/sessions", {})
                execution_id = execution["session_id"]
                with self.connect() as db:
                    db.execute(
                        "INSERT INTO chat_execution_sessions VALUES (?,?)",
                        (execution_id, session_id),
                    )
                team = None
                if route["task_id"]:
                    # Fresh authoritative ownership/template check; never trust message fields.
                    principal = await self.router.repository.local_principal()
                    existing = await self.router.repository.get_team_lifecycle(
                        route["task_id"], principal
                    )
                    pattern = existing.team.spec.template.pattern
                    team = TeamExecutionRequest(
                        goal=existing.task.goal,
                        outputs=PATTERN_OUTPUTS[pattern],
                        requested_pattern=pattern,
                    )
                elif route["kind"] == "new_task":
                    team = TeamExecutionRequest(
                        goal=body.text[:2000],
                        outputs=PATTERN_OUTPUTS[route["pattern"]],
                        requested_pattern=route["pattern"],
                    )
                work = DirectWorkRequest(
                    query=body.text,
                    session_id=execution_id,
                    domain_id=domain,
                    task_id=route["task_id"],
                    team=team,
                    request_id=f"chat-{sha256_text(session_id + body.message_id)[:32]}",
                    target=DraftTarget(
                        audience=Audience.PUBLIC if intent == "external_draft" else Audience.PRIVATE
                    ),
                )
                run = await self.call(
                    "POST", f"/v1/sessions/{execution_id}/work", work.model_dump(mode="json")
                )
                run_id, status = run["run_id"], "answered"
                if route["kind"] == "new_task":
                    # Record the durable Task/team the core actually created for this Run.
                    with suppress(RfaError):
                        created = await self.call("GET", f"/v1/runs/{run_id}/team")
                        route = route | {
                            "task_id": created["task_id"],
                            "team_id": created["team_id"],
                            "pattern": created["pattern"],
                            "label": body.text[:80],
                        }
                        with self.connect() as db:
                            db.execute(
                                "UPDATE chat_turns SET route_json=? "
                                "WHERE session_id=? AND message_id=?",
                                (json.dumps(route), session_id, body.message_id),
                            )
            else:
                status = "clarify"
        except (Exception, asyncio.CancelledError):
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
        result = await self.present(row)
        result["stages"].append(
            await stage(
                "completed",
                "저장 완료"
                if status == "stored"
                else ("Task 팀 결과" if row["intent"] == "task_run" else "답변"),
            )
        )
        return result

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
        result["route"] = json.loads(row.get("route_json", "{}"))
        with self.connect() as db:
            result["stages"] = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT event_json FROM chat_stages WHERE session_id=? "
                    "AND message_id=? ORDER BY sequence",
                    (row["session_id"], row["message_id"]),
                )
            ]
        if row["status"] == "stored":
            try:
                await self.call("GET", f"/v1/knowledge/sources/{row['source_id']}")
                result["reply"] = "내 KB에 비공개로 저장했어요. 나중에 이 내용에 관해 물어보세요."
            except RfaError:
                result["reply"] = "이전에 저장한 자료가 변경되었거나 더 이상 접근할 수 없어요."
        elif row["run_id"]:
            run = (await self.call("GET", f"/v1/runs/{row['run_id']}"))["result"]
            result["run"] = run
            if result["route"].get("kind") in {"task", "new_task"}:
                with suppress(RfaError):
                    team = await self.call("GET", f"/v1/runs/{row['run_id']}/team")
                    result["team"] = {
                        k: team[k]
                        for k in ("task_id", "team_id", "pattern", "status", "simulated", "summary")
                    }
            if run and run.get("draft"):
                result["reply"] = run["draft"]["content"]
            elif result.get("team") and result["team"]["summary"]:
                result["reply"] = result["team"]["summary"]
            else:
                result["reply"] = (
                    "현재 허용된 자료로 답변을 만들 수 없어요. 자료나 질문을 확인해 주세요."
                )
        elif row["status"] == "answered" and result["route"].get("kind") == "assistant":
            evidence = await self.router.excerpts(
                json.loads(row["refs_json"]), public=row["intent"] == "external_draft"
            )
            result["evidence"] = evidence
            result["reply"] = (
                (
                    "내 자료에서 찾았어요. (비서 직접 검색)\n\n"
                    + "\n\n".join(e["excerpt"] for e in evidence)
                )
                if evidence
                else (
                    "적합한 담당 에이전트가 없어 비서가 직접 확인했어요. "
                    "현재 허용된 KB에서 관련 근거를 찾지 못했어요. "
                    "자료나 질문을 조금 더 알려주세요."
                )
            )
            if row["intent"] == "external_draft":
                result["reply"] += (
                    "\n\n공개 근거 미리보기입니다. 승인/게시용 초안은 생성하지 않았어요."
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
