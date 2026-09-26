from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from rfa_mas.application.state_machine import ensure_transition
from rfa_mas.contracts import (
    AgentSpec,
    Audience,
    DomainId,
    ExecutionContext,
    ExecutionMode,
    KnowledgeDocument,
    MemberLifecycle,
    ObservationRecord,
    PersistentTask,
    PolicyDecision,
    PolicyRequest,
    ResultStatus,
    RunRecord,
    RunResult,
    SessionDetail,
    SessionMessage,
    SessionRecord,
    StructuredError,
    TaskRequest,
    TaskResult,
    TeamInstance,
    TeamLifecycle,
    TeamSpec,
    ToolEffect,
    TraceEvent,
    TrustedPrincipal,
    WorkRequest,
    WorkStatus,
    new_id,
)
from rfa_mas.errors import ResourceNotFoundError, RfaError
from rfa_mas.ports import TaskHandler
from rfa_mas.security import SecretRedactor


def _canonical_fingerprint(value: dict[str, Any]) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _team_definition(spec: TeamSpec) -> dict:
    # Stable across server-generated Task/team/member identifiers.
    return {
        "domain_id": spec.domain_id,
        "owner_id": spec.owner_id,
        "template": spec.template.model_dump(mode="json"),
        "definition_digest": spec.definition_digest,
        "execution_budget": spec.execution_budget.model_dump(mode="json")
        if spec.execution_budget
        else None,
        "members": [
            m.model_dump(mode="json")
            | {
                "spec": m.spec.model_dump(
                    mode="json",
                    exclude={
                        "agent_id",
                        "memory_namespace",
                    },
                )
            }
            for m in spec.members
        ],
    }


