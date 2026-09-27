"""Opt-in Langfuse export of verified, allowlisted observation metadata (P1-006C).

The local JSONL trace stays the source of truth. Export is additive: a record is sent only
after the local trace has verified it against the SQLite ledger and written it. The record
is projected onto a fixed attribute allowlist (opaque aliases, enum codes, counts,
durations, version references). It is sent once over OTLP/HTTP JSON to a self-hosted
Langfuse v4 ("/api/public/otel/v1/traces"), and the outcome is kept separately as
exported / failed / not_attempted. A connection or HTTP failure is never a trace success
and never fails the run.

Egress requires three things together: TRACE_BACKEND=langfuse, a loopback
LANGFUSE_BASE_URL, and LANGFUSE_EXPORT_ENABLED=true. Keys alone grant nothing.

Retention (P1-006F): community/OSS Langfuse ignores project retention without the
Enterprise "data-retention" entitlement, so LangfuseRetentionSweeper deletes this app's own
exported traces by ID once they are older than TRACE_RETENTION_DAYS and confirms the
deletion by querying again. It never touches traces without this app's export schema.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

import httpx
from pydantic import SecretStr, ValidationError

from rfa_mas.adapters.http import require_loopback_reference_url
from rfa_mas.adapters.local import LocalJsonlTrace
from rfa_mas.contracts import ObservationRecord, TraceEvent
from rfa_mas.errors import BackendNotImplementedError
from rfa_mas.security import SecretRedactor

logger = logging.getLogger(__name__)

OTLP_TRACES_PATH = "/api/public/otel/v1/traces"
OBSERVATIONS_PATH = "/api/public/v2/observations"
TRACES_PATH = "/api/public/traces"
EXPORT_SCHEMA = "rfa-langfuse-export-v1"
SERVICE_NAME = "rfa-mas"
TRACE_NAME = "rfa-run"
METADATA_PREFIX = "langfuse.observation.metadata."

ExportStatus = Literal["exported", "failed", "not_attempted"]
ExportReason = Literal[
    "ok",
    "egress_not_permitted",
    "credentials_missing",
    "redaction_blocked",
    "invalid_record",
    "connection_error",
    "timeout",
    "auth_error",
    "http_error",
    "rejected",
    "invalid_response",
    "internal_error",
]
_FAILED_REASONS = frozenset(
    {
        "connection_error",
        "timeout",
        "auth_error",
        "http_error",
        "rejected",
        "invalid_response",
        "internal_error",
    }
)

# Explicit allowlist of (metadata key, path into ObservationRecord.model_dump(mode="json")).
# Every value is an opaque alias minted by trusted code, a contract enum/code, a count, a
# duration or a version reference. Adding a contract field does not export it.
ALLOWED_METADATA: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("observation_id", ("observation_id",)),
    ("sequence", ("sequence",)),
    ("origin", ("origin",)),
    ("provider_ref", ("provider_ref",)),
    ("provider_kind", ("provider_kind",)),
    ("transport", ("transport",)),
    ("boundary", ("event", "event")),
    ("status", ("event", "status")),
    ("mode", ("event", "mode")),
    ("reason_code", ("event", "reason_code")),
    ("call_count", ("event", "call_count")),
    ("duration_ms", ("event", "duration_ms")),
    ("policy_decision_ref", ("event", "policy_decision_id")),
    ("sandbox_ref", ("event", "sandbox_id")),
    ("draft_ref", ("event", "draft_id")),
    ("approval_ref", ("event", "approval_id")),
    ("publication_ref", ("event", "publication_id")),
    ("evidence_ref", ("event", "evidence_ref")),
    ("request_ref", ("event", "execution", "request_id")),
    ("trace_ref", ("event", "execution", "trace_id")),
    ("run_ref", ("event", "execution", "run_id")),
    ("agent_ref", ("event", "execution", "agent_id")),
    ("session_ref", ("event", "execution", "session_id")),
    ("domain_id", ("event", "execution", "domain_id")),
    ("task_ref", ("event", "execution", "task_id")),
    ("team_ref", ("event", "execution", "team_id")),
    ("version_code", ("event", "versions", "code")),
    ("version_contract", ("event", "versions", "contract")),
    ("version_policy", ("event", "versions", "policy")),
    ("version_dataset", ("event", "versions", "dataset")),
    ("version_evaluator", ("event", "versions", "evaluator")),
    ("version_model", ("event", "versions", "model")),
    ("version_prompt", ("event", "versions", "prompt")),
    ("version_template", ("event", "versions", "template")),
    ("version_sources", ("event", "versions", "sources")),
)
ALLOWED_ATTRIBUTE_KEYS = frozenset(
    {
        "langfuse.trace.name",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        METADATA_PREFIX + "schema",
        METADATA_PREFIX + "token_usage",
        *(METADATA_PREFIX + key for key, _ in ALLOWED_METADATA),
    }
)
_ERROR_STATUSES = frozenset({"failed", "outcome_unknown"})


@dataclass(frozen=True)
class TraceExportReceipt:
    """Export outcome, kept apart from the local trace. Only fixed codes, never server text."""

    observation_id: str
    status: ExportStatus
    reason: ExportReason
    http_status: int | None = None
    otel_trace_id: str | None = None
    otel_span_id: str | None = None

    def __post_init__(self) -> None:
        if (self.status == "exported") != (self.reason == "ok"):
            raise ValueError("only an acknowledged export is 'exported'")
        if self.status == "failed" and self.reason not in _FAILED_REASONS:
            raise ValueError("failed export requires an attempted-request reason")
        if self.status == "not_attempted" and self.reason in _FAILED_REASONS:
            raise ValueError("not_attempted export cannot carry a request outcome")


class TraceExporter(Protocol):
    """Swappable transport (OTLP today; any other must keep the same receipt semantics)."""

    exporter_name: str

    async def export(self, record: ObservationRecord) -> TraceExportReceipt: ...


@dataclass(frozen=True)
class LangfuseEgress:
    """Per-endpoint egress permission. Key presence is never permission."""

    base_url: str | None
    export_enabled: bool

    def permitted(self) -> bool:
        if self.export_enabled is not True or not self.base_url:
            return False
        try:
            parsed = httpx.URL(self.base_url)
            if parsed.scheme not in {"http", "https"} or parsed.userinfo:
                return False
            if parsed.query or parsed.fragment:
                return False
            require_loopback_reference_url(self.base_url, setting_name="LANGFUSE_BASE_URL")
        except (BackendNotImplementedError, httpx.InvalidURL, ValueError, TypeError):
            return False
        return True


def _hex_id(kind: str, parts: Sequence[str], length: int) -> str:
    # Format mapping only: the inputs are already opaque aliases minted by trusted code.
    # The digest is not claimed as anonymization of anything.
    digest = hashlib.sha256("|".join((EXPORT_SCHEMA, kind, *parts)).encode("utf-8"))
    return digest.hexdigest()[:length]


def otel_ids(record: ObservationRecord) -> tuple[str, str]:
    """Deterministic OTel trace id (one per run) and span id (one per observation)."""
    execution = record.event.execution
    trace_id = _hex_id("trace", (execution.trace_id, execution.run_id), 32)
    span_id = _hex_id("span", (execution.run_id, record.observation_id), 16)
    return trace_id, span_id


def _pick(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = data
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def safe_attributes(record: ObservationRecord) -> dict[str, Any]:
    """Allowlisted attributes only. Unreported tokens are omitted, never sent as 0."""
    data = record.model_dump(mode="json")
    attributes: dict[str, Any] = {
        "langfuse.trace.name": TRACE_NAME,
        METADATA_PREFIX + "schema": EXPORT_SCHEMA,
    }
    for key, path in ALLOWED_METADATA:
        value = _pick(data, path)
        if value is None or value == []:
            continue
        if isinstance(value, list):
            value = tuple(str(item) for item in value)
        elif not isinstance(value, (str, int, float, bool)):
            raise ValueError("non-scalar value outside the allowlist")
        attributes[METADATA_PREFIX + key] = value
    event: TraceEvent = record.event
    reported = 0
    if event.input_tokens is not None:
        attributes["gen_ai.usage.input_tokens"] = event.input_tokens
        reported += 1
    if event.output_tokens is not None:
        attributes["gen_ai.usage.output_tokens"] = event.output_tokens
        reported += 1
    attributes[METADATA_PREFIX + "token_usage"] = ("not_reported", "partial", "reported")[reported]
    if not set(attributes) <= ALLOWED_ATTRIBUTE_KEYS:
        raise ValueError("attribute outside the allowlist")
    return attributes


def _otlp_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, tuple):
        return {"arrayValue": {"values": [_otlp_value(item) for item in value]}}
    raise ValueError("unsupported attribute value")


def _unix_nanos(record: ObservationRecord) -> tuple[int, int]:
    end = record.event.timestamp
    end_nanos = (int(end.replace(microsecond=0).timestamp()) * 1_000_000_000) + (
        end.microsecond * 1_000
    )
    duration = record.event.duration_ms
    start_nanos = end_nanos - (round(duration * 1_000_000) if duration is not None else 0)
    return start_nanos, end_nanos


def otlp_payload(record: ObservationRecord) -> dict[str, Any]:
    """One OTLP/JSON span. Status message and span events stay empty (no free text)."""
    trace_id, span_id = otel_ids(record)
    start, end = _unix_nanos(record)
    status = record.event.status
    code = 2 if status in _ERROR_STATUSES else 1 if status == "succeeded" else 0
    span = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": f"rfa.{record.event.event}",
        "kind": 1,
        "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(end),
        "attributes": [
            {"key": key, "value": _otlp_value(value)}
            for key, value in sorted(safe_attributes(record).items())
        ],
        "status": {"code": code},
    }
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": SERVICE_NAME}}]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "rfa_mas.adapters.langfuse", "version": EXPORT_SCHEMA},
                        "spans": [span],
                    }
                ],
            }
        ]
    }


class LangfuseOtlpExporter:
    """Single-attempt OTLP/HTTP JSON export. Only an acknowledged 2xx counts as exported."""

    exporter_name = "langfuse-otlp-http-json"

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        egress: LangfuseEgress,
        public_key: SecretStr | None,
        secret_key: SecretStr | None,
        redactor: SecretRedactor,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._client = client
        self.egress = egress
        self._public_key = public_key
        self._secret_key = secret_key
        self._redactor = redactor
        self._timeout = timeout_seconds

    def _authorization(self) -> str | None:
        return _basic_authorization(self._public_key, self._secret_key)

    def _contains_secret(self, body: str) -> bool:
        keys = (self._public_key, self._secret_key)
        known = tuple(key.get_secret_value() for key in keys if key is not None)
        return self._redactor.text(body) != body or any(v and v in body for v in known)

    async def export(self, record: ObservationRecord) -> TraceExportReceipt:
        try:
            record = ObservationRecord.model_validate(record.model_dump())
            trace_id, span_id = otel_ids(record)
            body = json.dumps(otlp_payload(record), separators=(",", ":"), sort_keys=True)
        except (ValidationError, ValueError, TypeError, AttributeError):
            return TraceExportReceipt("invalid", "not_attempted", "invalid_record")

        def receipt(status: ExportStatus, reason: ExportReason, http: int | None = None):
            return TraceExportReceipt(
                record.observation_id, status, reason, http, trace_id, span_id
            )

        if not self.egress.permitted():
            return receipt("not_attempted", "egress_not_permitted")
        authorization = self._authorization()
        if authorization is None:
            return receipt("not_attempted", "credentials_missing")
        if self._contains_secret(body):
            return receipt("not_attempted", "redaction_blocked")
        try:
            response = await self._client.post(
                OTLP_TRACES_PATH,
                content=body.encode("utf-8"),
                headers={
                    "Authorization": authorization,
                    "Content-Type": "application/json",
                    # Langfuse v4: make OTel spans queryable immediately.
                    "x-langfuse-ingestion-version": "4",
                },
                timeout=self._timeout,
            )
        except httpx.TimeoutException:
            return receipt("failed", "timeout")
        except (httpx.HTTPError, OSError):
            return receipt("failed", "connection_error")
        code = response.status_code
        if code in {401, 403}:
            return receipt("failed", "auth_error", code)
        if not 200 <= code < 300:
            return receipt("failed", "http_error", code)
        if not response.content.strip():
            return receipt("exported", "ok", code)
        try:
            data = response.json()
        except ValueError:
            return receipt("failed", "invalid_response", code)
        # A 2xx JSON object is an acknowledgement unless it reports a rejection. Langfuse
        # 4.46 answers with a queued ingestion-job object that echoes the project public
        # key and auth scope; the body is never kept. Processing is asynchronous, so
        # "exported" means acknowledged; queryability is proven only by read-back by ID.
        if not isinstance(data, dict):
            return receipt("failed", "invalid_response", code)
        if data.get("errors"):
            # Legacy ingestion-style per-event errors (e.g. a 207 multi-status body).
            return receipt("failed", "rejected", code)
        partial = data.get("partialSuccess")
        if partial is not None:
            if not isinstance(partial, dict):
                return receipt("failed", "invalid_response", code)
            try:
                rejected = int(partial.get("rejectedSpans") or 0)
            except (TypeError, ValueError):
                return receipt("failed", "invalid_response", code)
            if rejected > 0:
                # Server text (errorMessage) is untrusted and never kept.
                return receipt("failed", "rejected", code)
        return receipt("exported", "ok", code)


class LangfuseExportTrace:
    """TracePort: local JSONL (source of truth) first, then additive Langfuse export."""

    adapter_name = "local-jsonl-trace+langfuse-otlp"
    simulated = False

    def __init__(
        self, local: LocalJsonlTrace, exporter: TraceExporter, *, max_receipts: int = 10_000
    ) -> None:
        self.local = local
        self.exporter = exporter
        self._receipts: OrderedDict[str, TraceExportReceipt] = OrderedDict()
        self._max_receipts = max_receipts

    @property
    def trace_dir(self):
        return self.local.trace_dir

    @property
    def retention_days(self) -> int:
        return self.local.retention_days

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
        await self.local.emit(
            event=event,
            request_id=request_id,
            trace_id=trace_id,
            run_id=run_id,
            status=status,
            metadata=metadata,
        )

    async def emit_event(self, event: TraceEvent) -> None:
        await self.local.emit_event(event)

    async def emit_observation(self, record: ObservationRecord) -> None:
        # Raises for an unverified record or a failed local write: nothing is exported then.
        await self.local.emit_observation(record)
        try:
            receipt = await self.exporter.export(record)
        except Exception:  # export is additive; a telemetry bug must not fail the run
            receipt = TraceExportReceipt(record.observation_id, "failed", "internal_error")
        self._receipts[receipt.observation_id] = receipt
        self._receipts.move_to_end(receipt.observation_id)
        while len(self._receipts) > self._max_receipts:
            self._receipts.popitem(last=False)
        if receipt.status != "exported":
            logger.warning("langfuse trace export %s: %s", receipt.status, receipt.reason)

    def export_receipt(self, observation_id: str) -> TraceExportReceipt | None:
        return self._receipts.get(observation_id)

    def export_receipts(self) -> tuple[TraceExportReceipt, ...]:
        return tuple(self._receipts.values())


def _basic_authorization(public_key: SecretStr | None, secret_key: SecretStr | None) -> str | None:
    if public_key is None or secret_key is None:
        return None
    public, secret = public_key.get_secret_value(), secret_key.get_secret_value()
    if not public or not secret:
        return None
    return "Basic " + base64.b64encode(f"{public}:{secret}".encode()).decode("ascii")


# ---------------------------------------------------------------------------------------
# P1-006F: self-run retention job for community (OSS) Langfuse.
# ---------------------------------------------------------------------------------------

SweepStatus = Literal["completed", "partial", "failed", "not_attempted"]
SweepReason = Literal[
    "ok",
    "egress_not_permitted",
    "credentials_missing",
    "invalid_retention",
    "connection_error",
    "timeout",
    "auth_error",
    "http_error",
    "invalid_response",
    "budget_exhausted",
    "delete_unconfirmed",
]
_OTEL_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
# Oldest start time the sweep looks at. Wider than any retention the setting allows.
_SCAN_HORIZON = timedelta(days=3650)


@dataclass(frozen=True)
class RetentionSweepReport:
    """Counts and fixed codes only. No trace content, server text, key or raw ID list."""

    status: SweepStatus
    reason: SweepReason
    retention_days: int
    cutoff: str
    dry_run: bool
    scanned_observations: int = 0
    foreign_observations: int = 0
    expired_traces: int = 0
    kept_recent_traces: int = 0
    deleted_traces: int = 0
    confirmed_gone: int = 0
    still_queryable: int = 0
    delete_failed: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in self.__dataclass_fields__}


class _SweepError(Exception):
    def __init__(self, reason: SweepReason) -> None:
        super().__init__(reason)
        self.reason = reason


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _is_own_observation(row: dict[str, Any]) -> bool:
    """Only rows written by this app's exporter (schema marker + our service name)."""
    metadata = row.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("schema") != EXPORT_SCHEMA:
        return False
    service = metadata.get("resourceAttributes.service.name")
    return service == SERVICE_NAME


