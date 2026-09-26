"""Durable journal for the local runtime stand-in (separate SQLite DB, never the core DB).

Team lifecycle is local metadata only. This is not an OS sandbox, OpenShell, or a
teammate runtime; no sandbox_id is ever issued.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from rfa_mas.contracts import (
    AgentSpec,
    ExecutionMode,
    MemberLifecycle,
    ResultStatus,
    StructuredError,
    TaskRequest,
    TaskResult,
    TeamInstance,
    TeamMember,
    TeamSpec,
)
from rfa_mas.reference.local_security import (
    LocalServiceError,
    PrivateSqlite,
    fingerprint,
    register_safe_messages,
)

SERVICE_NAME = "rfa-local-runtime"
ADAPTER_NAME = "local-reference-runtime"

register_safe_messages(
    {
        "team_spec_conflict": "같은 팀 ID에 다른 팀 명세를 준비할 수 없습니다.",
        "agent_id_conflict": "agent ID가 이미 다른 팀에 속해 있습니다.",
        "team_recovery_required": "팀 상태가 불확실합니다. cleanup으로 복구한 뒤 다시 준비하세요.",
        "team_busy": "실행 중인 task가 있어 팀을 정리할 수 없습니다. 먼저 취소하세요.",
        "run_id_conflict": "같은 run ID가 다른 요청에 이미 사용되었습니다.",
        "run_in_progress": "같은 요청이 다른 프로세스에서 실행 중입니다. 상태를 조회하세요.",
    }
)

RunState = Literal["running", "succeeded", "failed", "denied", "timed_out", "cancelled", "unknown"]
_STATE_BY_STATUS: dict[ResultStatus, RunState] = {
    ResultStatus.SUCCEEDED: "succeeded",
    ResultStatus.FAILED: "failed",
    ResultStatus.DENIED: "denied",
    ResultStatus.TIMED_OUT: "timed_out",
    ResultStatus.OUTCOME_UNKNOWN: "unknown",
}

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS teams (
        team_id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL,
        spec_fingerprint TEXT NOT NULL,
        instance_json TEXT NOT NULL,
        generation INTEGER NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS team_members (
        agent_id TEXT PRIMARY KEY,
        team_id TEXT NOT NULL REFERENCES teams (team_id),
        role TEXT NOT NULL,
        spec_json TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS team_operations (
        idempotency_key TEXT PRIMARY KEY,
        fingerprint TEXT NOT NULL,
        team_id TEXT NOT NULL,
        operation TEXT NOT NULL CHECK (operation IN ('prepare', 'cleanup')),
        response_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE,
        fingerprint TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        domain_id TEXT NOT NULL,
        task_type TEXT NOT NULL,
        identity_json TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN
            ('running', 'succeeded', 'failed', 'denied', 'timed_out', 'cancelled', 'unknown')),
        result_json TEXT,
        recovered INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
)


class LocalMemberPrepareError(Exception):
    """Typed local-metadata failure: explicitly no allocation happened."""


class LocalMemberCleanupError(Exception):
    """Typed local-metadata cleanup failure."""


MemberHook = Callable[[TeamMember], None]


class LocalRunJournal(BaseModel):
    """Owner-only journal entry. Never contains payload bodies."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.1"] = "1.1"
    run_id: str
    agent_id: str
    domain_id: str
    task_type: str
    state: RunState
    recovered_after_restart: bool
    auto_replay: Literal[False] = False
    mode: Literal["local"] = "local"
    simulated: Literal[True] = True
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class RunStart:
    kind: Literal["result", "wait", "start"]
    result: TaskResult | None = None


def task_result(
    identity: dict[str, Any],
    status: ResultStatus,
    *,
    output: dict[str, Any] | None = None,
    code: str | None = None,
    message: str = "",
) -> TaskResult:
    return TaskResult(
        request_id=identity["request_id"],
        trace_id=identity["trace_id"],
        run_id=identity["run_id"],
        agent_id=identity["agent_id"],
        domain_id=identity["domain_id"],
        status=status,
        output=output or {},
        error=(
            None
            if code is None
            else StructuredError(
                code=code,
                retryable=False,
                message=message,
                request_id=identity["request_id"],
                trace_id=identity["trace_id"],
                run_id=identity["run_id"],
            )
        ),
        simulated=True,
        adapter=ADAPTER_NAME,
    )


def identity_of(request: TaskRequest) -> dict[str, Any]:
    return {
        "request_id": request.request_id,
        "trace_id": request.trace_id,
        "run_id": request.run_id,
        "agent_id": request.agent_id,
        "domain_id": request.domain_id.value,
    }


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _team_state(states: list[MemberLifecycle], *, phase: str) -> str:
    if phase == "prepare":
        if any(m.prepare == "unknown" for m in states):
            return "unknown"
        return "ready" if all(m.prepare == "prepared" for m in states) else "failed"
    if any(m.cleanup == "unknown" for m in states):
        return "unknown"
    return "failed" if any(m.cleanup == "failed" for m in states) else "cleaned"