@contextmanager
def _sqlite_setup_guard(
    path: Path, *, blocking: bool = True, timeout_seconds: float = 5.0
) -> Iterator[None]:
    """Same-host setup coordination, not a database/invocation authorization lock.

    Blocking use belongs in a synchronous worker thread. Async callers use a
    nonblocking acquisition and their own bounded, cancellable wait.
    """
    try:
        import fcntl
    except ImportError as exc:
        raise RfaError(
            "configuration_error", "SQLite setup에 동일-host POSIX lock이 필요합니다."
        ) from exc
    lock_path = Path(str(path.resolve()) + ".setup.lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RfaError("configuration_error", "SQLite setup lock은 일반 파일이어야 합니다.")
        os.fchmod(descriptor, 0o600)
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if not blocking:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RfaError("storage_busy", "SQLite 초기화가 진행 중입니다.") from None
                time.sleep(min(0.01, remaining))
            else:
                break
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


class SqliteWorkRepository:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            with connection:
                yield connection
        finally:
            connection.close()

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        def operation() -> None:
            # WAL persists in the DB. Switching it on every connection can
            # return SQLITE_BUSY immediately, despite SQLite's busy timeout.
            # Only setup is serialized; no caller transaction is ever replayed.
            with _sqlite_setup_guard(self.path), self._connect() as connection:
                mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                if mode != "wal":
                    raise RfaError("configuration_error", "SQLite WAL 초기화에 실패했습니다.")
                connection.execute("BEGIN IMMEDIATE")
                had_prior_schema = bool(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'runs'"
                    ).fetchone()
                )
                schema = """
                    CREATE TABLE IF NOT EXISTS runs (
                        run_id TEXT PRIMARY KEY,
                        request_id TEXT NOT NULL,
                        trace_id TEXT NOT NULL,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        status TEXT NOT NULL,
                        request_json TEXT NOT NULL,
                        result_json TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS drafts (
                        draft_id TEXT NOT NULL,
                        version INTEGER NOT NULL,
                        run_id TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        draft_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (draft_id, version),
                        FOREIGN KEY (run_id) REFERENCES runs(run_id)
                    );
                    CREATE TABLE IF NOT EXISTS graph_checkpoints (
                        checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL,
                        node TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        FOREIGN KEY (run_id) REFERENCES runs(run_id)
                    );
                    CREATE TABLE IF NOT EXISTS kb_documents (
                        source_id TEXT NOT NULL,
                        source_revision TEXT NOT NULL,
                        domain_id TEXT NOT NULL,
                        document_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (source_id, source_revision)
                    );
                    """
                # These are fixed DDL statements, not user SQL. executescript
                # commits a transaction first, which would open a startup race.
                for statement in schema.split(";"):
                    if statement.strip():
                        connection.execute(statement)
                # Each additive migration and its version marker commit together.
                # NULL ownership on old rows is intentional: never adopt legacy data.
                had_seed_registry = bool(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                        "AND name = 'installation_seeds'"
                    ).fetchone()
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS installation_seeds "
                    "(seed_id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
                )
                if had_prior_schema and not had_seed_registry:
                    # Pre-marker installations may intentionally have an empty KB.
                    # Migration must never resurrect even an entirely deleted fixture set.
                    connection.execute(
                        "INSERT OR IGNORE INTO installation_seeds VALUES "
                        "('synthetic-fixtures-v1', ?)",
                        (datetime.now(UTC).isoformat(),),
                    )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS rfa_schema_migrations "
                    "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
                )
                if (
                    connection.execute(
                        "SELECT 1 FROM rfa_schema_migrations WHERE version = 1"
                    ).fetchone()
                    is None
                ):
                    for statement in (
                        "CREATE TABLE local_identity (singleton INTEGER PRIMARY KEY "
                        "CHECK (singleton = 1), principal_json TEXT NOT NULL)",
                        "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, "
                        "thread_id TEXT NOT NULL UNIQUE, owner_id TEXT NOT NULL, "
                        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
                        "CREATE INDEX sessions_owner ON sessions(owner_id, updated_at)",
                        "CREATE TABLE product_task_owners (task_id TEXT PRIMARY KEY, "
                        "owner_id TEXT NOT NULL, domain_id TEXT NOT NULL)",
                        "CREATE TABLE session_tasks (session_id TEXT NOT NULL "
                        "REFERENCES sessions(session_id), task_id TEXT NOT NULL "
                        "REFERENCES product_task_owners(task_id), "
                        "PRIMARY KEY (session_id, task_id))",
                        "ALTER TABLE runs ADD COLUMN owner_id TEXT",
                        "ALTER TABLE runs ADD COLUMN session_id TEXT "
                        "REFERENCES sessions(session_id)",
                        "ALTER TABLE runs ADD COLUMN task_id TEXT "
                        "REFERENCES product_task_owners(task_id)",
                        "CREATE INDEX runs_owner_session ON runs(owner_id, session_id)",
                        "CREATE TABLE session_messages (message_id TEXT PRIMARY KEY, "
                        "session_id TEXT NOT NULL REFERENCES sessions(session_id), "
                        "run_id TEXT NOT NULL REFERENCES runs(run_id), "
                        "role TEXT NOT NULL CHECK (role IN ('user', 'assistant')), "
                        "content TEXT NOT NULL, created_at TEXT NOT NULL, "
                        "UNIQUE (run_id, role))",
                    ):
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO rfa_schema_migrations VALUES (1, ?)",
                        (datetime.now(UTC).isoformat(),),
                    )
                # Credentials authenticate this installation's owner, not a fixture
                if not connection.execute(
                    "SELECT 1 FROM rfa_schema_migrations WHERE version = 2"
                ).fetchone():
                    connection.execute(
                        "CREATE TABLE observation_aliases "
                        "(run_id TEXT NOT NULL REFERENCES runs(run_id), "
                        "kind TEXT NOT NULL, ref TEXT NOT NULL, alias TEXT NOT NULL UNIQUE, "
                        "PRIMARY KEY (run_id, kind, ref))"
                    )
                    connection.execute(
                        "CREATE TABLE observation_sequences (run_id TEXT PRIMARY KEY REFERENCES "
                        "runs(run_id), sequence INTEGER NOT NULL)"
                    )
                    connection.execute(
                        "CREATE TABLE observations (observation_id TEXT PRIMARY KEY, "
                        "run_id TEXT NOT NULL REFERENCES runs(run_id), sequence INTEGER NOT NULL, "
                        "record_json TEXT NOT NULL, created_at TEXT NOT NULL, "
                        "UNIQUE(run_id, sequence))"
                    )
                    connection.execute(
                        "INSERT INTO rfa_schema_migrations VALUES (2, ?)",
                        (datetime.now(UTC).isoformat(),),
                    )
                if not connection.execute(
                    "SELECT 1 FROM rfa_schema_migrations WHERE version = 3"
                ).fetchone():
                    for statement in (
                        "CREATE TABLE product_tasks (task_id TEXT PRIMARY KEY REFERENCES "
                        "product_task_owners(task_id), task_json TEXT NOT NULL)",
                        "CREATE TABLE team_slots (task_id TEXT PRIMARY KEY REFERENCES "
                        "product_tasks(task_id), team_id TEXT NOT NULL UNIQUE, "
                        "generation INTEGER NOT NULL, phase TEXT NOT NULL, "
                        "request_fingerprint TEXT NOT NULL, lifecycle_json TEXT NOT NULL)",
                        "CREATE TABLE task_creation_keys (owner_id TEXT NOT NULL, "
                        "key_hash TEXT NOT NULL, request_fingerprint TEXT NOT NULL, "
                        "task_id TEXT NOT NULL REFERENCES product_tasks(task_id), "
                        "PRIMARY KEY(owner_id, key_hash))",
                        "CREATE TABLE team_members (team_id TEXT NOT NULL REFERENCES "
                        "team_slots(team_id), agent_id TEXT NOT NULL, state_json TEXT NOT NULL, "
                        "PRIMARY KEY(team_id, agent_id))",
                        "CREATE TABLE team_lifecycle_events (team_id TEXT NOT NULL REFERENCES "
                        "team_slots(team_id), generation INTEGER NOT NULL, phase TEXT NOT NULL, "
                        "record_json TEXT NOT NULL, PRIMARY KEY(team_id, generation, phase))",
                    ):
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO rfa_schema_migrations VALUES (3, ?)",
                        (datetime.now(UTC).isoformat(),),
                    )
                # Credentials authenticate this installation's owner, not a fixture
                # or a user ID supplied in a request. No membership is implied.
                connection.execute(
                    "INSERT OR IGNORE INTO local_identity VALUES (1, ?)",
                    (
                        TrustedPrincipal(
                            user_id=new_id("owner"), authenticated=True
                        ).model_dump_json(),
                    ),
                )

        await asyncio.to_thread(operation)

    @staticmethod
    def _authenticated(principal: TrustedPrincipal) -> str:
        if not principal.authenticated or not principal.user_id:
            raise RfaError("authentication_required", "유효한 API 인증이 필요합니다.")
        return principal.user_id

    async def local_principal(self) -> TrustedPrincipal:
        def operation() -> TrustedPrincipal:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT principal_json FROM local_identity WHERE singleton = 1"
                ).fetchone()
                if row is None:
                    raise RfaError("configuration_error", "로컬 소유자 초기화가 필요합니다.")
                return TrustedPrincipal.model_validate_json(row[0])

        return await asyncio.to_thread(operation)

    @staticmethod
    def _session(connection: sqlite3.Connection, session_id: str, owner: str) -> SessionRecord:
        row = connection.execute(
            "SELECT * FROM sessions WHERE session_id = ? AND owner_id = ?", (session_id, owner)
        ).fetchone()
        if row is None:
            raise ResourceNotFoundError("session")
        tasks = connection.execute(
            "SELECT task_id FROM session_tasks WHERE session_id = ? ORDER BY task_id", (session_id,)
        ).fetchall()
        return SessionRecord(**dict(row), task_ids=tuple(item[0] for item in tasks))

    async def create_session(self, principal: TrustedPrincipal) -> SessionRecord:
        owner = self._authenticated(principal)
        now = datetime.now(UTC)
        record = SessionRecord(
            session_id=new_id("session"),
            thread_id=new_id("thread"),
            owner_id=owner,
            created_at=now,
            updated_at=now,
        )

        def operation() -> None:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
                    (record.session_id, record.thread_id, owner, now.isoformat(), now.isoformat()),
                )

        await asyncio.to_thread(operation)
        return record

    async def list_sessions(self, principal: TrustedPrincipal) -> list[SessionRecord]:
        owner = self._authenticated(principal)

        def operation() -> list[SessionRecord]:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT session_id FROM sessions WHERE owner_id = ? "
                    "ORDER BY updated_at DESC, session_id",
                    (owner,),
                ).fetchall()
                return [self._session(connection, row[0], owner) for row in rows]

        return await asyncio.to_thread(operation)

    async def get_session(self, session_id: str, principal: TrustedPrincipal) -> SessionDetail:
        owner = self._authenticated(principal)

        def operation() -> SessionDetail:
            with self._connect() as connection:
                # One read snapshot; authorize before reading messages or run data.
                connection.execute("BEGIN")
                session = self._session(connection, session_id, owner)
                messages = connection.execute(
                    "SELECT * FROM session_messages WHERE session_id = ? "
                    "ORDER BY created_at, rowid",
                    (session_id,),
                ).fetchall()
                runs = connection.execute(
                    "SELECT runs.*, sessions.thread_id FROM runs JOIN sessions USING(session_id) "
                    "WHERE runs.session_id = ? AND runs.owner_id = ? "
                    "ORDER BY runs.created_at, run_id",
                    (session_id, owner),
                ).fetchall()
                return SessionDetail(
                    **session.model_dump(),
                    messages=tuple(SessionMessage(**dict(row)) for row in messages),
                    runs=tuple(self._run_record(row) for row in runs),
                )

        return await asyncio.to_thread(operation)

    async def register_task_owner(self, task: PersistentTask) -> None:
        """Internal TaskFactory boundary, never a caller-supplied ownership claim.

        P0-019 owns Task creation/lifecycle. This minimal registry only permits
        already-created server Tasks to be associated with multiple sessions.
        """

        def operation() -> None:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT owner_id, domain_id FROM product_task_owners WHERE task_id = ?",
                    (task.task_id,),
                ).fetchone()
                if row is not None and tuple(row) != (task.owner_id, task.domain_id.value):
                    raise RfaError("idempotency_conflict", "Task 소유권을 변경할 수 없습니다.")
                connection.execute(
                    "INSERT OR IGNORE INTO product_task_owners VALUES (?, ?, ?)",
                    (task.task_id, task.owner_id, task.domain_id.value),
                )

        await asyncio.to_thread(operation)

    @staticmethod
    def _owned_team(connection, task_id: str, owner: str) -> TeamLifecycle:
        owned = connection.execute(
            "SELECT 1 FROM product_task_owners WHERE task_id = ? AND owner_id = ?",
            (task_id, owner),
        ).fetchone()
        if owned is None:
            raise ResourceNotFoundError("task")
        row = connection.execute(
            "SELECT lifecycle_json FROM team_slots WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            # Legacy registry entries lack goal/template; never invent these.
            raise RfaError("task_definition_missing", "Task 상세를 먼저 확인해야 합니다.")
        return TeamLifecycle.model_validate_json(row[0])

    @staticmethod
    def _team_event(connection, record: TeamLifecycle) -> None:
        connection.execute(
            "INSERT INTO team_lifecycle_events VALUES (?, ?, ?, ?)",
            (record.team.spec.team_id, record.generation, record.phase, record.model_dump_json()),
        )
        for member in record.team.member_states:
            connection.execute(
                "INSERT INTO team_members VALUES (?, ?, ?) ON CONFLICT(team_id, agent_id) "
                "DO UPDATE SET state_json=excluded.state_json",
                (record.team.spec.team_id, member.agent_id, member.model_dump_json()),
            )

    async def get_team_lifecycle(self, task_id: str, principal: TrustedPrincipal) -> TeamLifecycle:
        owner = self._authenticated(principal)

        def operation():
            with self._connect() as connection:
                return self._owned_team(connection, task_id, owner)

        return await asyncio.to_thread(operation)

    async def reserve_team(
        self,
        task: PersistentTask,
        spec: TeamSpec,
        principal: TrustedPrincipal,
        *,
        idempotency_key: str,
        request_fingerprint: str,
        mode: str,
        existing_task_id: str | None,
    ) -> tuple[TeamLifecycle, bool]:
        owner = self._authenticated(principal)
        task = PersistentTask.model_validate(task.model_dump())
        spec = TeamSpec.model_validate(spec.model_dump())
        if task.owner_id != owner or spec.owner_id != owner:
            raise ResourceNotFoundError("task")
        key_hash = _canonical_fingerprint({"key": idempotency_key})

        def operation():
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if existing_task_id is not None:
                    existing = self._owned_team(connection, existing_task_id, owner)
                    if existing.task.domain_id != task.domain_id:
                        raise ResourceNotFoundError("task")
                key = connection.execute(
                    "SELECT request_fingerprint, task_id FROM task_creation_keys "
                    "WHERE owner_id = ? AND key_hash = ?",
                    (owner, key_hash),
                ).fetchone()
                if key is not None:
                    if key[0] != request_fingerprint:
                        raise RfaError("idempotency_conflict", "동일 key의 내용이 다릅니다.")
                    return self._owned_team(connection, key[1], owner), False
                if existing_task_id is not None:
                    row = connection.execute(
                        "SELECT request_fingerprint FROM team_slots WHERE task_id = ?",
                        (existing_task_id,),
                    ).fetchone()
                    # Fingerprint is intent-only; explicit Task vs create belongs to key binding.
                    if row[0] != _canonical_fingerprint(
                        {
                            "goal": task.goal,
                            "spec": _team_definition(spec),
                        }
                    ):
                        raise RfaError("team_conflict", "기존 Task/팀 조건이 변경되었습니다.")
                    record, created = existing, False
                else:
                    if connection.execute(
                        "SELECT 1 FROM product_task_owners WHERE task_id = ?", (task.task_id,)
                    ).fetchone():
                        raise RfaError("team_conflict", "Task 식별자가 이미 사용 중입니다.")
                    record = TeamLifecycle(
                        task=task,
                        team=TeamInstance(
                            spec=spec,
                            state="provisioning",
                            mode=mode,
                            member_states=tuple(
                                MemberLifecycle(agent_id=m.spec.agent_id) for m in spec.members
                            ),
                        ),
                        generation=1,
                        operation="prepare",
                        operation_key=f"{spec.team_id}:prepare:1",
                        phase="pending",
                        reason="reserved",
                    )
                    connection.execute(
                        "INSERT INTO product_task_owners VALUES (?, ?, ?)",
                        (task.task_id, owner, task.domain_id.value),
                    )
                    connection.execute(
                        "INSERT INTO product_tasks VALUES (?, ?)",
                        (task.task_id, task.model_dump_json()),
                    )
                    connection.execute(
                        "INSERT INTO team_slots VALUES (?, ?, 1, 'pending', ?, ?)",
                        (
                            task.task_id,
                            spec.team_id,
                            _canonical_fingerprint(
                                {
                                    "goal": task.goal,
                                    "spec": _team_definition(spec),
                                }
                            ),
                            record.model_dump_json(),
                        ),
                    )
                    self._team_event(connection, record)
                    created = True
                connection.execute(
                    "INSERT INTO task_creation_keys VALUES (?, ?, ?, ?)",
                    (owner, key_hash, request_fingerprint, record.task.task_id),
                )
                return record, created

        return await asyncio.to_thread(operation)

    async def finish_team_operation(
        self,
        task_id: str,
        principal: TrustedPrincipal,
        *,
        generation: int,
        instance: TeamInstance,
        reason: str,
    ) -> TeamLifecycle:
        owner = self._authenticated(principal)
        instance = TeamInstance.model_validate(instance.model_dump())

        def operation():
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = self._owned_team(connection, task_id, owner)
                if current.generation != generation or current.phase != "pending":
                    raise RfaError(
                        "stale_team_operation", "이전 팀 작업 결과는 적용할 수 없습니다."
                    )
                if instance.spec != current.team.spec or instance.mode != current.team.mode:
                    raise RfaError("invalid_runtime_contract", "팀 응답 참조가 일치하지 않습니다.")
                record = TeamLifecycle.model_validate(
                    current.model_dump()
                    | {
                        "team": instance.model_dump(),
                        "phase": "finished",
                        "reason": reason,
                    }
                )
                changed = connection.execute(
                    "UPDATE team_slots SET phase='finished', lifecycle_json=? "
                    "WHERE task_id=? AND generation=? AND phase='pending'",
                    (record.model_dump_json(), task_id, generation),
                ).rowcount
                if changed != 1:
                    raise RfaError("stale_team_operation", "이전 팀 작업 결과입니다.")
                self._team_event(connection, record)
                return record

        return await asyncio.to_thread(operation)

    async def start_team_cleanup(
        self, task_id: str, principal: TrustedPrincipal
    ) -> tuple[TeamLifecycle, bool]:
        owner = self._authenticated(principal)

        def operation():
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = self._owned_team(connection, task_id, owner)
                if current.operation == "cleanup":
                    return (
                        current,
                        False,
                    )  # Unknown/failed cleanup is reconciled, not blindly repeated.
                if current.phase == "pending":
                    raise RfaError("team_busy", "진행 중인 팀 준비를 먼저 확인해야 합니다.")
                record = TeamLifecycle.model_validate(
                    current.model_dump()
                    | {
                        "team": current.team.model_dump() | {"state": "cleanup_pending"},
                        "generation": current.generation + 1,
                        "operation": "cleanup",
                        "operation_key": f"{current.team.spec.team_id}:cleanup:1",
                        "phase": "pending",
                        "reason": "cleanup_requested",
                    }
                )
                connection.execute(
                    "UPDATE team_slots SET generation=?, phase='pending', lifecycle_json=? "
                    "WHERE task_id=? AND generation=?",
                    (record.generation, record.model_dump_json(), task_id, current.generation),
                )
                self._team_event(connection, record)
                return record, True

        return await asyncio.to_thread(operation)

    async def create_owned_run(
        self,
        request: WorkRequest,
        principal: TrustedPrincipal,
        *,
        session_id: str | None,
        task_id: str | None = None,
    ) -> SessionRecord:
        owner = self._authenticated(principal)
        now = datetime.now(UTC).isoformat()

        def operation() -> SessionRecord:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                selected_session = session_id or new_id("session")
                if session_id is None:
                    connection.execute(
                        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
                        (selected_session, new_id("thread"), owner, now, now),
                    )
                self._session(connection, selected_session, owner)
                owned_key = "owned:" + _canonical_fingerprint(
                    {"owner": owner, "key": request.idempotency_key}
                )
                if connection.execute(
                    "SELECT 1 FROM runs WHERE run_id = ? OR idempotency_key = ?",
                    (request.run_id, owned_key),
                ).fetchone():
                    raise RfaError("idempotency_conflict", "중복 실행 요청입니다.")
                if connection.execute(
                    "SELECT 1 FROM runs WHERE session_id = ? AND status IN "
                    "('created', 'running', 'waiting_approval', 'outcome_unknown')",
                    (selected_session,),
                ).fetchone():
                    raise RfaError("thread_busy", "이 세션의 미완료 실행을 먼저 처리해야 합니다.")
                if task_id is not None:
                    task = connection.execute(
                        "SELECT domain_id FROM product_task_owners "
                        "WHERE task_id = ? AND owner_id = ?",
                        (task_id, owner),
                    ).fetchone()
                    if task is None:
                        raise ResourceNotFoundError("task")
                    if request.domain_id is None or task[0] != request.domain_id.value:
                        raise RfaError("task_domain_mismatch", "Task의 도메인을 명시해야 합니다.")
                    connection.execute(
                        "INSERT OR IGNORE INTO session_tasks VALUES (?, ?)",
                        (selected_session, task_id),
                    )
                connection.execute(
                    "INSERT INTO runs (run_id, request_id, trace_id, idempotency_key, status, "
                    "request_json, created_at, updated_at, owner_id, session_id, task_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        request.run_id,
                        request.request_id,
                        request.trace_id,
                        owned_key,
                        WorkStatus.CREATED.value,
                        request.model_dump_json(),
                        now,
                        now,
                        owner,
                        selected_session,
                        task_id,
                    ),
                )
                connection.execute(
                    "INSERT INTO session_messages VALUES (?, ?, ?, 'user', ?, ?)",
                    (new_id("message"), selected_session, request.run_id, request.query, now),
                )
                connection.execute(
                    "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                    (now, selected_session),
                )
                return self._session(connection, selected_session, owner)

        try:
            return await asyncio.to_thread(operation)
        except sqlite3.IntegrityError as exc:
            raise RfaError("idempotency_conflict", "중복 실행 요청입니다.") from exc

    @staticmethod
    def _run_record(row: sqlite3.Row) -> RunRecord:
        request = json.loads(row["request_json"])
        return RunRecord(
            run_id=row["run_id"],
            request_id=row["request_id"],
            trace_id=row["trace_id"],
            session_id=row["session_id"],
            thread_id=row["thread_id"],
            owner_id=row["owner_id"],
            task_id=row["task_id"],
            domain_id=request.get("domain_id"),
            status=row["status"],
            result=RunResult.model_validate_json(row["result_json"])
            if row["result_json"]
            else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def get_owned_run(self, run_id: str, principal: TrustedPrincipal) -> RunRecord:
        owner = self._authenticated(principal)

        def operation() -> RunRecord:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT runs.*, sessions.thread_id FROM runs JOIN sessions USING(session_id) "
                    "WHERE runs.run_id = ? AND runs.owner_id = ? AND sessions.owner_id = ?",
                    (run_id, owner, owner),
                ).fetchone()
                if row is None:
                    raise ResourceNotFoundError("run")
                return self._run_record(row)

        return await asyncio.to_thread(operation)

    async def observation_alias(
        self, run_id: str, principal: TrustedPrincipal, kind: str, reference: str = ""
    ) -> str:
        owned = await self.get_owned_run(run_id, principal)
        if kind == "domain":
            domain = DomainId(reference)
            if owned.domain_id is not None and owned.domain_id != domain:
                raise RfaError("invalid_trace_event", "도메인 관측 참조가 일치하지 않습니다.")

        def operation() -> str:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if kind == "domain":
                    previous = connection.execute(
                        "SELECT ref FROM observation_aliases WHERE run_id=? AND kind='domain'",
                        (run_id,),
                    ).fetchone()
                    if previous and previous[0] != reference:
                        raise RfaError("invalid_trace_event", "도메인 관측 참조가 변경되었습니다.")
                connection.execute(
                    "INSERT OR IGNORE INTO observation_aliases VALUES (?, ?, ?, ?)",
                    (run_id, kind, reference, new_id("obsref")),
                )
                return connection.execute(
                    "SELECT alias FROM observation_aliases WHERE run_id=? AND kind=? AND ref=?",
                    (run_id, kind, reference),
                ).fetchone()[0]

        return await asyncio.to_thread(operation)

    async def observation_context(
        self, run_id: str, principal: TrustedPrincipal
    ) -> ExecutionContext:
        row = await self.get_owned_run(run_id, principal)
        fields = {}
        for name in ("request_id", "trace_id", "run_id", "session_id", "task_id"):
            if name == "task_id" and row.task_id is None:
                continue
            # Do not duplicate caller request/trace strings. Mapping is run-local.
            fields[name] = await self.observation_alias(run_id, principal, name)
        fields["agent_id"] = await self.observation_alias(
            run_id, principal, "actor", "assistant-supervisor"
        )

        def bound_domain():
            with self._connect() as connection:
                domain = connection.execute(
                    "SELECT ref FROM observation_aliases WHERE run_id=? AND kind='domain'",
                    (run_id,),
                ).fetchone()
                return DomainId(domain[0]) if domain else row.domain_id

        return ExecutionContext(**fields, domain_id=await asyncio.to_thread(bound_domain))

    async def append_observation(
        self,
        run_id: str,
        principal: TrustedPrincipal,
        event: TraceEvent,
        *,
        origin: str,
        provider_ref: str,
        transport: str | None = None,
        provider_kind: str = "builtin",
    ) -> ObservationRecord:
        context = await self.observation_context(run_id, principal)
        event = TraceEvent.model_validate(event.model_dump())

        def operation() -> ObservationRecord:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                aliases = {
                    row[0]
                    for row in connection.execute(
                        "SELECT alias FROM observation_aliases WHERE run_id=?", (run_id,)
                    )
                }
                refs = [
                    value
                    for key, value in event.execution.model_dump().items()
                    if key not in {"schema_version", "domain_id"} and value is not None
                ]
                refs += [provider_ref, event.versions.policy, *event.versions.sources]
                refs += [
                    getattr(event, name)
                    for name in (
                        "policy_decision_id",
                        "sandbox_id",
                        "draft_id",
                        "approval_id",
                        "publication_id",
                        "evidence_ref",
                    )
                    if getattr(event, name) is not None
                ]
                if (
                    event.execution.model_dump(exclude={"agent_id"})
                    != context.model_dump(exclude={"agent_id"})
                    or any(ref not in aliases for ref in refs)
                    or event.versions.code != "rfa-observations-v1"
                    or any(
                        getattr(event.versions, name) is not None
                        for name in ("dataset", "evaluator", "model", "prompt", "template")
                    )
                ):
                    raise RfaError("invalid_trace_event", "신뢰된 관측 참조가 필요합니다.")
                connection.execute(
                    "INSERT INTO observation_sequences VALUES (?, 1) "
                    "ON CONFLICT(run_id) DO UPDATE SET sequence=sequence+1",
                    (run_id,),
                )
                sequence = connection.execute(
                    "SELECT sequence FROM observation_sequences WHERE run_id=?", (run_id,)
                ).fetchone()[0]
                record = ObservationRecord(
                    observation_id=new_id("observation"),
                    sequence=sequence,
                    origin=origin,
                    provider_ref=provider_ref,
                    transport=transport,
                    event=event,
                    provider_kind=provider_kind,
                )
                connection.execute(
                    "INSERT INTO observations VALUES (?, ?, ?, ?, ?)",
                    (
                        record.observation_id,
                        run_id,
                        sequence,
                        record.model_dump_json(),
                        record.event.timestamp.isoformat(),
                    ),
                )
                return record

        return await asyncio.to_thread(operation)

    async def get_observation(self, observation_id: str) -> ObservationRecord | None:
        def operation() -> ObservationRecord | None:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT record_json FROM observations WHERE observation_id=?", (observation_id,)
                ).fetchone()
                return ObservationRecord.model_validate_json(row[0]) if row else None

        return await asyncio.to_thread(operation)

    async def list_observations(
        self, run_id: str, principal: TrustedPrincipal
    ) -> tuple[ObservationRecord, ...]:
        await self.get_owned_run(run_id, principal)

        def operation() -> tuple[ObservationRecord, ...]:
            with self._connect() as connection:
                return tuple(
                    ObservationRecord.model_validate_json(row[0])
                    for row in connection.execute(
                        "SELECT record_json FROM observations WHERE run_id=? ORDER BY sequence",
                        (run_id,),
                    )
                )

        return await asyncio.to_thread(operation)

    async def create_run(self, request: WorkRequest) -> None:
        """Legacy/internal import only: these runs have no user-visible ownership."""
        now = datetime.now(UTC).isoformat()

        def operation() -> None:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO runs (
                        run_id, request_id, trace_id, idempotency_key, status,
                        request_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request.run_id,
                        request.request_id,
                        request.trace_id,
                        request.idempotency_key,
                        WorkStatus.CREATED.value,
                        request.model_dump_json(),
                        now,
                        now,
                    ),
                )

        try:
            await asyncio.to_thread(operation)
        except sqlite3.IntegrityError as exc:
            raise RfaError(
                "idempotency_conflict",
                "같은 run ID 또는 idempotency key의 작업이 이미 존재합니다.",
            ) from exc

    async def transition_run(self, run_id: str, status: WorkStatus) -> None:
        def operation() -> None:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT status FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                if row is None:
                    raise ResourceNotFoundError(f"run:{run_id}")
                current = WorkStatus(row["status"])
                ensure_transition(current, status)
                connection.execute(
                    "UPDATE runs SET status = ?, updated_at = ? WHERE run_id = ?",
                    (status.value, datetime.now(UTC).isoformat(), run_id),
                )

        await asyncio.to_thread(operation)

    async def save_result(self, result: RunResult) -> None:
        def operation() -> None:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE runs SET status = ?, result_json = ?, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (
                        result.status.value,
                        result.model_dump_json(),
                        result.updated_at.isoformat(),
                        result.run_id,
                    ),
                )
                if cursor.rowcount == 0:
                    raise ResourceNotFoundError(f"run:{result.run_id}")
                row = connection.execute(
                    "SELECT session_id FROM runs WHERE run_id = ?", (result.run_id,)
                ).fetchone()
                if row[0] is not None:
                    connection.execute(
                        "INSERT INTO session_messages VALUES (?, ?, ?, 'assistant', ?, ?) "
                        "ON CONFLICT(run_id, role) DO UPDATE SET content = excluded.content",
                        (
                            new_id("message"),
                            row[0],
                            result.run_id,
                            result.draft.content if result.draft else result.stop_reason,
                            result.updated_at.isoformat(),
                        ),
                    )
                    connection.execute(
                        "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                        (result.updated_at.isoformat(), row[0]),
                    )
                if result.draft is not None:
                    draft_json = result.draft.model_dump_json()
                    existing = connection.execute(
                        """
                        SELECT run_id, content_hash, draft_json FROM drafts
                        WHERE draft_id = ? AND version = ?
                        """,
                        (result.draft.draft_id, result.draft.version),
                    ).fetchone()
                    if existing is not None:
                        exact_match = (
                            existing["run_id"] == result.run_id
                            and existing["content_hash"] == result.draft.content_hash
                            and existing["draft_json"] == draft_json
                        )
                        if not exact_match:
                            raise RfaError(
                                "draft_version_conflict",
                                "같은 draft ID와 version에 다른 내용을 저장할 수 없습니다.",
                            )
                    else:
                        connection.execute(
                            """
                            INSERT INTO drafts (
                                draft_id, version, run_id, content_hash, draft_json, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                result.draft.draft_id,
                                result.draft.version,
                                result.run_id,
                                result.draft.content_hash,
                                draft_json,
                                datetime.now(UTC).isoformat(),
                            ),
                        )

        await asyncio.to_thread(operation)

    async def get_result(self, run_id: str) -> RunResult | None:
        def operation() -> RunResult | None:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT result_json FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                if row is None or row["result_json"] is None:
                    return None
                return RunResult.model_validate_json(row["result_json"])

        return await asyncio.to_thread(operation)

    async def save_checkpoint(self, run_id: str, node: str, metadata: dict[str, Any]) -> None:
        def operation() -> None:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO graph_checkpoints (run_id, node, metadata_json, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        node,
                        json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                        datetime.now(UTC).isoformat(),
                    ),
                )

        await asyncio.to_thread(operation)

    async def seed_documents_once(self, documents: list[KnowledgeDocument]) -> None:
        def operation() -> None:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS installation_seeds "
                    "(seed_id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
                )
                if connection.execute(
                    "SELECT 1 FROM installation_seeds WHERE seed_id = 'synthetic-fixtures-v1'"
                ).fetchone():
                    return
                now = datetime.now(UTC).isoformat()
                # Existing nonempty stores are never backfilled from fixtures.
                if not connection.execute("SELECT 1 FROM kb_documents LIMIT 1").fetchone():
                    connection.executemany(
                        "INSERT INTO kb_documents VALUES (?, ?, ?, ?, ?)",
                        [
                            (
                                d.source_id,
                                d.source_revision,
                                d.domain_id.value,
                                d.model_dump_json(),
                                now,
                            )
                            for d in documents
                        ],
                    )
                connection.execute(
                    "INSERT INTO installation_seeds VALUES ('synthetic-fixtures-v1', ?)", (now,)
                )

        await asyncio.to_thread(operation)

    async def upsert_documents(self, documents: list[KnowledgeDocument]) -> None:
        def operation() -> None:
            now = datetime.now(UTC).isoformat()
            with self._connect() as connection:
                connection.executemany(
                    """
                    INSERT OR REPLACE INTO kb_documents (
                        source_id, source_revision, domain_id, document_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            item.source_id,
                            item.source_revision,
                            item.domain_id.value,
                            item.model_dump_json(),
                            now,
                        )
                        for item in documents
                    ],
                )

        await asyncio.to_thread(operation)

    async def list_documents(self, domain_id: str) -> list[KnowledgeDocument]:
        def operation() -> list[KnowledgeDocument]:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT document_json FROM kb_documents
                    WHERE domain_id = ? ORDER BY source_id, source_revision
                    """,
                    (domain_id,),
                ).fetchall()
                return [KnowledgeDocument.model_validate_json(row["document_json"]) for row in rows]

        return await asyncio.to_thread(operation)


class LocalPolicy:
    adapter_name = "local-policy-v1"
    simulated = False
    policy_version = "local-v1"

    async def evaluate(self, request: PolicyRequest) -> PolicyDecision:
        principal = request.principal
        if request.action == "force_deny_for_simulation":
            return self._decision(request, False, "simulation_policy_denied", ())
        if request.tool_effect == ToolEffect.WRITE:
            return self._decision(request, False, "external_writes_disabled", ())

        allowed_audiences: list[Audience] = [Audience.PUBLIC]
        target = request.target_audience or request.resource_audience
        if target in {Audience.OWNER, Audience.PRIVATE} and not principal.authenticated:
            return self._decision(request, False, "target_identity_required", ())
        if target == Audience.COMPANY and not (principal.authenticated and principal.company_id):
            return self._decision(request, False, "target_company_membership_required", ())
        if target == Audience.BUSINESS_UNIT and not (
            principal.authenticated and principal.business_units
        ):
            return self._decision(
                request,
                False,
                "target_business_unit_membership_required",
                (),
            )
        if principal.authenticated:
            if principal.company_id:
                allowed_audiences.append(Audience.COMPANY)
            if principal.business_units:
                allowed_audiences.append(Audience.BUSINESS_UNIT)
            allowed_audiences.extend((Audience.OWNER, Audience.PRIVATE))
        if target == Audience.PUBLIC:
            allowed_audiences = [Audience.PUBLIC]
        elif target == Audience.COMPANY:
            allowed_audiences = [
                item for item in allowed_audiences if item in {Audience.PUBLIC, Audience.COMPANY}
            ]
        elif target == Audience.BUSINESS_UNIT:
            allowed_audiences = [
                item
                for item in allowed_audiences
                if item in {Audience.PUBLIC, Audience.COMPANY, Audience.BUSINESS_UNIT}
            ]

        allowed = self._resource_allowed(request, tuple(allowed_audiences))
        code = "allowed" if allowed else "membership_or_audience_denied"
        return self._decision(request, allowed, code, tuple(allowed_audiences))

    def _resource_allowed(
        self, request: PolicyRequest, allowed_audiences: tuple[Audience, ...]
    ) -> bool:
        audience = request.resource_audience
        principal = request.principal
        if audience not in allowed_audiences:
            return False
        if audience == Audience.PUBLIC:
            return True
        if audience == Audience.COMPANY:
            return bool(
                principal.authenticated
                and principal.company_id
                and (
                    request.resource_company_id is None
                    or request.resource_company_id == principal.company_id
                )
            )
        if audience == Audience.BUSINESS_UNIT:
            return bool(
                principal.authenticated
                and principal.business_units
                and (
                    request.resource_business_unit is None
                    or request.resource_business_unit in principal.business_units
                )
            )
        if audience in {Audience.OWNER, Audience.PRIVATE}:
            return bool(
                principal.authenticated
                and request.resource_owner_id
                and request.resource_owner_id == principal.user_id
            )
        return False

    def _decision(
        self,
        request: PolicyRequest,
        allowed: bool,
        code: str,
        allowed_audiences: tuple[Audience, ...],
    ) -> PolicyDecision:
        return PolicyDecision(
            request_id=request.request_id,
            trace_id=request.trace_id,
            run_id=request.run_id,
            agent_id=request.agent_id,
            domain_id=request.domain_id,
            allowed=allowed,
            code=code,
            safe_reason=(
                "요청된 자료 범위를 사용할 수 있습니다."
                if allowed
                else "검증된 identity와 자료 범위가 정책 조건을 충족하지 않습니다."
            ),
            policy_version=self.policy_version,
            allowed_audiences=allowed_audiences,
            simulated=False,
            adapter=self.adapter_name,
        )


class LocalRuntime:
    adapter_name = "local-runtime"
    simulated = False

    def __init__(self, *, timeout_seconds: float) -> None:
        self._timeout_seconds = timeout_seconds
        self._handlers: dict[str, TaskHandler] = {}
        self._results: dict[str, TaskResult] = {}
        self._idempotency: dict[str, tuple[str, TaskResult]] = {}
        self._inflight: dict[str, tuple[str, asyncio.Task[TaskResult]]] = {}
        self._lock = asyncio.Lock()
        self._teams: dict[str, TeamInstance] = {}
        self._team_keys: dict[str, tuple[str, TeamInstance]] = {}
        self._team_lock = asyncio.Lock()
        self._prepared_agents: dict[str, AgentSpec] = {}

    def register(self, task_type: str, handler: TaskHandler) -> None:
        self._handlers[task_type] = handler

    async def _prepare_member(self, member) -> None:
        # Local metadata only: no process/container/OS sandbox is provisioned.
        self._prepared_agents[member.spec.agent_id] = member.spec.model_copy(deep=True)

    async def _cleanup_member(self, member) -> None:
        self._prepared_agents.pop(member.spec.agent_id, None)

    async def prepare(self, spec: TeamSpec, *, idempotency_key: str) -> TeamInstance:
        spec = TeamSpec.model_validate_json(spec.model_dump_json(), strict=True)
        if (
            spec.template.runtime_kind != "local"
            or not spec.definition_digest
            or not spec.execution_budget
        ):
            raise RfaError("not_implemented", "검증된 local 팀 명세가 필요합니다.")
        fingerprint = _canonical_fingerprint({"prepare": spec.model_dump(mode="json")})
        async with self._team_lock:
            if idempotency_key in self._team_keys:
                old, result = self._team_keys[idempotency_key]
                self._ensure_same_request(old, fingerprint)
                return result.model_copy(deep=True)
            existing = self._teams.get(spec.team_id)
            if existing is not None:
                if existing.spec != spec:
                    raise RfaError("idempotency_conflict", "팀 명세가 변경되었습니다.")
                return existing.model_copy(deep=True)
            states = [MemberLifecycle(agent_id=m.spec.agent_id) for m in spec.members]
            for index, member in enumerate(spec.members):
                try:
                    await self._prepare_member(member)
                except RfaError as exc:
                    # This single typed fixture error explicitly means no allocation.
                    outcome = "failed" if exc.code == "member_prepare_failed" else "unknown"
                    states[index] = states[index].model_copy(update={"prepare": outcome})
                    break
                except Exception:
                    states[index] = states[index].model_copy(update={"prepare": "unknown"})
                    break
                states[index] = states[index].model_copy(update={"prepare": "prepared"})
            state = "ready" if all(m.prepare == "prepared" for m in states) else "failed"
            if any(m.prepare == "unknown" for m in states):
                state = "unknown"
            result = TeamInstance(
                spec=spec,
                state=state,
                mode=ExecutionMode.LOCAL,
                runtime_ref=f"local:{spec.team_id}",
                member_states=tuple(states),
                failed_agent_ids=tuple(m.agent_id for m in states if m.prepare == "failed"),
            )
            self._teams[spec.team_id] = result
            self._team_keys[idempotency_key] = (fingerprint, result.model_copy(deep=True))
            return result.model_copy(deep=True)

    async def cleanup(self, team_id: str, *, idempotency_key: str) -> TeamInstance:
        fingerprint = _canonical_fingerprint({"cleanup": team_id})
        async with self._team_lock:
            if idempotency_key in self._team_keys:
                old, result = self._team_keys[idempotency_key]
                self._ensure_same_request(old, fingerprint)
                return result.model_copy(deep=True)
            current = self._teams.get(team_id)
            if current is None:
                # Process restart cannot invent an external lifecycle receipt.
                raise RfaError("outcome_unknown", "로컬 준비 상태를 확인할 수 없습니다.")
            states = list(current.member_states)
            for index, member in enumerate(current.spec.members):
                if states[index].prepare not in {"prepared", "unknown"}:
                    continue
                try:
                    await self._cleanup_member(member)
                    outcome = "cleaned"
                except RfaError as exc:
                    outcome = "failed" if exc.code == "member_cleanup_failed" else "unknown"
                except Exception:
                    outcome = "unknown"
                states[index] = states[index].model_copy(update={"cleanup": outcome})
            state = "cleaned"
            if any(m.cleanup == "failed" for m in states):
                state = "failed"
            if any(m.cleanup == "unknown" for m in states):
                state = "unknown"
            result = current.model_copy(
                update={
                    "state": state,
                    "member_states": tuple(states),
                    "failed_agent_ids": tuple(
                        m.agent_id for m in states if m.prepare == "failed" or m.cleanup == "failed"
                    ),
                }
            )
            self._teams[team_id] = result
            self._team_keys[idempotency_key] = (fingerprint, result.model_copy(deep=True))
            return result.model_copy(deep=True)

    async def run(self, spec: AgentSpec, request: TaskRequest) -> TaskResult:
        fingerprint = _canonical_fingerprint(
            {
                "agent_spec": spec.model_dump(mode="json"),
                "task_request": request.model_dump(mode="json"),
            }
        )
        async with self._lock:
            cached = self._idempotency.get(request.idempotency_key)
            if cached is not None:
                cached_fingerprint, cached_result = cached
                self._ensure_same_request(cached_fingerprint, fingerprint)
                return cached_result

            pending = self._inflight.get(request.idempotency_key)
            if pending is not None:
                pending_fingerprint, task = pending
                self._ensure_same_request(pending_fingerprint, fingerprint)
            else:
                task = asyncio.create_task(self._execute(spec, request))
                self._inflight[request.idempotency_key] = (fingerprint, task)

        try:
            result = await asyncio.shield(task)
        except BaseException:
            if task.done():
                async with self._lock:
                    pending = self._inflight.get(request.idempotency_key)
                    if pending is not None and pending[1] is task:
                        self._inflight.pop(request.idempotency_key, None)
            raise

        async with self._lock:
            self._results[request.run_id] = result
            self._idempotency[request.idempotency_key] = (fingerprint, result)
            pending = self._inflight.get(request.idempotency_key)
            if pending is not None and pending[1] is task:
                self._inflight.pop(request.idempotency_key, None)
        return result

    async def _execute(self, spec: AgentSpec, request: TaskRequest) -> TaskResult:
        handler = self._handlers.get(request.task_type)
        if handler is None:
            result = TaskResult(
                request_id=request.request_id,
                trace_id=request.trace_id,
                run_id=request.run_id,
                agent_id=request.agent_id,
                domain_id=request.domain_id,
                status=ResultStatus.FAILED,
                error=StructuredError(
                    code="unknown_task_type",
                    message=f"등록되지 않은 task type입니다: {request.task_type}",
                    retryable=False,
                    request_id=request.request_id,
                    trace_id=request.trace_id,
                    run_id=request.run_id,
                ),
                simulated=False,
                adapter=self.adapter_name,
            )
        else:
            try:
                result = await asyncio.wait_for(
                    handler(spec, request), timeout=self._timeout_seconds
                )
            except TimeoutError:
                result = TaskResult(
                    request_id=request.request_id,
                    trace_id=request.trace_id,
                    run_id=request.run_id,
                    agent_id=request.agent_id,
                    domain_id=request.domain_id,
                    status=ResultStatus.TIMED_OUT,
                    error=StructuredError(
                        code="runtime_timeout",
                        message="로컬 task 실행 시간이 초과되었습니다.",
                        retryable=False,
                        request_id=request.request_id,
                        trace_id=request.trace_id,
                        run_id=request.run_id,
                    ),
                    simulated=False,
                    adapter=self.adapter_name,
                )
        return result

    @staticmethod
    def _ensure_same_request(cached_fingerprint: str, fingerprint: str) -> None:
        if cached_fingerprint != fingerprint:
            raise RfaError(
                "idempotency_conflict",
                "같은 idempotency key로 다른 runtime 요청을 실행할 수 없습니다.",
            )

    async def status(self, run_id: str) -> TaskResult | None:
        return self._results.get(run_id)

    async def cancel(self, run_id: str) -> TaskResult:
        raise RfaError(
            "not_implemented",
            "P0 local runtime은 task 취소를 구현하지 않았습니다.",
        )


class LocalJsonlTrace:
    adapter_name = "local-jsonl-trace"
    simulated = False

    def __init__(
        self,
        trace_dir: Path,
        redactor: SecretRedactor,
        *,
        repository: SqliteWorkRepository | None = None,
        retention_days: int = 7,
    ) -> None:
        self.trace_dir = trace_dir
        self._redactor = redactor
        self.repository = repository
        self.retention_days = retention_days
        self._lock = asyncio.Lock()

    async def emit(
        self,
        *,
        event: str,
        request_id: str,
        trace_id: str,
        run_id: str,
        status: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        # Legacy public shape remains callable but no longer accepts arbitrary
        # payloads or treats a caller-proposed identifier as trusted provenance.
        raise RfaError("invalid_trace_event", "신뢰된 관측 recorder를 사용해야 합니다.")

    async def emit_event(self, event: TraceEvent) -> None:
        raise RfaError("invalid_trace_event", "영속 관측 참조가 필요합니다.")

    async def emit_observation(self, record: ObservationRecord) -> None:
        record = ObservationRecord.model_validate(record.model_dump())
        if (
            self.repository is None
            or await self.repository.get_observation(record.observation_id) != record
        ):
            raise RfaError("invalid_trace_event", "확인되지 않은 관측은 내보낼 수 없습니다.")
        now = datetime.now(UTC)
        async with self._lock:
            await asyncio.to_thread(self._append_owned, record, now)

    @staticmethod
    def _private_open(path: Path, flags: int) -> int:
        descriptor = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise RfaError("configuration_error", "관측 파일은 일반 파일이어야 합니다.")
        os.fchmod(descriptor, 0o600)
        return descriptor

    def _append_owned(self, record: ObservationRecord, now: datetime) -> None:
        directory = self.trace_dir.absolute() / "rfa-observations-v1"
        # Refuse symlink traversal; do not touch legacy events.jsonl or unrelated data.
        if any(path.is_symlink() for path in (directory, *directory.parents)):
            raise RfaError("configuration_error", "관측 경로에 symlink를 사용할 수 없습니다.")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        manifest = directory / "owned.json"
        try:
            with _sqlite_setup_guard(directory / "export"):
                if manifest.is_symlink():
                    raise ValueError("symlink manifest")
                if manifest.exists():
                    fd = self._private_open(manifest, os.O_RDONLY)
                    with os.fdopen(fd, "r", encoding="utf-8") as handle:
                        registry = json.loads(handle.read(1_000_000))
                    if set(registry) != {"owner", "files"} or not re.fullmatch(
                        r"[0-9a-f]{32}", registry["owner"]
                    ):
                        raise ValueError("invalid manifest")
                else:
                    # Never adopt a pre-existing arbitrary file as ours.
                    registry = {"owner": new_id("export").split("_", 1)[1], "files": []}
                pattern = r"events-[0-9]{8}-" + registry["owner"] + r"\.jsonl"
                if not isinstance(registry["files"], list) or any(
                    not isinstance(name, str) or not re.fullmatch(pattern, name)
                    for name in registry["files"]
                ):
                    raise ValueError("invalid manifest entries")
                retained = []
                cutoff = (now - timedelta(days=self.retention_days)).strftime("%Y%m%d")
                for name in registry["files"]:
                    path = directory / name
                    if path.is_symlink():
                        raise ValueError("symlink in owned entries")
                    if path.exists() and not path.is_file():
                        raise ValueError("nonregular owned entry")
                    if name[7:15] < cutoff:
                        if path.exists():
                            path.unlink()
                    else:
                        retained.append(name)
                name = "events-" + now.strftime("%Y%m%d") + "-" + registry["owner"] + ".jsonl"
                path = directory / name
                if name not in retained and (path.exists() or path.is_symlink()):
                    raise ValueError("unregistered file is not adopted")
                # Reserve ownership before creation. A crash before log creation is
                # recoverable; a crash after append can yield a duplicate export
                # (deduplicate by observation_id). The SQLite ledger is authoritative.
                registry["files"] = sorted(set([*retained, name]))
                temporary = directory / (new_id("manifest") + ".tmp")
                fd = self._private_open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(registry, handle, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, manifest)
                fd = self._private_open(
                    path,
                    os.O_WRONLY | os.O_APPEND | (0 if path.exists() else os.O_CREAT | os.O_EXCL),
                )
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(record.model_dump_json() + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise RfaError("configuration_error", "안전한 관측 파일 저장에 실패했습니다.") from exc