class LangfuseRetentionSweeper:
    """Delete this app's Langfuse traces older than the retention period, by trace ID.

    A trace is expired only when it has an own observation older than the cutoff and no
    observation at or after the cutoff. Deletion is confirmed by querying the trace ID
    again; an acknowledged DELETE that is still queryable after the bound is reported as
    still_queryable, never as gone. Page, delete and wait budgets are explicit.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        egress: LangfuseEgress,
        public_key: SecretStr | None,
        secret_key: SecretStr | None,
        retention_days: int,
        page_limit: int = 500,
        max_pages: int = 40,
        max_deletes: int = 500,
        confirm_seconds: float = 180.0,
        poll_seconds: float = 5.0,
        timeout_seconds: float = 10.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self.egress = egress
        self._public_key = public_key
        self._secret_key = secret_key
        self.retention_days = retention_days
        self._page_limit = page_limit
        self._max_pages = max_pages
        self._max_deletes = max_deletes
        self._confirm_seconds = confirm_seconds
        self._poll_seconds = poll_seconds
        self._timeout = timeout_seconds
        self._sleep = sleep
        self._monotonic = monotonic

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        authorization = _basic_authorization(self._public_key, self._secret_key)
        if authorization is None:
            raise _SweepError("credentials_missing")
        if not self.egress.permitted():
            raise _SweepError("egress_not_permitted")
        try:
            response = await self._client.request(
                method,
                path,
                headers={"Authorization": authorization},
                timeout=self._timeout,
                **kwargs,
            )
        except httpx.TimeoutException:
            raise _SweepError("timeout") from None
        except (httpx.HTTPError, OSError):
            raise _SweepError("connection_error") from None
        if response.status_code in {401, 403}:
            raise _SweepError("auth_error")
        return response

    async def _query(
        self,
        *,
        start: datetime,
        end: datetime,
        trace_id: str | None = None,
        cursor: str | None = None,
        page: int | None = None,
    ) -> tuple[list[dict[str, Any]], str | None, bool]:
        params: dict[str, Any] = {
            "fields": "core,basic,metadata",
            "limit": self._page_limit,
            "fromStartTime": _iso(start),
            "toStartTime": _iso(end),
        }
        if trace_id is not None:
            params["filter"] = json.dumps(
                [{"type": "string", "column": "traceId", "operator": "=", "value": trace_id}]
            )
        if cursor is not None:
            params["cursor"] = cursor
        elif page is not None:
            params["page"] = page
        response = await self._request("GET", OBSERVATIONS_PATH, params=params)
        if response.status_code != 200:
            raise _SweepError("http_error")
        try:
            body = response.json()
        except ValueError:
            raise _SweepError("invalid_response") from None
        rows = body.get("data") if isinstance(body, dict) else None
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise _SweepError("invalid_response")
        meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
        next_cursor = meta.get("cursor") if isinstance(meta.get("cursor"), str) else None
        total_pages = meta.get("totalPages")
        has_more_pages = (
            isinstance(total_pages, int) and page is not None and page < total_pages
        )
        if trace_id is not None:
            rows = [row for row in rows if row.get("traceId") == trace_id]
        return rows, next_cursor or None, has_more_pages

    async def _expired_candidates(
        self, cutoff: datetime
    ) -> tuple[set[str], int, int, bool]:
        candidates: set[str] = set()
        foreign_trace_ids: set[str] = set()
        scanned = foreign = 0
        cursor: str | None = None
        page = 1
        for _ in range(self._max_pages):
            rows, cursor, more = await self._query(
                start=cutoff - _SCAN_HORIZON, end=cutoff, cursor=cursor, page=page
            )
            scanned += len(rows)
            for row in rows:
                trace_id = row.get("traceId")
                if not _is_own_observation(row):
                    foreign += 1
                    if isinstance(trace_id, str):
                        foreign_trace_ids.add(trace_id)
                elif isinstance(trace_id, str) and _OTEL_TRACE_ID.fullmatch(trace_id):
                    candidates.add(trace_id)
            if cursor is None and not more:
                return candidates - foreign_trace_ids, scanned, foreign, False
            page += 1
        return candidates - foreign_trace_ids, scanned, foreign, True

    async def _queryable(self, trace_id: str, now: datetime) -> bool:
        rows, _, _ = await self._query(
            start=now - _SCAN_HORIZON, end=now + timedelta(hours=1), trace_id=trace_id
        )
        return bool(rows)

    async def sweep(
        self, *, dry_run: bool = False, now: datetime | None = None
    ) -> RetentionSweepReport:
        """Run one sweep. `now` is injectable for tests only; the CLI always uses the clock."""
        current = (now or datetime.now(UTC)).astimezone(UTC)
        days = self.retention_days
        valid = isinstance(days, int) and not isinstance(days, bool) and 1 <= days <= 365
        cutoff = current - timedelta(days=days if valid else 0)
        base = {"retention_days": days, "cutoff": _iso(cutoff), "dry_run": dry_run}
        if not valid:
            return RetentionSweepReport("not_attempted", "invalid_retention", **base)
        if _basic_authorization(self._public_key, self._secret_key) is None:
            return RetentionSweepReport("not_attempted", "credentials_missing", **base)
        if not self.egress.permitted():
            return RetentionSweepReport("not_attempted", "egress_not_permitted", **base)
        counts: dict[str, int] = {}
        try:
            candidates, scanned, foreign, truncated = await self._expired_candidates(cutoff)
            counts |= {"scanned_observations": scanned, "foreign_observations": foreign}
            if truncated:
                # An unseen page may contain a foreign span sharing a candidate trace.
                # A trace-wide delete is safe only after the complete bounded scan.
                return RetentionSweepReport("partial", "budget_exhausted", **base, **counts)
            expired: list[str] = []
            kept = 0
            for trace_id in sorted(candidates):
                recent, _, _ = await self._query(
                    start=cutoff, end=current + timedelta(hours=1), trace_id=trace_id
                )
                if recent:
                    kept += 1
                else:
                    expired.append(trace_id)
            counts |= {"expired_traces": len(expired), "kept_recent_traces": kept}
            if len(expired) > self._max_deletes:
                truncated = True
                expired = expired[: self._max_deletes]
            if dry_run:
                status: SweepStatus = "partial" if truncated else "completed"
                reason: SweepReason = "budget_exhausted" if truncated else "ok"
                return RetentionSweepReport(status, reason, **base, **counts)
            deleted: list[str] = []
            failed = 0
            for trace_id in expired:
                response = await self._request("DELETE", f"{TRACES_PATH}/{trace_id}")
                if 200 <= response.status_code < 300:
                    deleted.append(trace_id)
                else:
                    failed += 1
            counts |= {"deleted_traces": len(deleted), "delete_failed": failed}
            pending = set(deleted)
            started = self._monotonic()
            while pending:
                for trace_id in sorted(pending):
                    if not await self._queryable(trace_id, current):
                        pending.discard(trace_id)
                if not pending or self._monotonic() - started >= self._confirm_seconds:
                    break
                await self._sleep(self._poll_seconds)
            counts |= {
                "confirmed_gone": len(deleted) - len(pending),
                "still_queryable": len(pending),
            }
        except _SweepError as exc:
            return RetentionSweepReport("failed", exc.reason, **base, **counts)
        if failed:
            return RetentionSweepReport("partial", "http_error", **base, **counts)
        if pending:
            return RetentionSweepReport("partial", "delete_unconfirmed", **base, **counts)
        if truncated:
            return RetentionSweepReport("partial", "budget_exhausted", **base, **counts)
        return RetentionSweepReport("completed", "ok", **base, **counts)
