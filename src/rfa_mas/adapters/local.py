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

from rfa_mas.application.state_machine import ensure_effect_transition, ensure_transition
from rfa_mas.adapters.retrieval import bm25_scores, lexical_terms
from rfa_mas.application.source_access import (
    LOCAL_ENDPOINTS, ProjectResolver, fresh_principal, no_projects, permitted, project_allowed,
)
from rfa_mas.contracts import (
    AgentSpec,
    Audience,
    DomainId,
    DraftBundle,
    ExecutionContext,
    ExecutionMode,
    FeedbackApplication,
    FeedbackRecord,
    JobRun,
    KnowledgeDelete,
    KnowledgeDocument,
    KnowledgeDocumentV11,
    KnowledgeRevision,
    KnowledgeWrite,
    MemberLifecycle,
    Notification,
    ObservationRecord,
    PersistentTask,
    PolicyDecision,
    PolicyRequest,
    PublicationStatus,
    PublicationReceipt,
    ResultStatus,
    RoleOutcome,
    RunRecord,
    RunResult,
    Schedule,
    SessionDetail,
    SessionMessage,
    SessionRecord,
    SourceMetadata,
    SourceRead,
    SourceRevisionRef,
    StructuredError,
    TaskRequest,
    TaskResult,
    TeamInstance,
    TeamLifecycle,
    TeamRunResult,
    TeamSpec,
    TodoCandidate,
    ToolEffect,
    ToolRequest,
    ToolResult,
    TraceEvent,
    TrustedPrincipal,
    WorkRequest,
    WorkStatus,
    new_id,
    sha256_text,
)
from rfa_mas.errors import ResourceNotFoundError, RfaError
from rfa_mas.ports import TaskHandler
from rfa_mas.ports.interfaces import EffectRecord, RetryLink
from rfa_mas.security import SecretRedactor

# P0-021 ledger: next action shown for each durable effect state.
_EFFECT_NEXT_ACTION = {
    "intent": "wait",
    "inflight": "wait",
    "completed": "none",
    "outcome_unknown": "query",
}


def _canonical_fingerprint(value: dict[str, Any]) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# P1-001D: fixed SQL (no user input) turning punctuation/whitespace in the lowered document
# text "t" into spaces, so short tokens such as "a" or "17" match only as whole tokens.
_TOKEN_BREAKS = "t"
for _mark in (":", ",", ".", "(", ")", "[", "]", "{", "}", "/", "#", "!", "?", ";", '"', "'",
              "*", "=", "+", "|", "<", ">", "~", "@", "%", "&", "-", "_"):
    _TOKEN_BREAKS = f"replace({_TOKEN_BREAKS}, '{_mark.replace(chr(39), chr(39) * 2)}', ' ')"
