"""Separate SQLite source of truth for the local review/publication stand-in only.

When the real teammate Response service is connected, that service is the
approval/publication source. This store never opens the core DB/checkpoint.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from rfa_mas.contracts import (
    ApprovalReference,
    DraftBundle,
    DraftBundleV11,
    DraftTarget,
    ExecutionMode,
    PublicationReceipt,
    PublicationStatus,
    ReviewDecision,
    ReviewStatus,
    SimulationScenario,
    ToolResult,
    new_id,
)
from rfa_mas.reference.local_security import (
    LocalServiceError,
    PrivateSqlite,
    register_safe_messages,
)

SERVICE_NAME = "rfa-local-response"
ADAPTER_NAME = "local-reference-response"

register_safe_messages(
    {
        "draft_version_conflict": "같은 초안 버전에 다른 내용을 제출할 수 없습니다.",
        "stale_draft_version": "최신 초안 버전이 아닙니다.",
        "draft_contract_mismatch": "초안 계약 버전이 기존 초안과 다릅니다.",
        "binding_mismatch": "결정 요청이 최신 초안 버전/hash/대상과 일치하지 않습니다.",
        "decision_already_recorded": "이 초안 버전의 수동 결정이 이미 기록되었습니다.",
        "approval_not_found": "저장된 승인을 찾을 수 없습니다.",
        "approval_not_granted": "승인되지 않은 초안은 모의 게시할 수 없습니다.",
        "approval_superseded": "새 초안 버전으로 이전 승인이 무효화되었습니다.",
        "approval_expired": "승인 유효기간이 지났습니다.",
        "approval_binding_mismatch": "게시 요청이 승인된 초안 binding과 일치하지 않습니다.",
        "already_published": "이 승인으로 이미 모의 게시 영수증이 발급되었습니다.",
        "publication_outcome_unknown": (
            "이전 모의 게시 결과가 불확실합니다. 조회만 가능하며 자동 재게시하지 않습니다."
        ),
    }
)

_SAFE_REASONS = {
    ReviewStatus.PENDING: "로컬 수동 검토 대기 중입니다. 자동 승인하지 않습니다.",
    ReviewStatus.APPROVED: (
        "로컬 설치 owner가 이 초안 버전/hash/대상을 수동 승인했습니다. "
        "실제 외부 게시 권한은 없습니다."
    ),
    ReviewStatus.REJECTED: "로컬 설치 owner가 이 초안 버전을 거절했습니다.",
    ReviewStatus.REVISION_REQUESTED: "로컬 설치 owner가 이 초안 버전의 수정을 요청했습니다.",
}

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS drafts (
        draft_id TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version >= 1),
        contract TEXT NOT NULL CHECK (contract IN ('1.0', '1.1')),
        content_hash TEXT NOT NULL,
        payload_hash TEXT,
        draft_json TEXT NOT NULL,
        decision TEXT NOT NULL DEFAULT 'pending',
        decided_by TEXT,
        superseded INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (draft_id, version)
    )""",
    """CREATE TABLE IF NOT EXISTS submissions (
        idempotency_key TEXT PRIMARY KEY,
        fingerprint TEXT NOT NULL,
        draft_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        response_json TEXT NOT NULL,
        FOREIGN KEY (draft_id, version) REFERENCES drafts (draft_id, version)
    )""",
    """CREATE TABLE IF NOT EXISTS decisions (
        draft_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        fingerprint TEXT NOT NULL,
        decision TEXT NOT NULL,
        approver_id TEXT NOT NULL,
        approval_id TEXT UNIQUE,
        approval_json TEXT,
        invalidated INTEGER NOT NULL DEFAULT 0,
        response_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (draft_id, version),
        FOREIGN KEY (draft_id, version) REFERENCES drafts (draft_id, version)
    )""",
    """CREATE TABLE IF NOT EXISTS publications (
        publication_id TEXT PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE,
        fingerprint TEXT NOT NULL,
        approval_id TEXT NOT NULL UNIQUE REFERENCES decisions (approval_id),
        status TEXT NOT NULL,
        response_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS tool_calls (
        idempotency_key TEXT PRIMARY KEY,
        fingerprint TEXT NOT NULL,
        response_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
)


class LocalReviewView(BaseModel):
    """Owner-only local review state. mode/authority are always the local mock."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.1"] = "1.1"
    contract: Literal["1.0", "1.1"]
    draft_id: str
    version: int
    run_id: str
    content: str
    content_hash: str
    payload_hash: str | None = None
    target: DraftTarget
    decision: ReviewStatus
    decided_by: str | None = None
    approval: ApprovalReference | None = None
    superseded: bool
    mode: Literal["mock"] = "mock"
    authority: Literal["reference_mock"] = "reference_mock"
    simulated: Literal[True] = True
    updated_at: datetime


