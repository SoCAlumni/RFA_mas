from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rfa_mas.application.state_machine import ensure_transition
from rfa_mas.contracts import (
    AgentSpec,
    Audience,
    KnowledgeDocument,
    PolicyDecision,
    PolicyRequest,
    ResultStatus,
    RunResult,
    StructuredError,
    TaskRequest,
    TaskResult,
    ToolEffect,
    WorkRequest,
    WorkStatus,
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


class SqliteWorkRepository:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        def operation() -> None:
            with self._connect() as connection:
                connection.executescript(
                    """
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
                )

        await asyncio.to_thread(operation)

    async def create_run(self, request: WorkRequest) -> None:
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

    def register(self, task_type: str, handler: TaskHandler) -> None:
        self._handlers[task_type] = handler

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

    def __init__(self, trace_dir: Path, redactor: SecretRedactor) -> None:
        self.trace_dir = trace_dir
        self._redactor = redactor
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
        event_metadata = metadata or {}
        run_simulated = bool(event_metadata.get("simulated", self.simulated))
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": event,
            "request_id": request_id,
            "trace_id": trace_id,
            "run_id": run_id,
            "status": status,
            "simulated": run_simulated,
            "adapter": self.adapter_name,
            "metadata": self._redactor.value(event_metadata),
        }
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        path = self.trace_dir / "events.jsonl"
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        async with self._lock:
            await asyncio.to_thread(self._append, path, line)

    @staticmethod
    def _append(path: Path, line: str) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