for _code in (9, 10, 13):
    _TOKEN_BREAKS = f"replace({_TOKEN_BREAKS}, char({_code}), ' ')"


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
    def __init__(self, path: Path, *, project_resolver: ProjectResolver = no_projects) -> None:
        self.path = path
        self.project_resolver = project_resolver

    def _private_files(self, *, create: bool = False) -> None:
        # Harden the application DB itself, not just the separate checkpointer.
        # Same-user hostile directory replacement is not an OS sandbox boundary.
        for path in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
            if path == self.path and create:
                flags |= os.O_CREAT
            try:
                fd = os.open(path, flags, 0o600)
            except FileNotFoundError:
                if path == self.path:
                    raise RfaError(
                        "configuration_error", "자료 저장소가 초기화되지 않았습니다."
                    ) from None
                continue
            except OSError:
                raise RfaError(
                    "configuration_error", "안전한 자료 저장 파일이 필요합니다."
                ) from None
            try:
                info = os.fstat(fd)
                # SQLite removes sidecars when its final connection closes. An
                # already opened descriptor may therefore have no directory link.
                # The primary DB must never disappear or gain another hardlink.
                allowed_links = {1} if path == self.path else {0, 1}
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_nlink not in allowed_links
                ):
                    raise RfaError(
                        "configuration_error", "자료 저장 파일 형식/소유권이 올바르지 않습니다."
                    )
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self._private_files(create=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            self._private_files()
            with connection:
                yield connection
        finally:
            connection.close()

    async def initialize(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Validate before path.resolve() in the shared startup guard.
        self._private_files(create=True)

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
                if not connection.execute(
                    "SELECT 1 FROM rfa_schema_migrations WHERE version = 4"
                ).fetchone():
                    connection.execute(
                        "CREATE TABLE kb_sources (source_id TEXT PRIMARY KEY, owner_id TEXT, "
                        "domain_id TEXT NOT NULL, provider TEXT NOT NULL, namespace TEXT NOT NULL, "
                        "external_id TEXT NOT NULL, current_revision TEXT, "
                        "restricted INTEGER NOT NULL, "
                        "UNIQUE(owner_id, domain_id, provider, namespace, external_id))"
                    )
                    connection.execute(
                        "CREATE TABLE kb_source_revisions (source_id TEXT NOT NULL REFERENCES "
                        "kb_sources(source_id), source_revision TEXT NOT NULL, "
                        "metadata_json TEXT NOT NULL, operation TEXT NOT NULL, "
                        "provider_revision TEXT NOT NULL, fingerprint TEXT NOT NULL, "
                        "PRIMARY KEY(source_id, source_revision), "
                        "UNIQUE(source_id, operation, provider_revision))"
                    )
                    self._index_legacy_documents(connection)
                    connection.execute(
                        "INSERT INTO rfa_schema_migrations VALUES (4, ?)",
                        (datetime.now(UTC).isoformat(),),
                    )
                if not connection.execute(
                    "SELECT 1 FROM rfa_schema_migrations WHERE version=5"
                ).fetchone():
                    connection.execute(
                        "CREATE TABLE kb_revision_context (source_id TEXT NOT NULL, "
                        "source_revision TEXT NOT NULL, content_hash TEXT NOT NULL, "
                        "character_count INTEGER NOT NULL, epistemic_state TEXT NOT NULL, "
                        "parent_refs TEXT NOT NULL, PRIMARY KEY(source_id,source_revision), "
                        "FOREIGN KEY(source_id,source_revision) "
                        "REFERENCES kb_documents(source_id,source_revision))"
                    )
                    self._backfill_context(connection)
                    connection.execute("INSERT INTO rfa_schema_migrations VALUES (5, ?)",
                                       (datetime.now(UTC).isoformat(),))
                if not connection.execute(
                    "SELECT 1 FROM rfa_schema_migrations WHERE version=6"
                ).fetchone():
                    # P0-020: Run<->Task<->Team binding and durable role receipts.
                    for statement in (
                        "CREATE TABLE run_team_bindings (run_id TEXT PRIMARY KEY REFERENCES "
                        "runs(run_id), task_id TEXT NOT NULL REFERENCES product_tasks(task_id), "
                        "team_id TEXT NOT NULL, owner_id TEXT NOT NULL, domain_id TEXT NOT NULL, "
                        "bound_at TEXT NOT NULL)",
                        "CREATE TABLE role_executions (execution_key TEXT PRIMARY KEY, "
                        "run_id TEXT NOT NULL REFERENCES run_team_bindings(run_id), "
                        "role TEXT NOT NULL, agent_id TEXT NOT NULL, status TEXT NOT NULL, "
                        "outcome_json TEXT, started_at TEXT NOT NULL, finished_at TEXT, "
                        "UNIQUE(run_id, role))",
                        "CREATE TABLE team_run_results (run_id TEXT PRIMARY KEY REFERENCES "
                        "run_team_bindings(run_id), result_json TEXT NOT NULL, "
                        "created_at TEXT NOT NULL)",
                    ):
                        connection.execute(statement)
                    connection.execute("INSERT INTO rfa_schema_migrations VALUES (6, ?)",
                                       (datetime.now(UTC).isoformat(),))
                if not connection.execute(
                    "SELECT 1 FROM rfa_schema_migrations WHERE version=7"
                ).fetchone():
                    # P1-004B: owner-scoped work candidates with a dedup fingerprint.
                    connection.execute(
                        "CREATE TABLE todo_candidates (candidate_id TEXT PRIMARY KEY, "
                        "owner_id TEXT NOT NULL, domain_id TEXT NOT NULL, "
                        "fingerprint TEXT NOT NULL, state TEXT NOT NULL, "
                        "candidate_json TEXT NOT NULL, updated_at TEXT NOT NULL, "
                        "UNIQUE(owner_id, domain_id, fingerprint))"
                    )
                    connection.execute("INSERT INTO rfa_schema_migrations VALUES (7, ?)",
                                       (datetime.now(UTC).isoformat(),))
                if not connection.execute(
                    "SELECT 1 FROM rfa_schema_migrations WHERE version=8"
                ).fetchone():
                    # P1-005A: immutable DRAFT version metadata and publication receipts.
                    for statement in (
                        "CREATE TABLE draft_version_meta (draft_id TEXT NOT NULL, "
                        "version INTEGER NOT NULL, run_id TEXT NOT NULL REFERENCES runs(run_id), "
                        "attachments_json TEXT NOT NULL, created_at TEXT NOT NULL, "
                        "PRIMARY KEY(draft_id, version))",
                        "CREATE TABLE publications (publication_id TEXT PRIMARY KEY, "
                        "run_id TEXT NOT NULL REFERENCES runs(run_id), owner_id TEXT NOT NULL, "
                        "idempotency_key TEXT NOT NULL, status TEXT NOT NULL, "
                        "receipt_json TEXT NOT NULL, updated_at TEXT NOT NULL, "
                        "UNIQUE(owner_id, idempotency_key))",
                    ):
                        connection.execute(statement)
                    connection.execute("INSERT INTO rfa_schema_migrations VALUES (8, ?)",
                                       (datetime.now(UTC).isoformat(),))
                if not connection.execute(
                    "SELECT 1 FROM rfa_schema_migrations WHERE version=9"
                ).fetchone():
                    # P0-021: owner-scoped effect ledger (state, payload fingerprint, result
                    # ref, approval mirror), cancel/revocation barriers and retry lineage.
                    for statement in (
                        "CREATE TABLE effect_ledger (owner_id TEXT NOT NULL, "
                        "operation_key TEXT NOT NULL, kind TEXT NOT NULL, run_id TEXT, "
                        "payload_fingerprint TEXT NOT NULL, state TEXT NOT NULL CHECK (state IN "
                        "('intent', 'inflight', 'completed', 'outcome_unknown')), outcome TEXT, "
                        "result_ref TEXT, approval_json TEXT, next_action TEXT NOT NULL, "
                        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
                        "PRIMARY KEY(owner_id, operation_key))",
                        "CREATE INDEX effect_ledger_run ON effect_ledger(run_id, kind)",
                        "CREATE TABLE run_effect_barriers (run_id TEXT PRIMARY KEY REFERENCES "
                        "runs(run_id), owner_id TEXT NOT NULL, reason TEXT NOT NULL CHECK "
                        "(reason IN ('cancelled', 'permission_revoked')), "
                        "created_at TEXT NOT NULL)",
                        "CREATE TABLE run_lineage (run_id TEXT PRIMARY KEY REFERENCES "
                        "runs(run_id), owner_id TEXT NOT NULL, retry_of TEXT NOT NULL UNIQUE "
                        "REFERENCES runs(run_id), created_at TEXT NOT NULL)",
                    ):
                        connection.execute(statement)
                    connection.execute("INSERT INTO rfa_schema_migrations VALUES (9, ?)",
                                       (datetime.now(UTC).isoformat(),))
                if not connection.execute(
                    "SELECT 1 FROM rfa_schema_migrations WHERE version=10"
                ).fetchone():
                    # P0-022 (after P0-021's 9): owner schedule intent, append-only
                    # history and the scheduled-run ledger. The runner alone owns the separate
                    # APScheduler job store; these tables never hold callables or credentials.
                    for statement in (
                        "CREATE TABLE schedules (schedule_id TEXT PRIMARY KEY, "
                        "owner_id TEXT NOT NULL, job_type TEXT NOT NULL CHECK (job_type IN "
                        "('kb_refresh', 'candidate_scan', 'briefing')), domain_id TEXT NOT NULL, "
                        "state TEXT NOT NULL CHECK (state IN ('active', 'disabled', 'cancelled')), "
                        "revision INTEGER NOT NULL, schedule_json TEXT NOT NULL, "
                        "updated_at TEXT NOT NULL)",
                        "CREATE INDEX schedules_owner ON schedules(owner_id, schedule_id)",
                        "CREATE TABLE schedule_history (schedule_id TEXT NOT NULL REFERENCES "
                        "schedules(schedule_id), revision INTEGER NOT NULL, action TEXT NOT NULL, "
                        "at TEXT NOT NULL, PRIMARY KEY(schedule_id, revision))",
                        # Idempotency: one run per UTC fire time AND per local wall-clock
                        # occurrence (a DST fall-back repeat maps to the same occurrence).
                        "CREATE TABLE schedule_runs (run_key TEXT PRIMARY KEY, schedule_id TEXT "
                        "NOT NULL REFERENCES schedules(schedule_id), owner_id TEXT NOT NULL, "
                        "job_type TEXT NOT NULL, scheduled_fire_time TEXT NOT NULL, "
                        "occurrence TEXT NOT NULL, status TEXT NOT NULL, reason TEXT, "
                        "summary_json TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, "
                        "UNIQUE(schedule_id, scheduled_fire_time), "
                        "UNIQUE(schedule_id, occurrence))",
                    ):
                        connection.execute(statement)
                    connection.execute("INSERT INTO rfa_schema_migrations VALUES (10, ?)",
                                       (datetime.now(UTC).isoformat(),))
                if not connection.execute(
                    "SELECT 1 FROM rfa_schema_migrations WHERE version=11"
                ).fetchone():
                    # P0-024: source-revision outbox (ids only, written in the KB write
                    # transaction), per-consumer processing records, owner-only notification
                    # history and exactly-once candidate notices per evidence revision set.
                    for statement in (
                        "CREATE TABLE source_revision_events (event_id INTEGER PRIMARY KEY "
                        "AUTOINCREMENT, owner_id TEXT, domain_id TEXT NOT NULL, "
                        "source_id TEXT NOT NULL, source_revision TEXT NOT NULL, "
                        "operation TEXT NOT NULL, derived INTEGER NOT NULL, "
                        "created_at TEXT NOT NULL, UNIQUE(source_id, source_revision))",
                        "CREATE INDEX source_events_owner ON "
                        "source_revision_events(owner_id, domain_id, event_id)",
                        "CREATE TABLE source_event_consumption (consumer_id TEXT NOT NULL, "
                        "event_id INTEGER NOT NULL REFERENCES source_revision_events(event_id), "
                        "run_key TEXT NOT NULL, PRIMARY KEY(consumer_id, event_id))",
                        "CREATE TABLE notifications (notification_id TEXT PRIMARY KEY, "
                        "owner_id TEXT NOT NULL, schedule_id TEXT NOT NULL REFERENCES "
                        "schedules(schedule_id), run_key TEXT NOT NULL UNIQUE REFERENCES "
                        "schedule_runs(run_key), delivery TEXT NOT NULL CHECK (delivery IN "
                        "('active', 'held')), held_at TEXT, hold_reason TEXT, "
                        "notification_json TEXT NOT NULL, created_at TEXT NOT NULL)",
                        "CREATE INDEX notifications_owner ON notifications(owner_id, created_at)",
                        "CREATE TABLE candidate_notices (owner_id TEXT NOT NULL, "
                        "candidate_id TEXT NOT NULL, evidence_key TEXT NOT NULL, "
                        "notification_id TEXT NOT NULL REFERENCES notifications(notification_id), "
                        "PRIMARY KEY(owner_id, candidate_id, evidence_key))",
                    ):
                        connection.execute(statement)
                    # Existing current revisions become unprocessed events once.
                    connection.execute(
                        "INSERT OR IGNORE INTO source_revision_events (owner_id, domain_id, "
                        "source_id, source_revision, operation, derived, created_at) "
                        "SELECT s.owner_id, s.domain_id, s.source_id, s.current_revision, "
                        "'backfill', CASE WHEN c.parent_refs IS NOT NULL AND "
                        "c.parent_refs != '[]' THEN 1 ELSE 0 END, ? FROM kb_sources s "
                        "LEFT JOIN kb_revision_context c ON c.source_id = s.source_id "
                        "AND c.source_revision = s.current_revision "
                        "WHERE s.current_revision IS NOT NULL",
                        (datetime.now(UTC).isoformat(),),
                    )
                    connection.execute("INSERT INTO rfa_schema_migrations VALUES (11, ?)",
                                       (datetime.now(UTC).isoformat(),))
                if not connection.execute(
                    "SELECT 1 FROM rfa_schema_migrations WHERE version=12"
                ).fetchone():
                    # P1-005B: owner feedback memory, revision history and application log.
                    # (After 9 P0-021, 10 P0-022 and 11 P0-024; each migration is independent.)
                    for statement in (
                        "CREATE TABLE feedback_items (feedback_id TEXT PRIMARY KEY, "
                        "owner_id TEXT NOT NULL, category TEXT NOT NULL, "
                        "state TEXT NOT NULL CHECK (state IN ('active', 'revoked')), "
                        "revision INTEGER NOT NULL, domain_id TEXT, target_audience TEXT, "
                        "target_channel TEXT, record_json TEXT NOT NULL, "
                        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
                        "CREATE INDEX feedback_items_owner ON feedback_items"
                        "(owner_id, state, category)",
                        "CREATE TABLE feedback_item_revisions (feedback_id TEXT NOT NULL "
                        "REFERENCES feedback_items(feedback_id), revision INTEGER NOT NULL, "
                        "record_json TEXT NOT NULL, created_at TEXT NOT NULL, "
                        "PRIMARY KEY(feedback_id, revision))",
                        "CREATE TABLE feedback_applications (feedback_id TEXT NOT NULL "
                        "REFERENCES feedback_items(feedback_id), feedback_revision INTEGER "
                        "NOT NULL, owner_id TEXT NOT NULL, run_id TEXT NOT NULL, "
                        "effect TEXT NOT NULL, application_json TEXT NOT NULL, "
                        "applied_at TEXT NOT NULL, "
                        "PRIMARY KEY(feedback_id, feedback_revision, run_id, effect))",
                        "CREATE INDEX feedback_applications_run ON feedback_applications"
                        "(owner_id, run_id)",
                    ):
                        connection.execute(statement)
                    connection.execute("INSERT INTO rfa_schema_migrations VALUES (12, ?)",
                                       (datetime.now(UTC).isoformat(),))
                if not connection.execute(
                    "SELECT 1 FROM rfa_schema_migrations WHERE version=13"
                ).fetchone():
                    # P0-025: owner-scoped append-only event feed for UI polling. Each
                    # owner has its own gap-free sequence (the cursor). References only.
                    # (After 9 P0-021, 10 P0-022, 11 P0-024 and 12 P1-005B.)
                    for statement in (
                        "CREATE TABLE run_event_feed (owner_id TEXT NOT NULL, "
                        "seq INTEGER NOT NULL, event_key TEXT NOT NULL, "
                        "run_id TEXT NOT NULL REFERENCES runs(run_id), kind TEXT NOT NULL, "
                        "occurred_at TEXT, recorded_at TEXT NOT NULL, event_json TEXT NOT NULL, "
                        "PRIMARY KEY(owner_id, seq), UNIQUE(owner_id, event_key))",
                        "CREATE INDEX run_event_feed_run ON run_event_feed(owner_id, run_id, seq)",
                    ):
                        connection.execute(statement)
                    connection.execute("INSERT INTO rfa_schema_migrations VALUES (13, ?)",
                                       (datetime.now(UTC).isoformat(),))
                # A role receipt left running by a previous process is unknown: never
                # success, never permission to re-execute a possibly effectful step.
                connection.execute(
                    "UPDATE role_executions SET status='unknown', finished_at=? "
                    "WHERE status='running'",
                    (datetime.now(UTC).isoformat(),),
                )
                # Same rule for every ledgered effect: an intent/inflight left by a previous
                # process may or may not have happened. Only a result query resolves it.
                connection.execute(
                    "UPDATE effect_ledger SET state='outcome_unknown', next_action='query', "
                    "updated_at=? WHERE state IN ('intent', 'inflight')",
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

    # -- P0-020 Run<->Task<->Team binding and role receipts ------------------------
    async def bind_run_team(
        self, run_id: str, principal: TrustedPrincipal, *, task_id: str, team_id: str
    ) -> tuple[str, str]:
        owner = self._authenticated(principal)
        now = datetime.now(UTC).isoformat()

        def operation() -> tuple[str, str]:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                run = connection.execute(
                    "SELECT session_id, task_id, request_json FROM runs "
                    "WHERE run_id = ? AND owner_id = ?",
                    (run_id, owner),
                ).fetchone()
                if run is None:
                    raise ResourceNotFoundError("run")
                self._ensure_open(connection, run_id)
                lifecycle = self._owned_team(connection, task_id, owner)
                if (
                    lifecycle.team.spec.team_id != team_id
                    or lifecycle.team.state != "ready"
                    or lifecycle.operation != "prepare"
                    or lifecycle.phase != "finished"
                    or lifecycle.task.status != "active"
                ):
                    raise RfaError("team_not_ready", "준비된 팀에만 실행을 결합할 수 있습니다.")
                domain = json.loads(run["request_json"]).get("domain_id")
                if domain != lifecycle.task.domain_id.value:
                    raise RfaError("task_domain_mismatch", "Task와 실행의 도메인이 다릅니다.")
                if run["task_id"] is not None and run["task_id"] != task_id:
                    raise RfaError("team_binding_conflict", "이 실행은 다른 Task에 결합되어 있습니다.")
                existing = connection.execute(
                    "SELECT task_id, team_id FROM run_team_bindings WHERE run_id = ?", (run_id,)
                ).fetchone()
                if existing is not None:
                    if (existing[0], existing[1]) != (task_id, team_id):
                        raise RfaError(
                            "team_binding_conflict", "이 실행은 다른 팀에 결합되어 있습니다."
                        )
                    return task_id, team_id
                connection.execute(
                    "INSERT INTO run_team_bindings VALUES (?, ?, ?, ?, ?, ?)",
                    (run_id, task_id, team_id, owner, domain, now),
                )
                connection.execute(
                    "UPDATE runs SET task_id = ? WHERE run_id = ? AND task_id IS NULL",
                    (task_id, run_id),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO session_tasks VALUES (?, ?)",
                    (run["session_id"], task_id),
                )
                return task_id, team_id

        return await asyncio.to_thread(operation)

    # -- P1-004B work candidates ------------------------------------------------------
    async def upsert_candidate(self, candidate: TodoCandidate, principal: TrustedPrincipal,
                               *, expected_state: str | None = None) -> TodoCandidate:
        """Insert, or replace only when the stored state still equals expected_state."""
        owner = self._authenticated(principal)
        candidate = TodoCandidate.model_validate(candidate.model_dump())
        if candidate.owner_id != owner:
            raise ResourceNotFoundError("candidate")

        def operation():
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT candidate_id, state FROM todo_candidates WHERE owner_id=? "
                    "AND domain_id=? AND fingerprint=?",
                    (owner, candidate.domain_id.value, candidate.fingerprint),
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO todo_candidates VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (candidate.candidate_id, owner, candidate.domain_id.value,
                         candidate.fingerprint, candidate.state, candidate.model_dump_json(),
                         candidate.updated_at.isoformat()),
                    )
                    return candidate
                if row["candidate_id"] != candidate.candidate_id or (
                    expected_state is not None and row["state"] != expected_state
                ):
                    raise RfaError("idempotency_conflict", "후보 상태가 변경되었습니다.")
                connection.execute(
                    "UPDATE todo_candidates SET state=?, candidate_json=?, updated_at=? "
                    "WHERE candidate_id=?",
                    (candidate.state, candidate.model_dump_json(),
                     candidate.updated_at.isoformat(), candidate.candidate_id),
                )
                return candidate

        return await asyncio.to_thread(operation)

    async def list_candidates(self, principal: TrustedPrincipal,
                              domain_id: DomainId | None = None) -> list[TodoCandidate]:
        owner = self._authenticated(principal)

        def operation():
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT candidate_json FROM todo_candidates WHERE owner_id=? "
                    "AND (? IS NULL OR domain_id=?) ORDER BY candidate_id",
                    (owner, domain_id.value if domain_id else None,
                     domain_id.value if domain_id else None),
                ).fetchall()
                return [TodoCandidate.model_validate_json(r[0]) for r in rows]

        return await asyncio.to_thread(operation)

    # -- P0-022 owner schedules and the scheduled-run ledger ----------------------------
    SCHEDULE_TRANSITIONS = {
        ("active", "disabled"): "disabled",
        ("disabled", "active"): "enabled",
        ("active", "cancelled"): "cancelled",
        ("disabled", "cancelled"): "cancelled",
    }

    @staticmethod
    def _schedule_row(connection, row) -> Schedule:
        history = connection.execute(
            "SELECT revision, action, at FROM schedule_history WHERE schedule_id=? "
            "ORDER BY revision",
            (row["schedule_id"],),
        ).fetchall()
        return Schedule.model_validate(
            json.loads(row["schedule_json"])
            | {"state": row["state"], "revision": row["revision"],
               "updated_at": row["updated_at"], "history": [dict(h) for h in history]}
        )

    @staticmethod
    def _owned_schedule_row(connection, schedule_id: str, owner: str):
        row = connection.execute(
            "SELECT * FROM schedules WHERE schedule_id=? AND owner_id=?", (schedule_id, owner)
        ).fetchone()
        if row is None:
            raise ResourceNotFoundError("schedule")  # Same answer for absent and not owned.
        return row

    async def create_schedule(self, schedule: Schedule, principal: TrustedPrincipal) -> Schedule:
        owner = self._authenticated(principal)
        schedule = Schedule.model_validate(schedule.model_dump())
        if schedule.owner_id != owner or schedule.state != "active" or schedule.revision != 1:
            raise RfaError("invalid_schedule", "예약 생성 요청이 올바르지 않습니다.")
        definition = schedule.model_dump(
            mode="json",
            include={"schedule_id", "owner_id", "job_type", "domain_id", "task_ref", "cron",
                     "timezone", "args", "created_at", "schema_version"},
        )

        def operation() -> Schedule:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO schedules VALUES (?, ?, ?, ?, 'active', 1, ?, ?)",
                    (schedule.schedule_id, owner, schedule.job_type, schedule.domain_id.value,
                     json.dumps(definition, sort_keys=True), schedule.created_at.isoformat()),
                )
                connection.execute(
                    "INSERT INTO schedule_history VALUES (?, 1, 'created', ?)",
                    (schedule.schedule_id, schedule.created_at.isoformat()),
                )
                row = self._owned_schedule_row(connection, schedule.schedule_id, owner)
                return self._schedule_row(connection, row)

        return await asyncio.to_thread(operation)

    async def list_schedules(self, principal: TrustedPrincipal) -> list[Schedule]:
        owner = self._authenticated(principal)

        def operation() -> list[Schedule]:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT * FROM schedules WHERE owner_id=? ORDER BY schedule_id", (owner,)
                ).fetchall()
                return [self._schedule_row(connection, row) for row in rows]

        return await asyncio.to_thread(operation)

    async def get_schedule(self, schedule_id: str, principal: TrustedPrincipal) -> Schedule:
        owner = self._authenticated(principal)

        def operation() -> Schedule:
            with self._connect() as connection:
                row = self._owned_schedule_row(connection, schedule_id, owner)
                return self._schedule_row(connection, row)

        return await asyncio.to_thread(operation)

    async def transition_schedule(
        self, schedule_id: str, principal: TrustedPrincipal, *, to_state: str, at: datetime
    ) -> Schedule:
        """Owner-only state change with append-only history. Cancelled is terminal."""
        owner = self._authenticated(principal)

        def operation() -> Schedule:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = self._owned_schedule_row(connection, schedule_id, owner)
                if row["state"] != to_state:
                    action = self.SCHEDULE_TRANSITIONS.get((row["state"], to_state))
                    if action is None:
                        raise RfaError("invalid_state_transition", "예약 상태를 바꿀 수 없습니다.")
                    revision = row["revision"] + 1
                    updated = connection.execute(
                        "UPDATE schedules SET state=?, revision=?, updated_at=? "
                        "WHERE schedule_id=? AND revision=?",
                        (to_state, revision, at.isoformat(), schedule_id, row["revision"]),
                    ).rowcount
                    if updated != 1:
                        raise RfaError("idempotency_conflict", "예약이 변경되었습니다.")
                    connection.execute(
                        "INSERT INTO schedule_history VALUES (?, ?, ?, ?)",
                        (schedule_id, revision, action, at.isoformat()),
                    )
                    row = self._owned_schedule_row(connection, schedule_id, owner)
                return self._schedule_row(connection, row)

        return await asyncio.to_thread(operation)

    async def schedule_task_allowed(
        self, task_id: str, domain_id: DomainId, principal: TrustedPrincipal
    ) -> bool:
        owner = self._authenticated(principal)

        def operation() -> bool:
            with self._connect() as connection:
                return connection.execute(
                    "SELECT 1 FROM product_task_owners WHERE task_id=? AND owner_id=? "
                    "AND domain_id=?",
                    (task_id, owner, domain_id.value),
                ).fetchone() is not None

        return await asyncio.to_thread(operation)

    async def runner_schedules(self) -> list[Schedule]:
        """Trusted runner-process read for job-store reconciliation (no user input)."""

        def operation() -> list[Schedule]:
            with self._connect() as connection:
                rows = connection.execute("SELECT * FROM schedules ORDER BY schedule_id").fetchall()
                return [self._schedule_row(connection, row) for row in rows]

        return await asyncio.to_thread(operation)

    async def runner_schedule(self, schedule_id: str) -> Schedule | None:
        def operation() -> Schedule | None:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM schedules WHERE schedule_id=?", (schedule_id,)
                ).fetchone()
                return None if row is None else self._schedule_row(connection, row)

        return await asyncio.to_thread(operation)

    @staticmethod
    def _job_run(row) -> JobRun:
        data = dict(row)
        return JobRun.model_validate(data | {"summary": json.loads(data.pop("summary_json"))})

    async def claim_schedule_run(self, run: JobRun) -> tuple[JobRun, bool]:
        """Insert once per (schedule, fire time) and (schedule, occurrence); else the original."""
        run = JobRun.model_validate(run.model_dump())

        def operation() -> tuple[JobRun, bool]:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                inserted = connection.execute(
                    "INSERT OR IGNORE INTO schedule_runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (run.run_key, run.schedule_id, run.owner_id, run.job_type,
                     run.scheduled_fire_time.isoformat(), run.occurrence, run.status,
                     run.reason, json.dumps(run.summary, sort_keys=True),
                     run.started_at.isoformat(),
                     run.finished_at.isoformat() if run.finished_at else None),
                ).rowcount
                row = connection.execute(
                    "SELECT * FROM schedule_runs WHERE run_key=? OR (schedule_id=? AND "
                    "(scheduled_fire_time=? OR occurrence=?)) ORDER BY started_at LIMIT 1",
                    (run.run_key, run.schedule_id, run.scheduled_fire_time.isoformat(),
                     run.occurrence),
                ).fetchone()
                return self._job_run(row), inserted == 1

        return await asyncio.to_thread(operation)

    async def finish_schedule_run(self, run: JobRun) -> JobRun:
        """Only a still-running claim can finish; a recovered/finished row is never rewritten."""
        run = JobRun.model_validate(run.model_dump())

        def operation() -> JobRun:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE schedule_runs SET status=?, reason=?, summary_json=?, finished_at=? "
                    "WHERE run_key=? AND status='running'",
                    (run.status, run.reason, json.dumps(run.summary, sort_keys=True),
                     run.finished_at.isoformat() if run.finished_at else None, run.run_key),
                )
                row = connection.execute(
                    "SELECT * FROM schedule_runs WHERE run_key=?", (run.run_key,)
                ).fetchone()
                return self._job_run(row)

        return await asyncio.to_thread(operation)

    async def recover_interrupted_schedule_runs(self, at: datetime) -> int:
        """Runner start only (it holds the owner lock): a run left running is unknown."""

        def operation() -> int:
            with self._connect() as connection:
                return connection.execute(
                    "UPDATE schedule_runs SET status='outcome_unknown', "
                    "reason='runner_interrupted', finished_at=? WHERE status='running'",
                    (at.isoformat(),),
                ).rowcount

        return await asyncio.to_thread(operation)

    async def list_schedule_runs(self, schedule_id: str, principal: TrustedPrincipal):
        owner = self._authenticated(principal)

        def operation() -> list[JobRun]:
            with self._connect() as connection:
                self._owned_schedule_row(connection, schedule_id, owner)
                rows = connection.execute(
                    "SELECT * FROM schedule_runs WHERE schedule_id=? AND owner_id=? "
                    "ORDER BY scheduled_fire_time",
                    (schedule_id, owner),
                ).fetchall()
                return [self._job_run(row) for row in rows]

        return await asyncio.to_thread(operation)

    # -- P0-024 source-revision outbox and owner notification history -------------------
    async def pending_source_events(self, principal: TrustedPrincipal, domain_id: DomainId,
                                    consumer_id: str, *, limit: int = 500) -> list[dict]:
        """Owner's original (non-derived) revisions not yet processed by this consumer."""
        owner = self._authenticated(principal)

        def operation() -> list[dict]:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT e.event_id, e.source_id, e.source_revision, e.operation "
                    "FROM source_revision_events e WHERE e.owner_id=? AND e.domain_id=? "
                    "AND e.derived=0 AND NOT EXISTS (SELECT 1 FROM source_event_consumption x "
                    "WHERE x.consumer_id=? AND x.event_id=e.event_id) "
                    "ORDER BY e.event_id LIMIT ?",
                    (owner, domain_id.value, consumer_id, limit),
                ).fetchall()
                return [dict(row) for row in rows]

        return await asyncio.to_thread(operation)

    async def consume_source_events(self, consumer_id: str, event_ids: list[int],
                                    run_key: str) -> int:
        def operation() -> int:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                return sum(
                    connection.execute(
                        "INSERT OR IGNORE INTO source_event_consumption VALUES (?, ?, ?)",
                        (consumer_id, event_id, run_key),
                    ).rowcount
                    for event_id in event_ids
                )

        return await asyncio.to_thread(operation)

    @staticmethod
    def _notification_row(row) -> Notification:
        return Notification.model_validate(
            json.loads(row["notification_json"])
            | {"delivery": row["delivery"], "held_at": row["held_at"],
               "hold_reason": row["hold_reason"]}
        )

    async def record_notification(self, notification: Notification, *,
                                  evidence: dict[str, str] | None = None,
                                  limit: int = 50) -> Notification | None:
        """One notification per run key. With `evidence`, drop items already noticed for the
        same candidate evidence (atomic with the notice rows); None when nothing is new."""
        notification = Notification.model_validate(notification.model_dump())

        def operation() -> Notification | None:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM notifications WHERE run_key=?", (notification.run_key,)
                ).fetchone()
                if row is not None:
                    return self._notification_row(row)
                items = list(notification.items)
                if evidence is not None:
                    items = [
                        item for item in items
                        if connection.execute(
                            "SELECT 1 FROM candidate_notices WHERE owner_id=? AND "
                            "candidate_id=? AND evidence_key=?",
                            (notification.owner_id, item.candidate_id,
                             evidence[item.candidate_id]),
                        ).fetchone() is None
                    ]
                    if not items:
                        return None
                items = [item.model_copy(update={"rank": number})
                         for number, item in enumerate(items[:limit], 1)]
                final = notification.model_copy(update={
                    "items": tuple(items), "delivery": "active", "held_at": None,
                    "hold_reason": None,
                })
                connection.execute(
                    "INSERT INTO notifications VALUES (?, ?, ?, ?, 'active', NULL, NULL, ?, ?)",
                    (final.notification_id, final.owner_id, final.schedule_id, final.run_key,
                     final.model_dump_json(exclude={"delivery", "held_at", "hold_reason"}),
                     final.created_at.isoformat(timespec="microseconds")),
                )
                if evidence is not None:
                    connection.executemany(
                        "INSERT INTO candidate_notices VALUES (?, ?, ?, ?)",
                        [(final.owner_id, item.candidate_id, evidence[item.candidate_id],
                          final.notification_id) for item in items],
                    )
                return final

        return await asyncio.to_thread(operation)

    async def hold_stale_notifications(self, owner_id: str, *, before: datetime,
                                       at: datetime) -> int:
        """Active notifications older than `before` move to history-only (held)."""

        def operation() -> int:
            with self._connect() as connection:
                return connection.execute(
                    "UPDATE notifications SET delivery='held', held_at=?, "
                    "hold_reason='stale_24h' WHERE owner_id=? AND delivery='active' "
                    "AND created_at < ?",
                    (at.isoformat(), owner_id,
                     before.astimezone(UTC).isoformat(timespec="microseconds")),
                ).rowcount

        return await asyncio.to_thread(operation)

    async def list_notifications(self, principal: TrustedPrincipal, *,
                                 include_held: bool = False) -> list[Notification]:
        owner = self._authenticated(principal)

        def operation() -> list[Notification]:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT * FROM notifications WHERE owner_id=? AND (? OR delivery='active') "
                    "ORDER BY created_at DESC, notification_id",
                    (owner, int(include_held)),
                ).fetchall()
                return [self._notification_row(row) for row in rows]

        return await asyncio.to_thread(operation)


    # -- P0-021 durable effect ledger -----------------------------------------------------
    # Ledger rows are written in the SAME SQLite transaction as the receipt that owns the
    # details (publication, role receipt, team slot), so the two never disagree. The
    # LangGraph checkpoint is a separate store and is never assumed atomic with them.
    @staticmethod
    def _effect(row: sqlite3.Row) -> EffectRecord:
        return EffectRecord(
            operation_key=row["operation_key"],
            kind=row["kind"],
            run_id=row["run_id"],
            payload_fingerprint=row["payload_fingerprint"],
            state=row["state"],
            outcome=row["outcome"],
            result_ref=row["result_ref"],
            approval=json.loads(row["approval_json"]) if row["approval_json"] else None,
            next_action=row["next_action"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _ensure_open(connection, run_id: str) -> None:
        """Cancel/revocation barrier: no NEW effect may start for this run."""
        row = connection.execute(
            "SELECT reason FROM run_effect_barriers WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is not None:
            raise RfaError(
                row[0], "취소되었거나 권한이 회수된 실행은 새 작업을 시작할 수 없습니다."
            )

    @staticmethod
    def _run_owner(connection, run_id: str) -> str:
        row = connection.execute("SELECT owner_id FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None or not row[0]:
            raise ResourceNotFoundError("run")
        return row[0]

    def _ledger_row(self, connection, owner: str, operation_key: str):
        return connection.execute(
            "SELECT * FROM effect_ledger WHERE owner_id = ? AND operation_key = ?",
            (owner, operation_key),
        ).fetchone()

    def _ledger_begin(
        self,
        connection,
        owner: str,
        *,
        operation_key: str,
        kind: str,
        run_id: str | None,
        fingerprint: str,
        state: str = "intent",
        result_ref: str | None = None,
        approval: dict | None = None,
    ) -> tuple[bool, EffectRecord]:
        row = self._ledger_row(connection, owner, operation_key)
        if row is not None:
            if (
                row["kind"] != kind
                or row["run_id"] != run_id
                or row["payload_fingerprint"] != fingerprint
            ):
                raise RfaError(
                    "idempotency_conflict", "같은 작업 key로 다른 요청을 실행할 수 없습니다."
                )
            return False, self._effect(row)
        now = datetime.now(UTC).isoformat()
        connection.execute(
            "INSERT INTO effect_ledger (owner_id, operation_key, kind, run_id, "
            "payload_fingerprint, state, outcome, result_ref, approval_json, next_action, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
            (
                owner,
                operation_key,
                kind,
                run_id,
                fingerprint,
                state,
                result_ref,
                json.dumps(approval, sort_keys=True) if approval is not None else None,
                _EFFECT_NEXT_ACTION[state],
                now,
                now,
            ),
        )
        return True, self._effect(self._ledger_row(connection, owner, operation_key))

    def _ledger_advance(
        self,
        connection,
        owner: str,
        operation_key: str,
        *,
        state: str,
        outcome: str | None = None,
        approval: dict | None = None,
        missing_ok: bool = False,
    ) -> EffectRecord | None:
        row = self._ledger_row(connection, owner, operation_key)
        if row is None:
            if missing_ok:  # Receipt created before migration 9: nothing to mirror.
                return None
            raise ResourceNotFoundError("effect")
        if row["state"] == state == "outcome_unknown":
            return self._effect(row)
        ensure_effect_transition(row["state"], state)
        changed = connection.execute(
            "UPDATE effect_ledger SET state = ?, outcome = COALESCE(?, outcome), "
            "approval_json = COALESCE(?, approval_json), next_action = ?, updated_at = ? "
            "WHERE owner_id = ? AND operation_key = ? AND state = ?",
            (
                state,
                outcome,
                json.dumps(approval, sort_keys=True) if approval is not None else None,
                _EFFECT_NEXT_ACTION[state],
                datetime.now(UTC).isoformat(),
                owner,
                operation_key,
                row["state"],
            ),
        ).rowcount
        if changed != 1:
            raise RfaError("invalid_state_transition", "작업 기록 상태가 변경되었습니다.")
        return self._effect(self._ledger_row(connection, owner, operation_key))

    async def begin_effect(
        self,
        run_id: str,
        *,
        operation_key: str,
        kind: str,
        payload_fingerprint: str,
        result_ref: str | None = None,
        approval: dict | None = None,
    ) -> tuple[bool, EffectRecord]:
        """Durable INTENT before an effectful call; the owner comes from the stored run.

        Returns (True, record) for a new intent, or (False, record) for the prior state
        of the same key. A different payload under the same key is rejected. The barrier
        blocks new intents only; replaying a completed result is not a new call.
        """

        def operation():
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                owner = self._run_owner(connection, run_id)
                if self._ledger_row(connection, owner, operation_key) is None:
                    self._ensure_open(connection, run_id)
                return self._ledger_begin(
                    connection,
                    owner,
                    operation_key=operation_key,
                    kind=kind,
                    run_id=run_id,
                    fingerprint=payload_fingerprint,
                    result_ref=result_ref,
                    approval=approval,
                )

        return await asyncio.to_thread(operation)

    async def advance_effect(
        self,
        run_id: str,
        operation_key: str,
        *,
        state: str,
        outcome: str | None = None,
        approval: dict | None = None,
    ) -> EffectRecord:
        def operation():
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                owner = self._run_owner(connection, run_id)
                return self._ledger_advance(
                    connection, owner, operation_key, state=state, outcome=outcome,
                    approval=approval,
                )

        return await asyncio.to_thread(operation)

    async def effects_by_ref(
        self, run_id: str, *, kind: str, result_ref: str
    ) -> tuple[EffectRecord, ...]:
        def operation():
            with self._connect() as connection:
                owner = self._run_owner(connection, run_id)
                rows = connection.execute(
                    "SELECT * FROM effect_ledger WHERE owner_id = ? AND run_id = ? AND kind = ? "
                    "AND result_ref = ? ORDER BY created_at, operation_key",
                    (owner, run_id, kind, result_ref),
                ).fetchall()
                return tuple(self._effect(row) for row in rows)

        return await asyncio.to_thread(operation)

    async def run_effects(
        self, run_id: str, principal: TrustedPrincipal
    ) -> tuple[EffectRecord, ...]:
        """Owner-authorized ledger of one run, including its bound Task team operations."""
        owner = self._authenticated(principal)
        await self.get_owned_run(run_id, principal)

        def operation():
            with self._connect() as connection:
                # Task teams reserved for this run: bound ones, and the run-keyed
                # reservation even when a crash happened before binding (P0-020 key).
                tasks = {
                    row[0]
                    for row in connection.execute(
                        "SELECT task_id FROM run_team_bindings WHERE run_id = ? AND owner_id = ? "
                        "UNION SELECT task_id FROM task_creation_keys WHERE owner_id = ? "
                        "AND key_hash = ?",
                        (run_id, owner, owner,
                         _canonical_fingerprint({"key": f"run-team:{run_id}"})),
                    ).fetchall()
                }
                rows = connection.execute(
                    "SELECT * FROM effect_ledger WHERE owner_id = ? AND run_id = ? "
                    "ORDER BY created_at, operation_key",
                    (owner, run_id),
                ).fetchall()
                for task_id in sorted(tasks):
                    rows += connection.execute(
                        "SELECT * FROM effect_ledger WHERE owner_id = ? AND run_id IS NULL "
                        "AND result_ref LIKE ? ORDER BY created_at, operation_key",
                        (owner, f"team:{task_id}@%"),
                    ).fetchall()
                return tuple(self._effect(row) for row in rows)

        return await asyncio.to_thread(operation)

    async def set_run_barrier(
        self, run_id: str, principal: TrustedPrincipal, reason: str
    ) -> str:
        """Durable cancel/revocation barrier; the first recorded reason is kept."""
        owner = self._authenticated(principal)
        if reason not in {"cancelled", "permission_revoked"}:
            raise RfaError("invalid_state_transition", "지원하지 않는 중단 사유입니다.")
        await self.get_owned_run(run_id, principal)

        def operation():
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT OR IGNORE INTO run_effect_barriers VALUES (?, ?, ?, ?)",
                    (run_id, owner, reason, datetime.now(UTC).isoformat()),
                )
                return connection.execute(
                    "SELECT reason FROM run_effect_barriers WHERE run_id = ?", (run_id,)
                ).fetchone()[0]

        return await asyncio.to_thread(operation)

    async def run_barrier(self, run_id: str, principal: TrustedPrincipal) -> str | None:
        owner = self._authenticated(principal)

        def operation():
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT reason FROM run_effect_barriers WHERE run_id = ? AND owner_id = ?",
                    (run_id, owner),
                ).fetchone()
                return row[0] if row else None

        return await asyncio.to_thread(operation)

    async def owned_run_request(self, run_id: str, principal: TrustedPrincipal) -> dict:
        owner = self._authenticated(principal)
        await self.get_owned_run(run_id, principal)

        def operation():
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT request_json FROM runs WHERE run_id = ? AND owner_id = ?",
                    (run_id, owner),
                ).fetchone()
                if row is None:
                    raise ResourceNotFoundError("run")
                return json.loads(row[0])

        return await asyncio.to_thread(operation)

    async def retry_links(
        self, run_id: str, principal: TrustedPrincipal
    ) -> tuple[RetryLink, ...]:
        """Explicit retry lineage touching this run (as the retry or as its source)."""
        owner = self._authenticated(principal)
        await self.get_owned_run(run_id, principal)

        def operation():
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT run_id, retry_of, created_at FROM run_lineage WHERE owner_id = ? "
                    "AND (run_id = ? OR retry_of = ?) ORDER BY created_at",
                    (owner, run_id, run_id),
                ).fetchall()
                return tuple(
                    RetryLink(run_id=r[0], retry_of=r[1], created_at=r[2]) for r in rows
                )

        return await asyncio.to_thread(operation)

    # -- P0-024: scheduled job effects in the P0-021 ledger -------------------------------
    _SCHEDULE_PHASES = {
        "intent": "intent",
        "succeeded": "completed",
        "skipped": "completed",
        "denied": "completed",
        "failed": "completed",
        "outcome_unknown": "outcome_unknown",
    }

    async def record_scheduled_effect(
        self, *, operation_key: str, owner_id: str, kind: str, phase: str,
        result_ref: str | None = None,
    ) -> EffectRecord:
        """Mirror one scheduled fire into the effect ledger (runner process only).

        The owner is verified against the scheduled-run ledger row for this exact run key
        and job type, never taken from the caller alone. The scheduled-run ledger stays
        authoritative for fire idempotency; this row adds intent/outcome durability.
        """
        state = self._SCHEDULE_PHASES.get(phase)
        if state is None or not kind.startswith("schedule:"):
            raise RfaError("invalid_state_transition", "지원하지 않는 예약 작업 기록입니다.")

        def operation() -> EffectRecord:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                run = connection.execute(
                    "SELECT run_key FROM schedule_runs WHERE run_key = ? AND owner_id = ? "
                    "AND job_type = ?",
                    (operation_key, owner_id, kind.removeprefix("schedule:")),
                ).fetchone()
                if run is None:
                    raise ResourceNotFoundError("schedule run")
                fingerprint = _canonical_fingerprint({"schedule_run": operation_key, "kind": kind})
                ref = result_ref or f"schedule_run:{operation_key}"
                _, record = self._ledger_begin(
                    connection, owner_id, operation_key=operation_key, kind=kind, run_id=None,
                    fingerprint=fingerprint, result_ref=ref,
                )
                if state == "intent" or record.state == "completed":
                    return record  # Replayed intent, or an outcome that is already final.
                return self._ledger_advance(
                    connection, owner_id, operation_key, state=state, outcome=phase
                )

        return await asyncio.to_thread(operation)


    # -- P1-005A immutable DRAFT versions and publication receipts ----------------------
    async def draft_versions(self, run_id: str, principal: TrustedPrincipal):
        """Owner-authorized versions (DraftBundle, attachments) ordered by version."""
        await self.get_owned_run(run_id, principal)

        def operation():
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT d.draft_json, m.attachments_json FROM drafts d "
                    "LEFT JOIN draft_version_meta m ON m.draft_id=d.draft_id "
                    "AND m.version=d.version WHERE d.run_id=? ORDER BY d.version",
                    (run_id,),
                ).fetchall()
                return [
                    (DraftBundle.model_validate_json(r[0]),
                     tuple(json.loads(r[1])) if r[1] else ())
                    for r in rows
                ]

        return await asyncio.to_thread(operation)

    async def append_draft_version(self, run_id: str, principal: TrustedPrincipal,
                                   draft: DraftBundle, attachments: tuple[dict, ...]) -> None:
        """Insert version N+1 only if N is the current latest version (optimistic CAS)."""
        await self.get_owned_run(run_id, principal)
        draft = DraftBundle.model_validate(draft.model_dump())
        now = datetime.now(UTC).isoformat()

        def operation():
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                latest = connection.execute(
                    "SELECT draft_id, max(version) FROM drafts WHERE run_id=?", (run_id,)
                ).fetchone()
                if latest[0] != draft.draft_id or latest[1] != draft.version - 1:
                    raise RfaError("draft_version_conflict", "최신 DRAFT 버전이 아닙니다.")
                connection.execute(
                    "INSERT INTO drafts (draft_id, version, run_id, content_hash, draft_json, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (draft.draft_id, draft.version, run_id, draft.content_hash,
                     draft.model_dump_json(), now),
                )
                connection.execute(
                    "INSERT INTO draft_version_meta VALUES (?, ?, ?, ?, ?)",
                    (draft.draft_id, draft.version, run_id, json.dumps(list(attachments)), now),
                )

        await asyncio.to_thread(operation)

    async def get_publication(self, run_id: str, principal: TrustedPrincipal):
        owner = self._authenticated(principal)
        await self.get_owned_run(run_id, principal)

        def operation():
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT receipt_json FROM publications WHERE run_id=? AND owner_id=? "
                    "ORDER BY updated_at DESC LIMIT 1",
                    (run_id, owner),
                ).fetchone()
                return PublicationReceipt.model_validate_json(row[0]) if row else None

        return await asyncio.to_thread(operation)

    async def put_publication(self, receipt: PublicationReceipt, principal: TrustedPrincipal,
                              *, expected_status: str | None) -> PublicationReceipt:
        """Create (expected_status None) or advance a receipt; never two per Run."""
        owner = self._authenticated(principal)
        receipt = PublicationReceipt.model_validate(receipt.model_dump())
        await self.get_owned_run(receipt.run_id, principal)
        now = datetime.now(UTC).isoformat()

        def operation():
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                same_key = connection.execute(
                    "SELECT receipt_json FROM publications WHERE owner_id=? AND idempotency_key=?",
                    (owner, receipt.idempotency_key),
                ).fetchone()
                existing = connection.execute(
                    "SELECT publication_id, status, receipt_json FROM publications "
                    "WHERE run_id=? AND owner_id=?",
                    (receipt.run_id, owner),
                ).fetchone()
                if expected_status is None:
                    if same_key is not None:
                        stored = PublicationReceipt.model_validate_json(same_key[0])
                        # P0-021: a same-key replay must be the SAME publication request;
                        # another run/content/approval under that key is rejected.
                        if (stored.run_id, stored.binding, stored.approval_id) != (
                            receipt.run_id, receipt.binding, receipt.approval_id
                        ):
                            raise RfaError(
                                "idempotency_conflict",
                                "같은 게시 key로 다른 게시 내용을 요청할 수 없습니다.",
                            )
                        return stored
                    if existing is not None:
                        raise RfaError("publication_exists", "이미 게시 요청이 있습니다.")
                    self._ensure_open(connection, receipt.run_id)
                    connection.execute(
                        "INSERT INTO publications VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (receipt.publication_id, receipt.run_id, owner,
                         receipt.idempotency_key, receipt.status.value,
                         receipt.model_dump_json(), now),
                    )
                    binding = receipt.binding
                    self._ledger_begin(
                        connection,
                        owner,
                        operation_key="publication:" + receipt.idempotency_key,
                        kind="publication",
                        run_id=receipt.run_id,
                        fingerprint=_canonical_fingerprint(
                            {
                                "run_id": receipt.run_id,
                                "binding": binding.model_dump(mode="json"),
                                "approval_id": receipt.approval_id,
                            }
                        ),
                        result_ref="publication:" + receipt.publication_id,
                        # Approval mirror bound to this publication (not an approval).
                        approval={
                            "approval_id": receipt.approval_id,
                            "draft_id": binding.draft_id,
                            "draft_version": binding.version,
                            "content_hash": binding.content_hash,
                            "payload_hash": binding.payload_hash,
                            "target": binding.target.model_dump(mode="json"),
                            "policy_version": binding.policy_version,
                        },
                    )
                    return receipt
                if existing is None or existing[0] != receipt.publication_id \
                        or existing[1] != expected_status:
                    raise RfaError("invalid_state_transition", "게시 상태가 변경되었습니다.")
                connection.execute(
                    "UPDATE publications SET status=?, receipt_json=?, updated_at=? "
                    "WHERE publication_id=?",
                    (receipt.status.value, receipt.model_dump_json(), now,
                     receipt.publication_id),
                )
                ledger_state = {
                    PublicationStatus.SUCCEEDED: "completed",
                    PublicationStatus.FAILED: "completed",
                    PublicationStatus.OUTCOME_UNKNOWN: "outcome_unknown",
                }.get(receipt.status)
                if ledger_state is not None:
                    self._ledger_advance(
                        connection,
                        owner,
                        "publication:" + receipt.idempotency_key,
                        state=ledger_state,
                        outcome=receipt.status.value,
                        missing_ok=True,
                    )
                return receipt

        return await asyncio.to_thread(operation)

    # -- P0-025 owner-scoped run event feed (polling cursor, append-only) ------------------
    async def record_run_events(
        self, principal: TrustedPrincipal, events: list[dict[str, Any]]
    ) -> int:
        """Append events not yet recorded, in the given order; returns how many were new.

        Each owner has its own gap-free sequence, so a cursor reveals nothing about other
        owners. Idempotent per (owner, event_key). A run not owned by the caller is skipped.
        """
        owner = self._authenticated(principal)
        now = datetime.now(UTC).isoformat()

        def operation() -> int:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                owned = {
                    row[0]
                    for row in connection.execute(
                        "SELECT run_id FROM runs WHERE owner_id = ?", (owner,)
                    ).fetchall()
                }
                last = connection.execute(
                    "SELECT coalesce(max(seq), 0) FROM run_event_feed WHERE owner_id = ?",
                    (owner,),
                ).fetchone()[0]
                added = 0
                for event in events:
                    if event["run_id"] not in owned:
                        continue
                    if connection.execute(
                        "SELECT 1 FROM run_event_feed WHERE owner_id = ? AND event_key = ?",
                        (owner, event["event_key"]),
                    ).fetchone():
                        continue
                    last += 1
                    added += 1
                    connection.execute(
                        "INSERT INTO run_event_feed VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (owner, last, event["event_key"], event["run_id"], event["kind"],
                         event.get("occurred_at"), now,
                         json.dumps(event["payload"], sort_keys=True, ensure_ascii=False)),
                    )
                return added

        return await asyncio.to_thread(operation)

    async def list_run_events(
        self,
        principal: TrustedPrincipal,
        *,
        after: int,
        limit: int,
        run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        owner = self._authenticated(principal)

        def operation() -> list[dict[str, Any]]:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT seq, event_key, run_id, kind, occurred_at, recorded_at, event_json "
                    "FROM run_event_feed WHERE owner_id = ? AND seq > ? "
                    "AND (? IS NULL OR run_id = ?) ORDER BY seq LIMIT ?",
                    (owner, after, run_id, run_id, limit),
                ).fetchall()
                return [
                    {"seq": r[0], "event_key": r[1], "run_id": r[2], "kind": r[3],
                     "occurred_at": r[4], "recorded_at": r[5], "payload": json.loads(r[6])}
                    for r in rows
                ]

        return await asyncio.to_thread(operation)

    # -- P1-005B owner feedback memory ---------------------------------------------------
    async def create_feedback(
        self, record: FeedbackRecord, principal: TrustedPrincipal
    ) -> FeedbackRecord:
        owner = self._authenticated(principal)
        record = FeedbackRecord.model_validate(record.model_dump())

        def operation() -> FeedbackRecord:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO feedback_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (record.feedback_id, owner, record.category.value, record.state,
                     record.revision, record.scope.domain_id, record.scope.target_audience,
                     record.scope.target_channel, record.model_dump_json(),
                     record.created_at.isoformat(), record.updated_at.isoformat()),
                )
                connection.execute(
                    "INSERT INTO feedback_item_revisions VALUES (?, ?, ?, ?)",
                    (record.feedback_id, record.revision, record.model_dump_json(),
                     record.updated_at.isoformat()),
                )
                return record

        return await asyncio.to_thread(operation)

    async def get_feedback(self, feedback_id: str, principal: TrustedPrincipal) -> FeedbackRecord:
        owner = self._authenticated(principal)

        def operation() -> FeedbackRecord:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT record_json FROM feedback_items WHERE feedback_id=? AND owner_id=?",
                    (feedback_id, owner),
                ).fetchone()
                if row is None:
                    raise ResourceNotFoundError("feedback")
                return FeedbackRecord.model_validate_json(row[0])

        return await asyncio.to_thread(operation)

    async def list_feedback(
        self,
        principal: TrustedPrincipal,
        *,
        domain_id: DomainId | None = None,
        state: str | None = None,
        category: str | None = None,
    ) -> list[FeedbackRecord]:
        owner = self._authenticated(principal)

        def operation() -> list[FeedbackRecord]:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT record_json FROM feedback_items WHERE owner_id=? "
                    "AND (? IS NULL OR domain_id IS NULL OR domain_id=?) "
                    "AND (? IS NULL OR state=?) AND (? IS NULL OR category=?) "
                    "ORDER BY created_at, feedback_id",
                    (owner, domain_id, domain_id, state, state, category, category),
                ).fetchall()
                return [FeedbackRecord.model_validate_json(row[0]) for row in rows]

        return await asyncio.to_thread(operation)

    async def feedback_revisions(
        self, feedback_id: str, principal: TrustedPrincipal
    ) -> list[FeedbackRecord]:
        await self.get_feedback(feedback_id, principal)

        def operation() -> list[FeedbackRecord]:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT record_json FROM feedback_item_revisions WHERE feedback_id=? "
                    "ORDER BY revision",
                    (feedback_id,),
                ).fetchall()
                return [FeedbackRecord.model_validate_json(row[0]) for row in rows]

        return await asyncio.to_thread(operation)

    async def replace_feedback(
        self, record: FeedbackRecord, principal: TrustedPrincipal, *, expected_revision: int
    ) -> FeedbackRecord:
        """CAS: store revision N+1 only if N is current and the item is still active."""
        owner = self._authenticated(principal)
        record = FeedbackRecord.model_validate(record.model_dump())

        def operation() -> FeedbackRecord:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    "SELECT revision, state FROM feedback_items WHERE feedback_id=? AND owner_id=?",
                    (record.feedback_id, owner),
                ).fetchone()
                if current is None:
                    raise ResourceNotFoundError("feedback")
                if (
                    current[0] != expected_revision
                    or record.revision != expected_revision + 1
                    or current[1] != "active"
                ):
                    raise RfaError("invalid_state_transition", "피드백 revision이 변경되었습니다.")
                connection.execute(
                    "UPDATE feedback_items SET state=?, revision=?, record_json=?, updated_at=? "
                    "WHERE feedback_id=?",
                    (record.state, record.revision, record.model_dump_json(),
                     record.updated_at.isoformat(), record.feedback_id),
                )
                connection.execute(
                    "INSERT INTO feedback_item_revisions VALUES (?, ?, ?, ?)",
                    (record.feedback_id, record.revision, record.model_dump_json(),
                     record.updated_at.isoformat()),
                )
                return record

        return await asyncio.to_thread(operation)

    async def apply_feedback(
        self,
        principal: TrustedPrincipal,
        *,
        effect: str,
        domain_id: DomainId,
        target_audience: str,
        target_channel: str,
        run_id: str | None,
        keep: Any = None,
    ) -> list[FeedbackRecord]:
        """Select in-scope ACTIVE items and record their application in ONE transaction.

        Only style guidance and disclosure markers are ever applicable; factual corrections
        and policy proposals are not selectable here. A committed revocation is never read.
        """
        owner = self._authenticated(principal)
        category = {
            "style_guidance": "style_preference",
            "withhold_markers": "personal_disclosure_preference",
        }[effect]
        now = datetime.now(UTC)

        def operation() -> list[FeedbackRecord]:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    "SELECT record_json FROM feedback_items WHERE owner_id=? AND state='active' "
                    "AND category=? AND (domain_id IS NULL OR domain_id=?) "
                    "AND (target_audience IS NULL OR target_audience=?) "
                    "AND (target_channel IS NULL OR target_channel=?) "
                    "ORDER BY created_at, feedback_id",
                    (owner, category, domain_id, target_audience, target_channel),
                ).fetchall()
                records = [FeedbackRecord.model_validate_json(row[0]) for row in rows]
                if keep is not None:
                    records = [item for item in records if keep(item)]
                if run_id is not None:
                    for item in records:
                        application = FeedbackApplication(
                            feedback_id=item.feedback_id, feedback_revision=item.revision,
                            run_id=run_id, effect=effect, domain_id=domain_id,
                            target_audience=target_audience, target_channel=target_channel,
                            applied_at=now,
                        )
                        # Replay of the same generation step records nothing new.
                        connection.execute(
                            "INSERT OR IGNORE INTO feedback_applications VALUES "
                            "(?, ?, ?, ?, ?, ?, ?)",
                            (item.feedback_id, item.revision, owner, run_id, effect,
                             application.model_dump_json(), now.isoformat()),
                        )
                return records

        return await asyncio.to_thread(operation)

    async def feedback_applications(
        self,
        principal: TrustedPrincipal,
        *,
        run_id: str | None = None,
        feedback_id: str | None = None,
    ) -> list[FeedbackApplication]:
        owner = self._authenticated(principal)

        def operation() -> list[FeedbackApplication]:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT application_json FROM feedback_applications WHERE owner_id=? "
                    "AND (? IS NULL OR run_id=?) AND (? IS NULL OR feedback_id=?) "
                    "ORDER BY applied_at, feedback_id, effect",
                    (owner, run_id, run_id, feedback_id, feedback_id),
                ).fetchall()
                return [FeedbackApplication.model_validate_json(row[0]) for row in rows]

        return await asyncio.to_thread(operation)

    async def run_team_binding(
        self, run_id: str, principal: TrustedPrincipal
    ) -> tuple[str, str] | None:
        owner = self._authenticated(principal)

        def operation():
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT task_id, team_id FROM run_team_bindings "
                    "WHERE run_id = ? AND owner_id = ?",
                    (run_id, owner),
                ).fetchone()
                return (row[0], row[1]) if row else None

        return await asyncio.to_thread(operation)

    async def begin_role_execution(
        self,
        run_id: str,
        principal: TrustedPrincipal,
        *,
        execution_key: str,
        role: str,
        agent_id: str,
    ) -> tuple[str, RoleOutcome | None]:
        """Return ('started', None) for a new receipt, or the durable prior state."""
        owner = self._authenticated(principal)
        now = datetime.now(UTC).isoformat()

        def operation():
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if connection.execute(
                    "SELECT 1 FROM run_team_bindings WHERE run_id = ? AND owner_id = ?",
                    (run_id, owner),
                ).fetchone() is None:
                    raise ResourceNotFoundError("run")
                row = connection.execute(
                    "SELECT execution_key, agent_id, status, outcome_json FROM role_executions "
                    "WHERE run_id = ? AND role = ?",
                    (run_id, role),
                ).fetchone()
                if row is None:
                    # Cancel/revocation blocks every later role before any receipt exists.
                    self._ensure_open(connection, run_id)
                    connection.execute(
                        "INSERT INTO role_executions VALUES (?, ?, ?, ?, 'running', NULL, ?, NULL)",
                        (execution_key, run_id, role, agent_id, now),
                    )
                    self._ledger_begin(
                        connection,
                        owner,
                        operation_key="role_execution:" + execution_key,
                        kind="role_execution",
                        run_id=run_id,
                        fingerprint=_canonical_fingerprint(
                            {"run_id": run_id, "role": role, "agent_id": agent_id}
                        ),
                        state="inflight",
                        result_ref="role_execution:" + execution_key,
                    )
                    return "started", None
                if row["execution_key"] != execution_key or row["agent_id"] != agent_id:
                    raise RfaError("team_binding_conflict", "역할 실행 key가 일치하지 않습니다.")
                outcome = (
                    RoleOutcome.model_validate_json(row["outcome_json"])
                    if row["outcome_json"]
                    else None
                )
                return row["status"], outcome

        return await asyncio.to_thread(operation)

    async def finish_role_execution(
        self, run_id: str, principal: TrustedPrincipal, outcome: RoleOutcome
    ) -> None:
        owner = self._authenticated(principal)
        outcome = RoleOutcome.model_validate(outcome.model_dump())
        now = datetime.now(UTC).isoformat()

        def operation():
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                updated = connection.execute(
                    "UPDATE role_executions SET status = ?, outcome_json = ?, finished_at = ? "
                    "WHERE execution_key = ? AND run_id = ? AND role = ? AND status = 'running' "
                    "AND run_id IN (SELECT run_id FROM run_team_bindings WHERE owner_id = ?)",
                    (
                        outcome.status,
                        outcome.model_dump_json(),
                        now,
                        outcome.execution_key,
                        run_id,
                        outcome.role,
                        owner,
                    ),
                ).rowcount
                if updated != 1:
                    # Terminal receipts (including restart-unknown) are never overwritten.
                    raise RfaError("invalid_state_transition", "역할 실행 상태를 바꿀 수 없습니다.")
                self._ledger_advance(
                    connection,
                    owner,
                    "role_execution:" + outcome.execution_key,
                    state="outcome_unknown" if outcome.status == "unknown" else "completed",
                    outcome=outcome.status,
                    missing_ok=True,
                )

        await asyncio.to_thread(operation)

    async def save_team_result(
        self, run_id: str, principal: TrustedPrincipal, result: TeamRunResult
    ) -> None:
        owner = self._authenticated(principal)
        result = TeamRunResult.model_validate(result.model_dump())
        now = datetime.now(UTC).isoformat()

        def operation():
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if connection.execute(
                    "SELECT 1 FROM run_team_bindings WHERE run_id = ? AND owner_id = ? "
                    "AND task_id = ? AND team_id = ?",
                    (run_id, owner, result.task_id, result.team_id),
                ).fetchone() is None:
                    raise ResourceNotFoundError("run")
                connection.execute(
                    "INSERT INTO team_run_results VALUES (?, ?, ?) ON CONFLICT(run_id) "
                    "DO UPDATE SET result_json = excluded.result_json",
                    (run_id, result.model_dump_json(), now),
                )

        await asyncio.to_thread(operation)

    async def get_team_result(
        self, run_id: str, principal: TrustedPrincipal
    ) -> TeamRunResult | None:
        owner = self._authenticated(principal)

        def operation():
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT r.result_json FROM team_run_results r JOIN run_team_bindings b "
                    "ON r.run_id = b.run_id WHERE r.run_id = ? AND b.owner_id = ?",
                    (run_id, owner),
                ).fetchone()
                return TeamRunResult.model_validate_json(row[0]) if row else None

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
                    # The prepare call follows this reservation: durable intent first.
                    self._ledger_begin(
                        connection,
                        owner,
                        operation_key="team:" + record.operation_key,
                        kind="team_prepare",
                        run_id=None,
                        fingerprint=_canonical_fingerprint(
                            {"goal": task.goal, "spec": _team_definition(spec)}
                        ),
                        result_ref=f"team:{task.task_id}@{record.generation}",
                    )
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
                self._ledger_advance(
                    connection,
                    owner,
                    "team:" + current.operation_key,
                    state="outcome_unknown"
                    if reason in {"outcome_unknown", "invalid_contract"}
                    else "completed",
                    outcome=reason,
                    missing_ok=True,
                )
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
                self._ledger_begin(
                    connection,
                    owner,
                    operation_key="team:" + record.operation_key,
                    kind="team_cleanup",
                    run_id=None,
                    fingerprint=_canonical_fingerprint(
                        {"cleanup": record.team.spec.team_id, "task_id": task_id}
                    ),
                    result_ref=f"team:{task_id}@{record.generation}",
                )
                return record, True

        return await asyncio.to_thread(operation)

    async def create_owned_run(
        self,
        request: WorkRequest,
        principal: TrustedPrincipal,
        *,
        session_id: str | None,
        task_id: str | None = None,
        retry_of: str | None = None,
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
                if retry_of is not None:
                    # P0-021: only an explicit owner retry links a NEW run to a terminal one.
                    source = connection.execute(
                        "SELECT status FROM runs WHERE run_id = ? AND owner_id = ?",
                        (retry_of, owner),
                    ).fetchone()
                    if source is None:
                        raise ResourceNotFoundError("run")
                    if source[0] not in {"failed", "cancelled", "outcome_unknown"}:
                        raise RfaError(
                            "invalid_state_transition",
                            "종료된 실패·취소 실행만 재시도할 수 있습니다.",
                        )
                    if connection.execute(
                        "SELECT 1 FROM run_lineage WHERE retry_of = ?", (retry_of,)
                    ).fetchone():
                        raise RfaError("retry_exists", "이미 재시도된 실행입니다.")
                if connection.execute(
                    "SELECT 1 FROM runs WHERE session_id = ? AND status IN "
                    "('created', 'running', 'waiting_approval', 'outcome_unknown') "
                    "AND run_id != ?",
                    (selected_session, retry_of or ""),
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
                if retry_of is not None:
                    connection.execute(
                        "INSERT INTO run_lineage VALUES (?, ?, ?, ?)",
                        (request.run_id, owner, retry_of, now),
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
        # P0-020: link the team only after the durable Run<->Task<->Team binding exists.
        # Earlier observations keep null; stored records are never rewritten.
        binding = await self.run_team_binding(run_id, principal)
        if binding is not None and "task_id" in fields:
            fields["team_id"] = await self.observation_alias(run_id, principal, "team_id")

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

    @staticmethod
    def _index_legacy_documents(connection, *, trusted_fixture: bool = False) -> None:
        sources = connection.execute(
            "SELECT DISTINCT source_id FROM kb_documents WHERE source_id NOT IN "
            "(SELECT source_id FROM kb_sources)"
        ).fetchall()
        for (source_id,) in sources:
            rows = connection.execute(
                "SELECT source_revision, document_json FROM kb_documents WHERE source_id=?",
                (source_id,),
            ).fetchall()
            doc = KnowledgeDocument.model_validate_json(rows[0][1])
            ambiguous = len(rows) != 1 or (
                doc.owner_id is None and doc.audience != Audience.PUBLIC and not trusted_fixture
            )
            connection.execute(
                "INSERT INTO kb_sources VALUES (?, ?, ?, ?, 'local', ?, ?, ?)",
                (
                    source_id,
                    doc.owner_id,
                    doc.domain_id.value,
                    "fixture" if trusted_fixture else "legacy",
                    source_id,
                    None if ambiguous else rows[0][0],
                    int(ambiguous),
                ),
            )

    @staticmethod
    def _knowledge_principal(principal: TrustedPrincipal) -> TrustedPrincipal:
        try:
            value = TrustedPrincipal.model_validate(principal.model_dump(), strict=True)
            if value.authenticated is not True or not value.user_id:
                raise ValueError("authentication required")
            return value
        except (ValueError, AttributeError):
            raise RfaError("authentication_required", "자료 접근에 인증이 필요합니다.") from None

    def _knowledge_owner(self, connection, source_id, principal):
        row = connection.execute(
            "SELECT * FROM kb_sources WHERE source_id=? AND owner_id=? AND restricted=0 "
            "AND provider IN ('note','github_issue','confluence')",
            (source_id, principal.user_id),
        ).fetchone()
        if row is None:
            # Legacy rows are read-only, never adopted by the current installation owner.
            raise ResourceNotFoundError("source")
        self._project_guard(connection, source_id, row["current_revision"], principal)
        self._stored_parent_guard(connection, source_id, row["current_revision"], principal)
        return row

    def _project_guard(self, connection, source_id, revision, principal):
        row = connection.execute(
            "SELECT json_extract(document_json,'$.project_id'), "
            "json_extract(document_json,'$.company_id') FROM kb_documents "
            "WHERE source_id=? AND source_revision=?", (source_id, revision),
        ).fetchone()
        if row is None or not project_allowed(*row, principal, self.project_resolver):
            raise ResourceNotFoundError("source")

    @staticmethod
    def _knowledge_revision(connection, source_id, revision) -> KnowledgeRevision:
        row = connection.execute(
            "SELECT d.document_json, r.metadata_json FROM kb_source_revisions r "
            "JOIN kb_documents d USING(source_id, source_revision) "
            "WHERE r.source_id=? AND r.source_revision=?",
            (source_id, revision),
        ).fetchone()
        if row is None:
            raise ResourceNotFoundError("source")
        return KnowledgeRevision.model_validate(
            json.loads(row[1]) | {"document": json.loads(row[0])}
        )

    def _stored_parent_guard(self, connection, source_id, revision, principal):
        row = connection.execute(
            "SELECT d.domain_id,json_extract(d.document_json,'$.policy_version'), "
            "c.parent_refs,c.epistemic_state FROM kb_documents d "
            "JOIN kb_revision_context c USING(source_id,source_revision) "
            "WHERE d.source_id=? AND d.source_revision=?", (source_id,revision),
        ).fetchone()
        if row is None:
            raise ResourceNotFoundError("source")
        try:
            parents=tuple(SourceRevisionRef.model_validate(p) for p in json.loads(row[2]))
            if row[3] != "cited" and not parents:
                raise ValueError("missing lineage")
            for parent in parents:
                self._current_closure(connection,parent.source_id,DomainId(row[0]),principal,
                    tuple(Audience),Audience.OWNER,row[1],expected=parent,path=(source_id,))
        except (RfaError,ValueError,TypeError):
            raise ResourceNotFoundError("source") from None

    @staticmethod
    def _store_knowledge_revision(connection, record, *, operation, fingerprint) -> None:
        doc = record.document
        connection.execute(
            "INSERT INTO kb_documents VALUES (?, ?, ?, ?, ?)",
            (
                doc.source_id,
                doc.source_revision,
                doc.domain_id.value,
                doc.model_dump_json(),
                record.created_at.isoformat(),
            ),
        )
        connection.execute(
            "INSERT INTO kb_source_revisions VALUES (?, ?, ?, ?, ?, ?)",
            (
                doc.source_id,
                doc.source_revision,
                record.model_dump_json(exclude={"document"}),
                operation,
                record.provider_revision,
                fingerprint,
            ),
        )

    @staticmethod
    def _record_source_event(connection, record, operation: str, *, derived: bool) -> None:
        """P0-024 outbox row in the revision's own transaction: identifiers only."""
        document = record.document
        connection.execute(
            "INSERT OR IGNORE INTO source_revision_events (owner_id, domain_id, source_id, "
            "source_revision, operation, derived, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (getattr(document, "owner_id", None), document.domain_id.value,
             document.source_id, document.source_revision, operation, int(derived),
             record.created_at.isoformat()),
        )

    async def write_knowledge(
        self,
        request: KnowledgeWrite,
        principal: TrustedPrincipal,
        *,
        policy_version: str,
        source_id: str | None = None,
        _parents: tuple[SourceRevisionRef, ...] = (),
        _epistemic_state: str = "cited",
    ) -> KnowledgeRevision:
        principal = self._knowledge_principal(principal)
        request = KnowledgeWrite.model_validate_json(request.model_dump_json(), strict=True)
        acl = request.acl
        if not project_allowed(acl.project_id, acl.company_id, principal, self.project_resolver):
            raise RfaError("policy_denied", "현재 프로젝트 권한으로 저장할 수 없습니다.")
        if acl.audience in {Audience.COMPANY, Audience.BUSINESS_UNIT} and (
            not principal.company_id
            or acl.company_id != principal.company_id
            or not set(acl.memberships) <= principal.business_units
        ):
            raise RfaError("policy_denied", "현재 조직 권한으로 공유할 수 없습니다.")
        payload = request.model_dump(mode="json", exclude={"expected_revision"})
        if acl.project_id is None:
            payload["acl"].pop("project_id", None)  # Only the new None field; preserve old nulls.
        if _parents:
            payload["derived"] = {"parents": [r.model_dump(mode="json") for r in _parents],
                                  "epistemic_state": _epistemic_state}
        fingerprint = _canonical_fingerprint(payload)

        def operation():
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                origin = request.provenance
                existing = connection.execute(
                    "SELECT * FROM kb_sources WHERE owner_id=? AND domain_id=? AND provider=? "
                    "AND namespace=? AND external_id=?",
                    (
                        principal.user_id,
                        request.domain_id.value,
                        origin.provider,
                        origin.namespace,
                        origin.external_id,
                    ),
                ).fetchone()
                if source_id is not None:
                    owned = self._knowledge_owner(connection, source_id, principal)
                    if existing is None or existing["source_id"] != owned["source_id"]:
                        raise RfaError(
                            "idempotency_conflict", "자료의 출처/도메인은 변경할 수 없습니다."
                        )
                if existing is not None:
                    identity = existing["source_id"]
                    self._knowledge_owner(connection, identity, principal)
                    replay = connection.execute(
                        "SELECT source_revision, fingerprint FROM kb_source_revisions "
                        "WHERE source_id=? AND operation='write' AND provider_revision=?",
                        (identity, request.provider_revision),
                    ).fetchone()
                    if replay:
                        if replay[1] != fingerprint:
                            raise RfaError(
                                "idempotency_conflict", "동일 출처 revision의 내용이 다릅니다."
                            )
                        # Historical receipt only. Never reset current head on replay.
                        self._project_guard(connection, identity, replay[0], principal)
                        self._stored_parent_guard(connection, identity, replay[0], principal)
                        return self._knowledge_revision(connection, identity, replay[0])
                    if request.expected_revision != existing["current_revision"]:
                        raise RfaError("idempotency_conflict", "최신 자료 revision이 필요합니다.")
                    previous = self._knowledge_revision(
                        connection, identity, existing["current_revision"]
                    )
                    if previous.deleted:
                        raise ResourceNotFoundError("source")
                    number = previous.revision_number + 1
                else:
                    if request.expected_revision is not None or source_id is not None:
                        raise ResourceNotFoundError("source")
                    identity, number = new_id("source"), 1
                    connection.execute(
                        "INSERT INTO kb_sources VALUES (?, ?, ?, ?, ?, ?, NULL, 0)",
                        (
                            identity,
                            principal.user_id,
                            request.domain_id.value,
                            origin.provider,
                            origin.namespace,
                            origin.external_id,
                        ),
                    )
                revision = new_id("revision")
                for parent in _parents:
                    self._current_closure(connection, parent.source_id, request.domain_id,
                        principal, tuple(Audience), Audience.OWNER, policy_version, expected=parent)
                record = KnowledgeRevision(
                    document=KnowledgeDocumentV11(
                        source_id=identity,
                        source_revision=revision,
                        domain_id=request.domain_id,
                        title=request.title,
                        content=request.content,
                        location={"uri": f"rfa://sources/{identity}"},
                        audience=acl.audience,
                        classification=acl.audience.value,
                        policy_version=policy_version,
                        required_memberships=acl.memberships,
                        owner_id=principal.user_id,
                        company_id=acl.company_id,
                        business_unit=acl.memberships[0] if len(acl.memberships) == 1 else None,
                        synthetic=request.synthetic,
                        project_id=acl.project_id,
                    ),
                    provenance=origin,
                    provider_revision=request.provider_revision,
                    revision_number=number,
                    acl_revision=revision,
                    deleted=False,
                    created_at=datetime.now(UTC),
                    source_modified_at=request.source_modified_at,
                )
                self._store_knowledge_revision(
                    connection, record, operation="write", fingerprint=fingerprint
                )
                self._insert_context(connection, record.document, _parents, _epistemic_state)
                self._record_source_event(connection, record, "write", derived=bool(_parents))
                updated = connection.execute(
                    "UPDATE kb_sources SET current_revision=? "
                    "WHERE source_id=? AND current_revision IS ?",
                    (revision, identity, request.expected_revision),
                ).rowcount
                if updated != 1:
                    raise RfaError("idempotency_conflict", "자료 revision이 변경되었습니다.")
                return record

        return await asyncio.to_thread(operation)

    async def write_derived_knowledge(self, request, principal, *, parents, policy_version,
                                      epistemic_state="inferred"):
        parents = tuple(SourceRevisionRef.model_validate_json(p.model_dump_json(), strict=True)
                        for p in parents)
        if not parents or len(parents) > 32 or len({(p.source_id,p.source_revision) for p in parents}) != len(parents):
            raise RfaError("policy_denied", "전체 부모 근거가 필요합니다.")
        # "cited" only after the Supervisor gate verified a verbatim parent quote;
        # "simulated" marks synthetic experiment numbers (P1-004A).
        if epistemic_state not in {"cited", "inferred", "simulated", "tentative", "conflicting"}:
            raise RfaError("policy_denied", "파생 자료 상태가 올바르지 않습니다.")
        return await self.write_knowledge(request, principal, policy_version=policy_version,
                                         _parents=parents, _epistemic_state=epistemic_state)

    async def delete_knowledge(
        self,
        source_id: str,
        request: KnowledgeDelete,
        principal: TrustedPrincipal,
    ) -> KnowledgeRevision:
        principal = self._knowledge_principal(principal)
        request = KnowledgeDelete.model_validate_json(request.model_dump_json(), strict=True)
        fingerprint = _canonical_fingerprint(request.model_dump(mode="json"))

        def operation():
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                source = self._knowledge_owner(connection, source_id, principal)
                replay = connection.execute(
                    "SELECT source_revision, fingerprint FROM kb_source_revisions "
                    "WHERE source_id=? AND operation='delete' AND provider_revision=?",
                    (source_id, request.mutation_id),
                ).fetchone()
                if replay:
                    if replay[1] != fingerprint:
                        raise RfaError("idempotency_conflict", "삭제 요청 내용이 다릅니다.")
                    return self._knowledge_revision(connection, source_id, replay[0])
                if source["current_revision"] != request.expected_revision:
                    raise RfaError("idempotency_conflict", "최신 자료 revision이 필요합니다.")
                old = self._knowledge_revision(connection, source_id, source["current_revision"])
                if old.deleted:
                    raise ResourceNotFoundError("source")
                revision = new_id("revision")
                record = KnowledgeRevision.model_validate(
                    old.model_dump()
                    | {
                        "document": old.document.model_dump() | {"source_revision": revision},
                        "provider_revision": request.mutation_id,
                        "acl_revision": revision,
                        "revision_number": old.revision_number + 1,
                        "deleted": True,
                        "created_at": datetime.now(UTC),
                    }
                )
                self._store_knowledge_revision(
                    connection, record, operation="delete", fingerprint=fingerprint
                )
                self._insert_context(connection, record.document)
                parent_refs = connection.execute(
                    "SELECT parent_refs FROM kb_revision_context WHERE source_id=? "
                    "AND source_revision=?",
                    (source_id, old.document.source_revision),
                ).fetchone()
                self._record_source_event(
                    connection, record, "delete",
                    derived=bool(parent_refs and parent_refs[0] != "[]"),
                )
                updated = connection.execute(
                    "UPDATE kb_sources SET current_revision=? "
                    "WHERE source_id=? AND current_revision=?",
                    (revision, source_id, request.expected_revision),
                ).rowcount
                if updated != 1:
                    raise RfaError("idempotency_conflict", "자료 revision이 변경되었습니다.")
                return record

        return await asyncio.to_thread(operation)

    async def get_knowledge(
        self,
        source_id: str,
        principal: TrustedPrincipal,
        *,
        revision: str | None = None,
    ) -> KnowledgeRevision:
        principal = self._knowledge_principal(principal)

        def operation():
            with self._connect() as connection:
                connection.execute("BEGIN")
                source = self._knowledge_owner(connection, source_id, principal)
                if revision is not None:
                    self._project_guard(connection, source_id, revision, principal)
                    self._stored_parent_guard(connection, source_id, revision, principal)
                record = self._knowledge_revision(
                    connection, source_id, revision or source["current_revision"]
                )
                if revision is None and record.deleted:
                    raise ResourceNotFoundError("source")
                return record

        return await asyncio.to_thread(operation)

    async def list_knowledge(
        self,
        principal: TrustedPrincipal,
        *,
        domain_id: DomainId | None = None,
    ) -> list[KnowledgeRevision]:
        principal = self._knowledge_principal(principal)

        def operation():
            with self._connect() as connection:
                connection.execute("BEGIN")
                rows = connection.execute(
                    "SELECT source_id, current_revision FROM kb_sources WHERE owner_id=? "
                    "AND restricted=0 AND provider IN ('note','github_issue','confluence') "
                    "AND (? IS NULL OR domain_id=?) ORDER BY source_id",
                    (
                        principal.user_id,
                        domain_id.value if domain_id else None,
                        domain_id.value if domain_id else None,
                    ),
                ).fetchall()
                records = []
                for row in rows:
                    try:
                        self._project_guard(connection, *row, principal)
                        self._stored_parent_guard(connection, *row, principal)
                    except ResourceNotFoundError:
                        continue
                    records.append(self._knowledge_revision(connection, *row))
                return [record for record in records if not record.deleted]

        return await asyncio.to_thread(operation)

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
                    self._index_legacy_documents(connection, trusted_fixture=True)
                    self._backfill_context(connection)
                connection.execute(
                    "INSERT INTO installation_seeds VALUES ('synthetic-fixtures-v1', ?)", (now,)
                )

        await asyncio.to_thread(operation)

    async def upsert_documents(self, documents: list[KnowledgeDocument]) -> None:
        def operation() -> None:
            now = datetime.now(UTC).isoformat()
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                for item in documents:
                    item = KnowledgeDocument.model_validate(item.model_dump())
                    old = connection.execute(
                        "SELECT document_json FROM kb_documents "
                        "WHERE source_id=? AND source_revision=?",
                        (item.source_id, item.source_revision),
                    ).fetchone()
                    if old:
                        if json.loads(old[0]) != item.model_dump(mode="json"):
                            raise RfaError(
                                "idempotency_conflict", "원문 revision은 변경할 수 없습니다."
                            )
                        continue
                    source = connection.execute(
                        "SELECT provider FROM kb_sources WHERE source_id=?",
                        (item.source_id,),
                    ).fetchone()
                    if source and source[0] not in {"legacy", "fixture"}:
                        raise RfaError(
                            "idempotency_conflict", "인증된 revision 저장 경로가 필요합니다."
                        )
                    connection.execute(
                        "INSERT INTO kb_documents VALUES (?, ?, ?, ?, ?)",
                        (
                            item.source_id,
                            item.source_revision,
                            item.domain_id.value,
                            item.model_dump_json(),
                            now,
                        ),
                    )
                    self._insert_context(connection, item)
                    if source:
                        # This legacy interface has no CAS/current-head assertion.
                        connection.execute(
                            "UPDATE kb_sources SET current_revision=NULL, restricted=1 "
                            "WHERE source_id=?",
                            (item.source_id,),
                        )
                self._index_legacy_documents(connection)

        await asyncio.to_thread(operation)

    @staticmethod
    def _insert_context(connection, document, parents=(), epistemic_state="cited"):
        connection.execute("INSERT INTO kb_revision_context VALUES (?,?,?,?,?,?)", (
            document.source_id, document.source_revision, sha256_text(document.content),
            len(document.content), epistemic_state,
            json.dumps([p.model_dump(mode="json") for p in parents], ensure_ascii=False),
        ))

    @staticmethod
    def _backfill_context(connection):
        # Migration/fixture system boundary only. Never silently repair missing
        # context on a subsequent read (which could erase missing lineage).
        rows = connection.execute("SELECT d.document_json FROM kb_documents d "
            "LEFT JOIN kb_revision_context c USING(source_id,source_revision) "
            "WHERE c.source_id IS NULL").fetchall()
        for row in rows:
            data = json.loads(row[0])
            cls = KnowledgeDocumentV11 if data.get("schema_version") == "1.1" else KnowledgeDocument
            SqliteWorkRepository._insert_context(connection, cls.model_validate(data))

    def _current_acl(self, connection, source_id, domain_id):
        row = connection.execute("""
            SELECT d.source_id,d.source_revision,
                json_extract(d.document_json,'$.audience') audience,
                json_extract(d.document_json,'$.owner_id') owner_id,
                json_extract(d.document_json,'$.company_id') company_id,
                json_extract(d.document_json,'$.project_id') project_id,
                json_extract(d.document_json,'$.required_memberships') memberships,
                json_extract(d.document_json,'$.policy_version') policy_version,
                c.content_hash,c.character_count,c.epistemic_state,c.parent_refs
            FROM kb_sources s JOIN kb_documents d ON s.source_id=d.source_id
                AND s.current_revision=d.source_revision
            JOIN kb_revision_context c USING(source_id,source_revision)
            LEFT JOIN kb_source_revisions r USING(source_id,source_revision)
            WHERE s.source_id=? AND s.domain_id=? AND s.restricted=0
                AND (r.metadata_json IS NULL OR json_extract(r.metadata_json,'$.deleted')=0)
            """, (source_id,domain_id.value)).fetchone()
        if row is None:
            raise RfaError("resume_review_required", "현재 근거를 확인할 수 없습니다.")
        data = dict(row)
        data["memberships"] = json.loads(data["memberships"] or "[]")
        return data

    def _current_closure(self, connection, source_id, domain_id, principal, audiences,
                         target, policy_version, *, expected=None, path=(), counter=None):
        counter = counter if counter is not None else [0]
        counter[0] += 1
        if len(path) >= 16 or counter[0] > 128 or source_id in path:
            raise RfaError("policy_denied", "근거 범위를 확인할 수 없습니다.")
        row = self._current_acl(connection, source_id, domain_id)
        if not permitted(row, principal, self.project_resolver, target, audiences, policy_version):
            raise RfaError("policy_denied", "현재 자료 권한이 충분하지 않습니다.")
        # ACL first; only now inspect source metadata. Never return an unapproved title/URI.
        detail = connection.execute(
            "SELECT json_extract(document_json,'$.title'), "
            "json_extract(document_json,'$.location') FROM kb_documents "
            "WHERE source_id=? AND source_revision=?", (source_id,row["source_revision"]),
        ).fetchone()
        reference = SourceRevisionRef(source_id=source_id, source_revision=row["source_revision"],
            location=json.loads(detail[1]), audience=row["audience"],
            content_hash=row["content_hash"], acl_revision=row["source_revision"],
            policy_version=row["policy_version"])
        if expected is not None:
            # Compare every supplied field; legacy1.0 ref has no policy/ACL fields.
            if any(getattr(reference,k) != getattr(expected,k) for k in
                   ("source_id","source_revision","location","audience","content_hash")):
                raise RfaError("resume_review_required", "근거가 변경되어 새 검토가 필요합니다.")
            if isinstance(expected, SourceRevisionRef) and reference != expected:
                raise RfaError("resume_review_required", "현재 근거 판정이 필요합니다.")
        try:
            parents = tuple(SourceRevisionRef.model_validate(p) for p in json.loads(row["parent_refs"]))
        except (ValueError, TypeError):
            raise RfaError("policy_denied", "근거 범위를 확인할 수 없습니다.") from None
        if row["epistemic_state"] != "cited" and not parents:
            raise RfaError("policy_denied", "파생 근거가 누락되었습니다.")
        # No source-only visited set: a diamond must validate BOTH referenced revisions.
        for parent in parents:
            self._current_closure(connection,parent.source_id,domain_id,principal,audiences,
                target,policy_version,expected=parent,path=(*path,source_id),counter=counter)
        return SourceMetadata(reference=reference,title=detail[0],
            character_count=row["character_count"],epistemic_state=row["epistemic_state"],parents=parents,
            owner_id=row["owner_id"],company_id=row["company_id"],
            required_memberships=tuple(row["memberships"]),project_id=row["project_id"])

    @staticmethod
    def _read_source_body(connection, metadata):
        reference = metadata.reference
        row = connection.execute("SELECT json_extract(document_json,'$.content') "
            "FROM kb_documents WHERE source_id=? AND source_revision=?",
            (reference.source_id,reference.source_revision)).fetchone()
        if row is None or sha256_text(row[0]) != reference.content_hash:
            raise RfaError("resume_review_required", "현재 근거 무결성을 확인할 수 없습니다.")
        return row[0]

    async def authorized_metadata(self, domain_id, principal, *, audiences=tuple(Audience),
                                  target=Audience.OWNER, endpoint="local-preview",
                                  policy_version="local-v1", query=None, limit=100):
        principal = fresh_principal(principal)
        if endpoint not in LOCAL_ENDPOINTS:
            raise RfaError("policy_denied", "자료 전송 경로를 지원하지 않습니다.")
        def operation():
            with self._connect() as connection:
                connection.execute("BEGIN")
                candidates = connection.execute("SELECT source_id FROM kb_sources "
                    "WHERE domain_id=? AND restricted=0 "
                    "ORDER BY provider,namespace,external_id,source_id",
                    (domain_id.value,)).fetchall()
                allowed = []
                for row in candidates:
                    try:
                        meta = self._current_closure(connection,row[0],domain_id,principal,
                                                     audiences,target,policy_version)
                    except RfaError:
                        continue
                    allowed.append(meta)
                if query is None:
                    return allowed[:limit]
                terms = lexical_terms(query)
                if not terms or not allowed:
                    return []
                # Rank only authorized IDs INSIDE SQLite. Application receives no
                # candidate body: only per-term counts and lengths leave the query, and
                # only the selected source reader accesses full content.
                # Count occurrences only where instr() finds the term (most docs do not).
                counts = ", ".join(
                    f"CASE WHEN instr({col}, ?) > 0 THEN "
                    f"(length({col}) - length(replace({col}, ?, ''))) / ? ELSE 0 END"
                    for col in ("b" if boundary else "t" for _, boundary in terms))
                arguments = []
                for term, boundary in terms:
                    needle = f" {term} " if boundary else term
                    arguments += [needle, needle, len(needle)]
                placeholders = ",".join("?" for _ in allowed)
                # The token-boundary text is only built when a short token needs it.
                boundary_text = (
                    "' ' || " + _TOKEN_BREAKS + " || ' '" if any(b for _, b in terms) else "''"
                )
                rows = connection.execute(
                    "WITH docs AS (SELECT d.source_id AS source_id, "
                    "s.provider AS provider, s.namespace AS namespace, "
                    "s.external_id AS external_id, "
                    "lower(coalesce(json_extract(d.document_json,'$.title'),'') || ' ' || "
                    "coalesce(json_extract(d.document_json,'$.content'),'')) AS t "
                    "FROM kb_documents d JOIN kb_sources s ON s.source_id=d.source_id "
                    "AND s.current_revision=d.source_revision WHERE d.source_id IN ("
                    + placeholders + ")), "
                    "bounded AS (SELECT source_id, provider, namespace, external_id, t, "
                    + boundary_text + " AS b FROM docs) "
                    "SELECT source_id, provider, namespace, external_id, length(t), "
                    + counts + " FROM bounded",
                    (*(m.reference.source_id for m in allowed), *arguments)).fetchall()
                scores = bm25_scores([(r[0], r[4], *r[5:]) for r in rows], len(terms))
                keys = {r[0]: (r[1], r[2], r[3], r[0]) for r in rows}
                by_id = {m.reference.source_id:m for m in allowed}
                # Stable tie-break: provider, namespace, external_id (never a random id).
                ranked = sorted(scores, key=lambda sid: (-scores[sid], *keys[sid]))
                return [by_id[sid] for sid in ranked[:limit]]
        return await asyncio.to_thread(operation)

    async def read_sources(self, domain_id, principal, references, *, audiences=tuple(Audience),
                           target=Audience.OWNER, endpoint="local-preview", policy_version="local-v1",
                           metadata_only=False):
        principal = fresh_principal(principal)
        if endpoint not in LOCAL_ENDPOINTS:
            raise RfaError("policy_denied", "자료 전송 경로를 지원하지 않습니다.")
        def operation():
            with self._connect() as connection:
                connection.execute("BEGIN")
                result = []
                for ref in references:
                    meta = self._current_closure(connection,ref.source_id,domain_id,principal,
                        audiences,target,policy_version,expected=ref)
                    if metadata_only:
                        result.append(meta)
                    else:
                        result.append(SourceRead(metadata=meta,content=self._read_source_body(connection,meta)))
                return result
        return await asyncio.to_thread(operation)

    async def list_documents(self, domain_id: str) -> list[KnowledgeDocument]:
        def operation() -> list[KnowledgeDocument]:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT d.document_json FROM kb_documents d
                    JOIN kb_sources s ON s.source_id=d.source_id
                        AND s.current_revision=d.source_revision
                    LEFT JOIN kb_source_revisions r ON r.source_id=d.source_id
                        AND r.source_revision=d.source_revision
                    WHERE s.domain_id = ? AND s.restricted=0
                    AND (r.metadata_json IS NULL OR json_extract(r.metadata_json, '$.deleted')=0)
                    ORDER BY d.source_id
                    """,
                    (domain_id,),
                ).fetchall()
                return [
                    (
                        KnowledgeDocumentV11
                        if json.loads(row[0]).get("schema_version") == "1.1"
                        else KnowledgeDocument
                    ).model_validate_json(row[0])
                    for row in rows
                ]

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
                and request.resource_company_id == principal.company_id
            )
        if audience == Audience.BUSINESS_UNIT:
            return bool(
                principal.authenticated
                and principal.business_units
                and request.resource_company_id
                and request.resource_company_id == principal.company_id
                and request.resource_business_unit in principal.business_units
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
        # Keyed by TaskRequest.run_id (a role execution key for team roles), so several
        # roles of one product Run never overwrite each other's status.
        self._running: dict[str, asyncio.Task[TaskResult]] = {}
        self._running_requests: dict[str, TaskRequest] = {}
        self._cancelled: dict[str, TaskResult] = {}
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
                self._running[request.run_id] = task
                self._running_requests[request.run_id] = request

        try:
            result = await asyncio.shield(task)
        except BaseException as exc:
            if task.done():
                async with self._lock:
                    pending = self._inflight.get(request.idempotency_key)
                    if pending is not None and pending[1] is task:
                        self._inflight.pop(request.idempotency_key, None)
                    if self._running.get(request.run_id) is task:
                        self._running.pop(request.run_id, None)
                    cancelled = self._cancelled.get(request.run_id)
                if (
                    isinstance(exc, asyncio.CancelledError)
                    and task.cancelled()
                    and cancelled is not None
                ):
                    # The inner role was cancelled through RuntimePort.cancel; the
                    # caller itself was not cancelled, so return the cancel receipt.
                    current = asyncio.current_task()
                    if current is None or not current.cancelling():
                        return cancelled
            raise

        async with self._lock:
            self._results[request.run_id] = result
            self._idempotency[request.idempotency_key] = (fingerprint, result)
            pending = self._inflight.get(request.idempotency_key)
            if pending is not None and pending[1] is task:
                self._inflight.pop(request.idempotency_key, None)
            if self._running.get(request.run_id) is task:
                self._running.pop(request.run_id, None)
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
        return self._cancelled.get(run_id) or self._results.get(run_id)

    async def cancel(self, run_id: str) -> TaskResult:
        """Cancel an in-flight local task. Already-finished effects are not undone."""
        async with self._lock:
            task = self._running.get(run_id)
            if task is None or task.done():
                if run_id in self._results or run_id in self._cancelled:
                    raise RfaError("invalid_state_transition", "이미 종료된 실행입니다.")
                raise ResourceNotFoundError("runtime task")
            request = self._running_requests[run_id]
            key = next(
                (k for k, (_, t) in self._inflight.items() if t is task), None
            )
            receipt = TaskResult(
                request_id=request.request_id,
                trace_id=request.trace_id,
                run_id=run_id,
                agent_id=request.agent_id,
                domain_id=request.domain_id,
                status=ResultStatus.FAILED,
                error=StructuredError(
                    code="cancelled",
                    message="실행이 취소되었습니다.",
                    retryable=False,
                    request_id=request.request_id,
                    trace_id=request.trace_id,
                    run_id=run_id,
                ),
                simulated=False,
                adapter=self.adapter_name,
            )
            self._cancelled[run_id] = receipt
            task.cancel()
        try:
            await asyncio.wait({task}, timeout=5)
        finally:
            async with self._lock:
                if key is not None:
                    pending = self._inflight.get(key)
                    if pending is not None and pending[1] is task:
                        self._inflight.pop(key, None)
        return receipt


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


class LocalAnalysisTools:
    """Allowlisted, side-effect-free READ computations over caller-supplied synthetic text.

    Not an MCP server, network tool or experiment runner: it parses/compares numbers only.
    """

    adapter_name = "local-analysis-tools"
    simulated = False
    TOOLS = frozenset({"benchmark_log_parse", "metric_compare"})
    _LATENCY = re.compile(r"(?:지연|latency)[^0-9]{0,20}(\d+(?:\.\d+)?)\s*ms", re.IGNORECASE)
    _ANY_MS = re.compile(r"(\d+(?:\.\d+)?)\s*ms", re.IGNORECASE)
    _ACCURACY = re.compile(r"(?:정확도|accuracy)[^0-9]{0,20}(\d+(?:\.\d+)?)\s*%", re.IGNORECASE)
    _ENVIRONMENT = re.compile(r"(?:환경|environment|env)[\s:=]*([A-Za-z0-9_.-]{1,40})", re.IGNORECASE)

    def __init__(self) -> None:
        self._idempotency: dict[str, tuple[str, ToolResult]] = {}
        self._lock = asyncio.Lock()

    def _compute(self, request: ToolRequest) -> dict[str, Any]:
        if request.tool_name == "benchmark_log_parse":
            text = request.arguments.get("text")
            if not isinstance(text, str) or len(text) > 20000:
                raise ValueError("text required")
            latency = self._LATENCY.search(text) or self._ANY_MS.search(text)
            accuracy = self._ACCURACY.search(text)
            environment = self._ENVIRONMENT.search(text)
            return {
                "latency_ms": float(latency.group(1)) if latency else None,
                "accuracy_pct": float(accuracy.group(1)) if accuracy else None,
                "environment": environment.group(1) if environment else None,
            }
        baseline, candidate = request.arguments.get("baseline"), request.arguments.get("candidate")
        if not isinstance(baseline, dict) or not isinstance(candidate, dict):
            raise ValueError("baseline/candidate required")
        output: dict[str, Any] = {"latency_change_pct": None, "accuracy_delta_pp": None}
        base_latency, new_latency = baseline.get("latency_ms"), candidate.get("latency_ms")
        if isinstance(base_latency, (int, float)) and isinstance(new_latency, (int, float)) \
                and base_latency > 0:
            output["latency_change_pct"] = round((new_latency - base_latency) / base_latency * 100, 1)
        base_acc, new_acc = baseline.get("accuracy_pct"), candidate.get("accuracy_pct")
        if isinstance(base_acc, (int, float)) and isinstance(new_acc, (int, float)):
            output["accuracy_delta_pp"] = round(new_acc - base_acc, 2)
        output["same_environment"] = (
            baseline.get("environment") is not None
            and baseline.get("environment") == candidate.get("environment")
        )
        return output

    async def execute(self, request: ToolRequest) -> ToolResult:
        request = ToolRequest.model_validate_json(request.model_dump_json())
        fingerprint = _canonical_fingerprint(request.model_dump(mode="json"))
        async with self._lock:
            cached = self._idempotency.get(request.idempotency_key)
            if cached is not None:
                if cached[0] != fingerprint:
                    raise RfaError(
                        "idempotency_conflict", "같은 idempotency key로 다른 요청입니다."
                    )
                return cached[1]
            error_code = None
            output: dict[str, Any] = {}
            if request.effect != ToolEffect.READ or request.tool_name not in self.TOOLS:
                status, error_code = ResultStatus.DENIED, "tool_not_allowed"
            else:
                try:
                    output, status = self._compute(request), ResultStatus.SUCCEEDED
                except (ValueError, TypeError):
                    status, error_code = ResultStatus.FAILED, "invalid_tool_arguments"
            result = ToolResult(
                request_id=request.request_id,
                trace_id=request.trace_id,
                run_id=request.run_id,
                agent_id=request.agent_id,
                domain_id=request.domain_id,
                idempotency_key=request.idempotency_key,
                status=status,
                output=output,
                error=StructuredError(
                    code=error_code,
                    message="허용되지 않았거나 유효하지 않은 도구 요청입니다.",
                    retryable=False,
                    request_id=request.request_id,
                    trace_id=request.trace_id,
                    run_id=request.run_id,
                ) if error_code else None,
                simulated=False,
                adapter=self.adapter_name,
            )
            self._idempotency[request.idempotency_key] = (fingerprint, result)
            return result