class LocalPublicationView(BaseModel):
    """Synthetic local-artifact receipt. Not a share permission or real publication."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.1"] = "1.1"
    receipt: PublicationReceipt
    artifact_kind: Literal["local-artifact"] = "local-artifact"
    external_write_performed: Literal[False] = False
    authority: Literal["reference_mock"] = "reference_mock"
    simulated: Literal[True] = True
    created_at: datetime


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


class LocalResponseStore:
    def __init__(self, path: Path, *, clock: Callable[[], datetime]) -> None:
        self._db = PrivateSqlite(path, service=SERVICE_NAME, schema=SCHEMA)
        self._clock = clock

    @property
    def path(self) -> Path:
        return self._db.path

    def initialize(self) -> None:
        self._db.initialize()

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise LocalServiceError("configuration_error", 503)
        return now.astimezone(UTC)

    # ---------- reads ----------
    @staticmethod
    def _latest(connection: sqlite3.Connection, draft_id: str) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM drafts WHERE draft_id = ? ORDER BY version DESC LIMIT 1",
            (draft_id,),
        ).fetchone()

    @staticmethod
    def _view(connection: sqlite3.Connection, row: sqlite3.Row) -> LocalReviewView:
        draft_type = DraftBundleV11 if row["contract"] == "1.1" else DraftBundle
        draft = draft_type.model_validate_json(row["draft_json"])
        decision = connection.execute(
            "SELECT approval_json FROM decisions WHERE draft_id = ? AND version = ?",
            (row["draft_id"], row["version"]),
        ).fetchone()
        approval = (
            ApprovalReference.model_validate_json(decision["approval_json"])
            if decision is not None and decision["approval_json"]
            else None
        )
        return LocalReviewView(
            contract=row["contract"],
            draft_id=draft.draft_id,
            version=draft.version,
            run_id=draft.run_id,
            content=draft.content,
            content_hash=draft.content_hash,
            payload_hash=row["payload_hash"],
            target=draft.target,
            decision=ReviewStatus(row["decision"]),
            decided_by=row["decided_by"],
            approval=approval,
            superseded=bool(row["superseded"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _legacy_decision(row: sqlite3.Row) -> ReviewDecision:
        draft = DraftBundle.model_validate_json(row["draft_json"])
        status = ReviewStatus(row["decision"])
        return ReviewDecision(
            request_id=draft.request_id,
            trace_id=draft.trace_id,
            run_id=draft.run_id,
            agent_id=draft.agent_id,
            domain_id=draft.domain_id,
            draft_id=draft.draft_id,
            draft_version=draft.version,
            content_hash=draft.content_hash,
            target=draft.target,
            decision=status,
            publication_status=PublicationStatus.NOT_REQUESTED,
            safe_reason=_SAFE_REASONS[status],
            simulated=True,
            adapter=ADAPTER_NAME,
        )

    def get_view(self, draft_id: str) -> LocalReviewView | None:
        with self._db.transaction(write=False) as connection:
            row = self._latest(connection, draft_id)
            return None if row is None else self._view(connection, row)

    def list_views(self, decision: ReviewStatus | None, limit: int) -> list[LocalReviewView]:
        with self._db.transaction(write=False) as connection:
            rows = connection.execute(
                """SELECT d.* FROM drafts d
                   WHERE d.version = (
                       SELECT MAX(x.version) FROM drafts x WHERE x.draft_id = d.draft_id
                   )
                     AND (? IS NULL OR d.decision = ?)
                   ORDER BY d.updated_at DESC, d.draft_id LIMIT ?""",
                (decision, decision, limit),
            ).fetchall()
            return [self._view(connection, row) for row in rows]

    def get_legacy(self, draft_id: str) -> ReviewDecision | None:
        with self._db.transaction(write=False) as connection:
            row = self._latest(connection, draft_id)
            if row is None or row["contract"] != "1.0":
                return None
            return self._legacy_decision(row)

    def get_publication(self, publication_id: str) -> LocalPublicationView | None:
        with self._db.transaction(write=False) as connection:
            row = connection.execute(
                "SELECT response_json FROM publications WHERE publication_id = ?",
                (publication_id,),
            ).fetchone()
            return None if row is None else LocalPublicationView.model_validate_json(row[0])

    def counts(self) -> dict[str, int]:
        with self._db.transaction(write=False) as connection:
            return {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("drafts", "submissions", "decisions", "publications", "tool_calls")
            }

    # ---------- writes ----------
    def submit(
        self,
        draft: DraftBundle | DraftBundleV11,
        *,
        idempotency_key: str,
        request_fingerprint: str,
        scenario: SimulationScenario | None,
    ) -> ReviewDecision | LocalReviewView:
        """Store a submission as pending. Never approves. Newer versions invalidate approvals."""

        contract = "1.1" if isinstance(draft, DraftBundleV11) else "1.0"
        draft_json = draft.model_dump_json()
        now = _iso(self._now())
        with self._db.transaction() as connection:
            replay = connection.execute(
                "SELECT fingerprint, response_json FROM submissions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if replay is not None:
                if replay["fingerprint"] != request_fingerprint:
                    raise LocalServiceError("idempotency_conflict", 409)
                return self._decode_submission(contract, replay["response_json"])
            latest = self._latest(connection, draft.draft_id)
            if latest is not None and latest["contract"] != contract:
                raise LocalServiceError("draft_contract_mismatch", 409)
            if latest is not None and draft.version < latest["version"]:
                raise LocalServiceError("stale_draft_version", 409)
            if latest is not None and draft.version == latest["version"]:
                if latest["draft_json"] != draft_json:
                    raise LocalServiceError("draft_version_conflict", 409)
                row = latest
            else:
                connection.execute(
                    """INSERT INTO drafts (draft_id, version, contract, content_hash, payload_hash,
                           draft_json, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        draft.draft_id,
                        draft.version,
                        contract,
                        draft.content_hash,
                        draft.payload_hash if isinstance(draft, DraftBundleV11) else None,
                        draft_json,
                        now,
                        now,
                    ),
                )
                # A newer version supersedes every older version and its approval.
                connection.execute(
                    "UPDATE drafts SET superseded = 1, updated_at = ? "
                    "WHERE draft_id = ? AND version < ?",
                    (now, draft.draft_id, draft.version),
                )
                connection.execute(
                    "UPDATE decisions SET invalidated = 1 WHERE draft_id = ? AND version < ?",
                    (draft.draft_id, draft.version),
                )
                row = self._latest(connection, draft.draft_id)
            assert row is not None
            response: ReviewDecision | LocalReviewView = (
                self._view(connection, row) if contract == "1.1" else self._legacy_decision(row)
            )
            connection.execute(
                """INSERT INTO submissions (idempotency_key, fingerprint, draft_id, version,
                       response_json) VALUES (?, ?, ?, ?, ?)""",
                (
                    idempotency_key,
                    request_fingerprint,
                    draft.draft_id,
                    draft.version,
                    response.model_dump_json(),
                ),
            )
            return response

    @staticmethod
    def _decode_submission(contract: str, raw: str) -> ReviewDecision | LocalReviewView:
        try:
            return (
                LocalReviewView.model_validate_json(raw)
                if contract == "1.1"
                else ReviewDecision.model_validate_json(raw)
            )
        except ValidationError:
            # Same key was stored for the other contract kind: a different request.
            raise LocalServiceError("idempotency_conflict", 409) from None

    def decide(
        self,
        draft_id: str,
        *,
        idempotency_key: str,
        request_fingerprint: str,
        draft_version: int,
        content_hash: str,
        payload_hash: str | None,
        target: DraftTarget,
        decision: ReviewStatus,
        approver_id: str,
        approval_ttl: timedelta,
    ) -> LocalReviewView:
        """Record one manual decision for the latest exact version. Approver is server-owned."""

        if decision == ReviewStatus.PENDING:
            raise LocalServiceError("invalid_request", 422)
        now = self._now()
        with self._db.transaction() as connection:
            replay = connection.execute(
                "SELECT fingerprint, response_json FROM decisions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if replay is not None:
                if replay["fingerprint"] != request_fingerprint:
                    raise LocalServiceError("idempotency_conflict", 409)
                return LocalReviewView.model_validate_json(replay["response_json"])
            latest = self._latest(connection, draft_id)
            if latest is None:
                raise LocalServiceError("not_found", 404)
            if draft_version != latest["version"]:
                raise LocalServiceError("stale_draft_version", 409)
            draft_type = DraftBundleV11 if latest["contract"] == "1.1" else DraftBundle
            draft = draft_type.model_validate_json(latest["draft_json"])
            expected_payload = latest["payload_hash"]
            if (
                content_hash != draft.content_hash
                or target != draft.target
                or payload_hash != expected_payload
            ):
                raise LocalServiceError("binding_mismatch", 409)
            if latest["decision"] != ReviewStatus.PENDING.value:
                raise LocalServiceError("decision_already_recorded", 409)
            approval: ApprovalReference | None = None
            if isinstance(draft, DraftBundleV11):
                approval = ApprovalReference(
                    approval_id=new_id("approval"),
                    approver_id=approver_id,
                    binding=draft.binding(),
                    decision=decision,
                    issued_at=now,
                    expires_at=now + approval_ttl,
                    mode=ExecutionMode.MOCK,
                    authority="reference_mock",
                )
            connection.execute(
                "UPDATE drafts SET decision = ?, decided_by = ?, updated_at = ? "
                "WHERE draft_id = ? AND version = ?",
                (decision.value, approver_id, _iso(now), draft_id, draft_version),
            )
            response = self._view(connection, self._latest(connection, draft_id))  # type: ignore[arg-type]
            response = response.model_copy(update={"approval": approval})
            try:
                connection.execute(
                    """INSERT INTO decisions (draft_id, version, idempotency_key, fingerprint,
                           decision, approver_id, approval_id, approval_json, response_json,
                           created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        draft_id,
                        draft_version,
                        idempotency_key,
                        request_fingerprint,
                        decision.value,
                        approver_id,
                        approval.approval_id if approval else None,
                        approval.model_dump_json() if approval else None,
                        response.model_dump_json(),
                        _iso(now),
                    ),
                )
            except sqlite3.IntegrityError:
                raise LocalServiceError("decision_already_recorded", 409) from None
            return response

    def publish(
        self,
        *,
        idempotency_key: str,
        request_fingerprint: str,
        approval_id: str,
        draft_id: str,
        version: int,
        payload_hash: str,
        simulate_outcome: Literal["succeeded", "outcome_unknown"],
    ) -> LocalPublicationView:
        """Issue one synthetic local-artifact receipt for one stored, current approval."""

        now = self._now()
        with self._db.transaction() as connection:
            replay = connection.execute(
                "SELECT fingerprint, response_json FROM publications WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if replay is not None:
                if replay["fingerprint"] != request_fingerprint:
                    raise LocalServiceError("idempotency_conflict", 409)
                # Query of the stored outcome; never re-executes or re-publishes.
                return LocalPublicationView.model_validate_json(replay["response_json"])
            stored = connection.execute(
                "SELECT approval_json, invalidated FROM decisions WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if stored is None or not stored["approval_json"]:
                raise LocalServiceError("approval_not_found", 404)
            approval = ApprovalReference.model_validate_json(stored["approval_json"])
            if approval.decision != ReviewStatus.APPROVED:
                raise LocalServiceError("approval_not_granted", 409)
            binding = approval.binding
            if (draft_id, version, payload_hash) != (
                binding.draft_id,
                binding.version,
                binding.payload_hash,
            ):
                raise LocalServiceError("approval_binding_mismatch", 409)
            latest = self._latest(connection, binding.draft_id)
            if stored["invalidated"] or latest is None or latest["version"] != binding.version:
                raise LocalServiceError("approval_superseded", 409)
            current = DraftBundleV11.model_validate_json(latest["draft_json"])
            if not approval.matches(current, now):
                if now >= approval.expires_at or now < approval.issued_at:
                    raise LocalServiceError("approval_expired", 409)
                raise LocalServiceError("approval_binding_mismatch", 409)
            prior = connection.execute(
                "SELECT status FROM publications WHERE approval_id = ?", (approval_id,)
            ).fetchone()
            if prior is not None:
                if prior["status"] == PublicationStatus.OUTCOME_UNKNOWN.value:
                    raise LocalServiceError("publication_outcome_unknown", 409)
                raise LocalServiceError("already_published", 409)
            publication_id = new_id("publication")
            unknown = simulate_outcome == "outcome_unknown"
            status = PublicationStatus.OUTCOME_UNKNOWN if unknown else PublicationStatus.SUCCEEDED
            try:
                receipt = PublicationReceipt(
                    publication_id=publication_id,
                    run_id=current.run_id,
                    idempotency_key=idempotency_key,
                    binding=binding,
                    approval_id=approval.approval_id,
                    status=status,
                    external_result_ref=None if unknown else f"local-artifact:{publication_id}",
                    mode=ExecutionMode.MOCK,
                    next_action="query" if unknown else "none",
                )
            except ValidationError:
                raise LocalServiceError("approval_binding_mismatch", 409) from None
            view = LocalPublicationView(receipt=receipt, created_at=now)
            try:
                connection.execute(
                    """INSERT INTO publications (publication_id, idempotency_key, fingerprint,
                           approval_id, status, response_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        publication_id,
                        idempotency_key,
                        request_fingerprint,
                        approval.approval_id,
                        receipt.status.value,
                        view.model_dump_json(),
                        _iso(now),
                    ),
                )
            except sqlite3.IntegrityError:
                raise LocalServiceError("already_published", 409) from None
            return view

    def tool_once(
        self,
        *,
        idempotency_key: str,
        request_fingerprint: str,
        compute: Callable[[], ToolResult],
    ) -> ToolResult:
        """Run a side-effect-free synthetic READ at most once per idempotency key."""

        with self._db.transaction() as connection:
            replay = connection.execute(
                "SELECT fingerprint, response_json FROM tool_calls WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if replay is not None:
                if replay["fingerprint"] != request_fingerprint:
                    raise LocalServiceError("idempotency_conflict", 409)
                return ToolResult.model_validate_json(replay["response_json"])
            result = compute()
            connection.execute(
                "INSERT INTO tool_calls (idempotency_key, fingerprint, response_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (idempotency_key, request_fingerprint, result.model_dump_json(), _iso(self._now())),
            )
            return result