class LocalRuntimeStore:
    def __init__(self, path: Path, *, clock: Callable[[], datetime]) -> None:
        self._db = PrivateSqlite(path, service=SERVICE_NAME, schema=SCHEMA)
        self._clock = clock

    @property
    def path(self) -> Path:
        return self._db.path

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise LocalServiceError("configuration_error", 503)
        return now.astimezone(UTC)

    def initialize(self) -> int:
        """Create schema and mark every journaled in-flight run unknown. Never replays."""

        self._db.initialize()
        now = _iso(self._now())
        with self._db.transaction() as connection:
            rows = connection.execute(
                "SELECT run_id, identity_json FROM runs WHERE state = 'running'"
            ).fetchall()
            for row in rows:
                unknown = task_result(
                    json.loads(row["identity_json"]),
                    ResultStatus.OUTCOME_UNKNOWN,
                    output={"recovery": "manual_query_required", "auto_replay": False},
                    code="outcome_unknown",
                    message="재시작으로 실행 결과를 확인할 수 없습니다. 자동 재실행하지 않습니다.",
                )
                connection.execute(
                    "UPDATE runs SET state = 'unknown', result_json = ?, recovered = 1, "
                    "updated_at = ? WHERE run_id = ? AND state = 'running'",
                    (unknown.model_dump_json(), now, row["run_id"]),
                )
            return len(rows)

    # ---------------- teams ----------------
    @staticmethod
    def _replay(
        connection: sqlite3.Connection, key: str, request_fingerprint: str, operation: str
    ) -> TeamInstance | None:
        row = connection.execute(
            "SELECT fingerprint, operation, response_json FROM team_operations "
            "WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        if row["fingerprint"] != request_fingerprint or row["operation"] != operation:
            raise LocalServiceError("idempotency_conflict", 409)
        return TeamInstance.model_validate_json(row["response_json"])

    def _record(
        self,
        connection: sqlite3.Connection,
        *,
        key: str,
        request_fingerprint: str,
        operation: str,
        instance: TeamInstance,
    ) -> TeamInstance:
        connection.execute(
            "INSERT INTO team_operations (idempotency_key, fingerprint, team_id, operation, "
            "response_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                key,
                request_fingerprint,
                instance.spec.team_id,
                operation,
                instance.model_dump_json(),
                _iso(self._now()),
            ),
        )
        return instance

    def prepare(
        self,
        spec: TeamSpec,
        *,
        owner_id: str,
        idempotency_key: str,
        request_fingerprint: str,
        prepare_member: MemberHook,
    ) -> TeamInstance:
        spec_fingerprint = fingerprint(spec.model_dump(mode="json"))
        with self._db.transaction() as connection:
            replay = self._replay(connection, idempotency_key, request_fingerprint, "prepare")
            if replay is not None:
                return replay
            row = connection.execute(
                "SELECT * FROM teams WHERE team_id = ?", (spec.team_id,)
            ).fetchone()
            fresh = [MemberLifecycle(agent_id=m.spec.agent_id) for m in spec.members]
            generation = 1
            if row is not None:
                if row["owner_id"] != owner_id or row["spec_fingerprint"] != spec_fingerprint:
                    raise LocalServiceError("team_spec_conflict", 409)
                current = TeamInstance.model_validate_json(row["instance_json"])
                generation = row["generation"] + 1
                if current.state == "ready":
                    return self._record(
                        connection,
                        key=idempotency_key,
                        request_fingerprint=request_fingerprint,
                        operation="prepare",
                        instance=current,
                    )
                if current.state not in {"failed", "cleaned"} or any(
                    m.cleanup in {"failed", "unknown"} for m in current.member_states
                ):
                    raise LocalServiceError("team_recovery_required", 409)
                states = fresh if current.state == "cleaned" else list(current.member_states)
            else:
                for member in spec.members:
                    bound = connection.execute(
                        "SELECT team_id FROM team_members WHERE agent_id = ?",
                        (member.spec.agent_id,),
                    ).fetchone()
                    if bound is not None and bound["team_id"] != spec.team_id:
                        raise LocalServiceError("agent_id_conflict", 409)
                states = fresh
            for index, member in enumerate(spec.members):
                if states[index].prepare == "prepared":
                    continue
                try:
                    prepare_member(member)
                    outcome = "prepared"
                except LocalMemberPrepareError:
                    outcome = "failed"
                except Exception:
                    outcome = "unknown"
                states[index] = MemberLifecycle(agent_id=member.spec.agent_id, prepare=outcome)
                if outcome != "prepared":
                    break
            instance = TeamInstance(
                spec=spec,
                state=_team_state(states, phase="prepare"),
                runtime_ref=f"local-reference:{spec.team_id}",
                sandbox_id=None,
                mode=ExecutionMode.LOCAL,
                member_states=tuple(states),
                failed_agent_ids=tuple(m.agent_id for m in states if m.prepare == "failed"),
            )
            now = _iso(self._now())
            connection.execute(
                "INSERT INTO teams (team_id, owner_id, spec_fingerprint, instance_json, "
                "generation, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (team_id) DO UPDATE SET instance_json = excluded.instance_json, "
                "generation = excluded.generation, updated_at = excluded.updated_at",
                (
                    spec.team_id,
                    owner_id,
                    spec_fingerprint,
                    instance.model_dump_json(),
                    generation,
                    now,
                ),
            )
            connection.executemany(
                "INSERT OR IGNORE INTO team_members (agent_id, team_id, role, spec_json) "
                "VALUES (?, ?, ?, ?)",
                [
                    (m.spec.agent_id, spec.team_id, m.role, m.spec.model_dump_json())
                    for m in spec.members
                ],
            )
            return self._record(
                connection,
                key=idempotency_key,
                request_fingerprint=request_fingerprint,
                operation="prepare",
                instance=instance,
            )

    def cleanup(
        self,
        team_id: str,
        *,
        owner_id: str,
        idempotency_key: str,
        request_fingerprint: str,
        cleanup_member: MemberHook,
    ) -> TeamInstance:
        with self._db.transaction() as connection:
            replay = self._replay(connection, idempotency_key, request_fingerprint, "cleanup")
            if replay is not None:
                return replay
            row = connection.execute("SELECT * FROM teams WHERE team_id = ?", (team_id,)).fetchone()
            if row is None or row["owner_id"] != owner_id:
                raise LocalServiceError("not_found", 404)
            busy = connection.execute(
                "SELECT COUNT(*) FROM runs r JOIN team_members m ON r.agent_id = m.agent_id "
                "WHERE m.team_id = ? AND r.state = 'running'",
                (team_id,),
            ).fetchone()[0]
            if busy:
                raise LocalServiceError("team_busy", 409)
            current = TeamInstance.model_validate_json(row["instance_json"])
            if current.state != "cleaned":
                states = list(current.member_states)
                for index, member in enumerate(current.spec.members):
                    if states[index].prepare not in {"prepared", "unknown"}:
                        continue
                    if states[index].cleanup == "cleaned":
                        continue
                    try:
                        cleanup_member(member)
                        outcome = "cleaned"
                    except LocalMemberCleanupError:
                        outcome = "failed"
                    except Exception:
                        outcome = "unknown"
                    states[index] = states[index].model_copy(update={"cleanup": outcome})
                current = TeamInstance(
                    spec=current.spec,
                    state=_team_state(states, phase="cleanup"),
                    runtime_ref=current.runtime_ref,
                    sandbox_id=None,
                    mode=ExecutionMode.LOCAL,
                    member_states=tuple(states),
                    failed_agent_ids=tuple(
                        m.agent_id for m in states if "failed" in {m.prepare, m.cleanup}
                    ),
                )
                connection.execute(
                    "UPDATE teams SET instance_json = ?, updated_at = ? WHERE team_id = ?",
                    (current.model_dump_json(), _iso(self._now()), team_id),
                )
            return self._record(
                connection,
                key=idempotency_key,
                request_fingerprint=request_fingerprint,
                operation="cleanup",
                instance=current,
            )

    def get_team(self, team_id: str, *, owner_id: str) -> TeamInstance | None:
        with self._db.transaction(write=False) as connection:
            row = connection.execute(
                "SELECT owner_id, instance_json FROM teams WHERE team_id = ?", (team_id,)
            ).fetchone()
            if row is None or row["owner_id"] != owner_id:
                return None
            return TeamInstance.model_validate_json(row["instance_json"])

    # ---------------- runs ----------------
    def begin_run(
        self,
        spec: AgentSpec,
        request: TaskRequest,
        *,
        owner_id: str,
        request_fingerprint: str,
        denial: TaskResult | None,
        task_type_label: str,
    ) -> RunStart:
        """Journal the run as 'running' BEFORE any handler executes, or persist a denial.

        task_type_label is a server-registered handler name or a fixed marker; raw
        caller strings are never journaled.
        """

        identity = identity_of(request)
        now = _iso(self._now())
        with self._db.transaction() as connection:
            row = connection.execute(
                "SELECT fingerprint, owner_id, state, result_json FROM runs "
                "WHERE idempotency_key = ?",
                (request.idempotency_key,),
            ).fetchone()
            if row is not None:
                if row["fingerprint"] != request_fingerprint or row["owner_id"] != owner_id:
                    raise LocalServiceError("idempotency_conflict", 409)
                if row["state"] == "running":
                    return RunStart("wait")
                return RunStart("result", TaskResult.model_validate_json(row["result_json"]))
            if connection.execute(
                "SELECT 1 FROM runs WHERE run_id = ?", (request.run_id,)
            ).fetchone():
                raise LocalServiceError("run_id_conflict", 409)
            if denial is None:
                denial = self._team_denial(connection, spec, identity, owner_id)
            state: RunState = "running" if denial is None else _STATE_BY_STATUS[denial.status]
            connection.execute(
                """INSERT INTO runs (run_id, idempotency_key, fingerprint, owner_id, agent_id,
                       domain_id, task_type, identity_json, state, result_json, created_at,
                       updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    request.run_id,
                    request.idempotency_key,
                    request_fingerprint,
                    owner_id,
                    request.agent_id,
                    request.domain_id.value,
                    task_type_label,
                    json.dumps(identity, sort_keys=True),
                    state,
                    None if denial is None else denial.model_dump_json(),
                    now,
                    now,
                ),
            )
            return RunStart("start") if denial is None else RunStart("result", denial)

    @staticmethod
    def _team_denial(
        connection: sqlite3.Connection,
        spec: AgentSpec,
        identity: dict[str, Any],
        owner_id: str,
    ) -> TaskResult | None:
        member = connection.execute(
            "SELECT m.spec_json, t.owner_id, t.instance_json FROM team_members m "
            "JOIN teams t ON t.team_id = m.team_id WHERE m.agent_id = ?",
            (spec.agent_id,),
        ).fetchone()
        if member is None:
            return None
        if member["owner_id"] != owner_id:
            code, message = "agent_not_owned", "다른 owner의 팀 agent는 실행할 수 없습니다."
        elif TeamInstance.model_validate_json(member["instance_json"]).state != "ready":
            code, message = "team_not_ready", "준비 완료된 팀의 agent만 실행할 수 있습니다."
        elif AgentSpec.model_validate_json(member["spec_json"]) != spec:
            code, message = "agent_spec_mismatch", "준비된 팀 agent 명세와 다릅니다."
        else:
            return None
        return task_result(identity, ResultStatus.DENIED, code=code, message=message)

    def finish_run(self, run_id: str, result: TaskResult) -> TaskResult:
        """Only running -> terminal. A recorded cancel or restart-unknown always wins."""

        with self._db.transaction() as connection:
            row = connection.execute(
                "SELECT state, result_json FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise LocalServiceError("not_found", 404)
            if row["state"] != "running":
                return TaskResult.model_validate_json(row["result_json"])
            connection.execute(
                "UPDATE runs SET state = ?, result_json = ?, updated_at = ? WHERE run_id = ?",
                (
                    _STATE_BY_STATUS[result.status],
                    result.model_dump_json(),
                    _iso(self._now()),
                    run_id,
                ),
            )
            return result

    def cancel_run(self, run_id: str, *, owner_id: str) -> tuple[TaskResult | None, bool]:
        with self._db.transaction() as connection:
            row = connection.execute(
                "SELECT owner_id, state, identity_json, result_json FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None or row["owner_id"] != owner_id:
                return None, False
            if row["state"] != "running":
                return TaskResult.model_validate_json(row["result_json"]), False
            cancelled = task_result(
                json.loads(row["identity_json"]),
                ResultStatus.FAILED,
                output={"cancelled": True},
                code="cancelled",
                message="로컬 runtime에서 task가 취소되었습니다.",
            )
            connection.execute(
                "UPDATE runs SET state = 'cancelled', result_json = ?, updated_at = ? "
                "WHERE run_id = ?",
                (cancelled.model_dump_json(), _iso(self._now()), run_id),
            )
            return cancelled, True

    def get_result(self, run_id: str, *, owner_id: str) -> TaskResult | None:
        with self._db.transaction(write=False) as connection:
            row = connection.execute(
                "SELECT owner_id, state, result_json FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None or row["owner_id"] != owner_id or row["state"] == "running":
                return None
            return TaskResult.model_validate_json(row["result_json"])

    def get_journal(self, run_id: str, *, owner_id: str) -> LocalRunJournal | None:
        with self._db.transaction(write=False) as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None or row["owner_id"] != owner_id:
                return None
            return LocalRunJournal(
                run_id=row["run_id"],
                agent_id=row["agent_id"],
                domain_id=row["domain_id"],
                task_type=row["task_type"],
                state=row["state"],
                recovered_after_restart=bool(row["recovered"]),
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )

    def counts(self) -> dict[str, int]:
        with self._db.transaction(write=False) as connection:
            return {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("teams", "team_members", "team_operations", "runs")
            }
