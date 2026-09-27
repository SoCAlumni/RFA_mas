"""Opt-in live check of the Langfuse trace export against a LOCAL self-hosted Langfuse v4.

What it verifies (P1-006C AC1-AC3), with synthetic data only:

* a canary work request runs through the product container with TRACE_BACKEND=langfuse;
  every observation is written locally first and its export receipt is 'exported' only
  after the server acknowledged it;
* each exported span is read back BY ID from the v2 observations API, carries only the
  allowlisted metadata (plus Langfuse's copies of our fixed resource/scope constants), and
  neither the canary, the query text, the draft body nor any key is stored;
* unreported tokens are not sent: usageDetails stays empty and metadata says
  token_usage=not_reported (Langfuse's own aggregate columns render that as 0);
* DELETE /api/public/traces/{id} removes the trace from the v2 query within a bound;
* the community (OSS) project reports no server-side retention (the data-retention
  entitlement is Enterprise-only), so retention is enforced by the app's own sweep;
* P1-006F: `LangfuseRetentionSweeper` deletes only this app's traces older than
  TRACE_RETENTION_DAYS by ID and confirms by query; a foreign trace is never touched;
* a closed loopback port is a failed export, never a success.

Expiry is exercised with an injected clock (real clock: nothing deleted; clock + 8 days:
own traces deleted). Langfuse's own nightly expiry does not exist on the community edition.

Run explicitly (a skipped run is not evidence):
    RFA_LANGFUSE_LIVE=1 RFA_LANGFUSE_ENV_FILE=/abs/tmp/langfuse-live.env \
    .venv/bin/python -m pytest -q tests/integration/test_langfuse_live.py

The env file lives OUTSIDE the repository and holds only LANGFUSE_BASE_URL (loopback),
LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY.
Values are never printed. RFA_LANGFUSE_EVIDENCE_OUT optionally receives a value-free JSON
summary (opaque aliases and hex span ids only, no keys).
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from rfa_mas.adapters.langfuse import ALLOWED_ATTRIBUTE_KEYS, METADATA_PREFIX
from rfa_mas.bootstrap import build_container
from rfa_mas.contracts import Audience, DomainId, DraftTarget, WorkRequest, WorkStatus
from rfa_mas.settings import Settings

LIVE_ENABLED = os.environ.get("RFA_LANGFUSE_LIVE") == "1"
ENV_FILE = os.environ.get("RFA_LANGFUSE_ENV_FILE")
EVIDENCE_OUT = os.environ.get("RFA_LANGFUSE_EVIDENCE_OUT")
REPO_ROOT = Path(__file__).resolve().parents[2]
CANARY = "PRIVATE_CANARY_LFLIVE7"
QUERY_TEXT = "TRIV3 공개 트랙"
ALLOWED_NAMES = {
    "LANGFUSE_BASE_URL",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
}
# Langfuse copies our own fixed OTel constants into observation metadata.
LANGFUSE_BOOKKEEPING = {
    "attributes.langfuse.trace.name",
    "resourceAttributes.service.name",
    "scope.name",
    "scope.version",
}
READBACK_SECONDS = 120.0
DELETE_SECONDS = 120.0

pytestmark = pytest.mark.skipif(
    not LIVE_ENABLED or not ENV_FILE,
    reason="opt-in live Langfuse self-host check (RFA_LANGFUSE_LIVE=1 and "
    "RFA_LANGFUSE_ENV_FILE); skipped run is not evidence",
)


def _live_values() -> dict[str, str]:
    path = Path(ENV_FILE or "").resolve()
    if path.is_relative_to(REPO_ROOT):
        pytest.fail("live env file must live outside the repository (never the repo .env)")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        name, sep, value = line.strip().partition("=")
        if sep and name in ALLOWED_NAMES:
            values[name] = value
    required = {"LANGFUSE_BASE_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"}
    missing = sorted(required - {name for name, value in values.items() if value})
    if missing:
        pytest.fail(f"live env file is missing: {missing}")
    return values


def _settings(tmp_path: Path, values: dict[str, str], **overrides: Any) -> Settings:
    kwargs: dict[str, Any] = {
        "_env_file": None,
        "database_url": f"sqlite:///{tmp_path / 'rfa.db'}",
        "trace_dir": tmp_path / "traces",
        "trace_backend": "langfuse",
        "langfuse_base_url": values["LANGFUSE_BASE_URL"],
        "langfuse_public_key": values["LANGFUSE_PUBLIC_KEY"],
        "langfuse_secret_key": values["LANGFUSE_SECRET_KEY"],
        "langfuse_export_enabled": True,
    }
    kwargs.update(overrides)
    return Settings(**kwargs)


def _client(values: dict[str, str]) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=values["LANGFUSE_BASE_URL"],
        auth=httpx.BasicAuth(values["LANGFUSE_PUBLIC_KEY"], values["LANGFUSE_SECRET_KEY"]),
        timeout=10,
    )


def _record_evidence(section: str, payload: dict[str, Any]) -> None:
    if not EVIDENCE_OUT:
        return
    path = Path(EVIDENCE_OUT)
    current = json.loads(path.read_text()) if path.exists() else {}
    current[section] = payload
    path.write_text(json.dumps(current, indent=2, sort_keys=True, ensure_ascii=False))


def _window() -> dict[str, str]:
    now = datetime.now(UTC)
    return {
        "fromStartTime": (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "toStartTime": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
    }


async def _observations(client: httpx.AsyncClient, column: str, value: str) -> list[dict]:
    response = await client.get(
        "/api/public/v2/observations",
        params={
            "filter": json.dumps(
                [{"type": "string", "column": column, "operator": "=", "value": value}]
            ),
            "fields": "core,basic,time,metadata,usage,io",
            "limit": 100,
            **_window(),
        },
    )
    response.raise_for_status()
    return [row for row in response.json().get("data", []) if row.get(column) == value]


async def _export_canary_run(tmp_path: Path, values: dict[str, str], principal):
    container = build_container(_settings(tmp_path, values))
    await container.startup()
    try:
        result = await container.service.run(
            WorkRequest(
                query=f"{QUERY_TEXT} {CANARY}",
                domain_id=DomainId.TRIV3,
                target=DraftTarget(audience=Audience.PUBLIC),
                request_id=CANARY,
                trace_id=CANARY,
                agent_id=CANARY,
            ),
            principal,
        )
        ledger = await container.service.observations.ledger(result.run_id, principal)
    finally:
        await container.shutdown()
    assert result.status == WorkStatus.COMPLETED, result.errors
    receipts = container.trace.export_receipts()
    rows = ledger.observations
    assert rows and {r.observation_id for r in receipts} == {r.observation_id for r in rows}
    assert {(r.status, r.reason) for r in receipts} == {("exported", "ok")}
    return result, rows, receipts


async def _read_back(client, receipts) -> tuple[dict[str, dict[str, Any]], float]:
    found: dict[str, dict[str, Any]] = {}
    started = time.monotonic()
    while time.monotonic() - started < READBACK_SECONDS and len(found) < len(receipts):
        for receipt in receipts:
            if receipt.otel_span_id not in found:
                rows = await _observations(client, "id", receipt.otel_span_id)
                if rows:
                    found[receipt.otel_span_id] = rows[0]
        if len(found) < len(receipts):
            await asyncio.sleep(3)
    return found, time.monotonic() - started


async def test_live_export_is_read_back_by_id_without_canary_or_secret(
    tmp_path: Path, principal
) -> None:
    values = _live_values()
    result, rows, receipts = await _export_canary_run(tmp_path, values, principal)
    async with _client(values) as client:
        found, elapsed = await _read_back(client, receipts)
    assert len(found) == len(receipts), f"read back {len(found)}/{len(receipts)} by id"

    forbidden = (
        CANARY,
        QUERY_TEXT,
        result.run_id,
        result.draft.content,
        principal.user_id,
        values["LANGFUSE_PUBLIC_KEY"],
        values["LANGFUSE_SECRET_KEY"],
    )
    stored = json.dumps(list(found.values()), ensure_ascii=False)
    assert not [value for value in forbidden if value in stored]
    allowed_metadata = {
        key.removeprefix(METADATA_PREFIX)
        for key in ALLOWED_ATTRIBUTE_KEYS
        if key.startswith(METADATA_PREFIX)
    }
    metadata_keys: set[str] = set()
    for receipt in receipts:
        row = found[receipt.otel_span_id]
        assert row["traceId"] == receipt.otel_trace_id
        metadata = row.get("metadata") or {}
        assert metadata.get("observation_id") == receipt.observation_id
        assert metadata.get("token_usage") == "not_reported"
        metadata_keys |= set(metadata)
        # No usage was sent; Langfuse's derived columns show 0, usageDetails stays empty.
        assert row.get("usageDetails") in ({}, None)
        assert row.get("input") in (None, "", {}) and row.get("output") in (None, "", {})
    unexpected = sorted(metadata_keys - allowed_metadata - LANGFUSE_BOOKKEEPING)
    assert not unexpected, f"unexpected stored metadata keys: {unexpected}"
    sample = next(iter(found.values()))
    _record_evidence(
        "export_readback",
        {
            "observations": len(rows),
            "exported": len(receipts),
            "read_back_by_id": len(found),
            "readback_seconds": round(elapsed, 1),
            "otel_trace_ids": sorted({r.otel_trace_id for r in receipts}),
            "sample_span_ids": sorted(found)[:3],
            "boundaries": sorted({row.get("name", "") for row in found.values()}),
            "metadata_keys_outside_allowlist": sorted(metadata_keys - allowed_metadata),
            "canary_or_secret_found": False,
            "langfuse_usage_columns_sample": {
                key: sample.get(key)
                for key in ("usageDetails", "inputUsage", "outputUsage", "totalUsage")
            },
        },
    )


async def test_live_delete_by_trace_id_removes_it_from_the_query(
    tmp_path: Path, principal
) -> None:
    values = _live_values()
    _, _, receipts = await _export_canary_run(tmp_path, values, principal)
    (trace_id,) = {r.otel_trace_id for r in receipts}
    async with _client(values) as client:
        found, _ = await _read_back(client, receipts)
        assert len(found) == len(receipts)
        deleted = await client.delete(f"/api/public/traces/{trace_id}")
        assert deleted.status_code == 200
        started = time.monotonic()
        remaining = len(found)
        while remaining and time.monotonic() - started < DELETE_SECONDS:
            await asyncio.sleep(5)
            remaining = len(await _observations(client, "traceId", trace_id))
        elapsed = time.monotonic() - started
    assert remaining == 0, f"{remaining} observations still queryable after {elapsed:.0f}s"
    _record_evidence(
        "delete_by_id",
        {
            "otel_trace_id": trace_id,
            "observations_before": len(found),
            "delete_status": deleted.status_code,
            "gone_from_v2_query_after_seconds": round(elapsed, 1),
            "raw_blob_removal": "not_verified_by_test",
        },
    )


async def test_live_community_project_has_no_server_retention_so_sweep_is_required() -> None:
    """Community Langfuse keeps traces indefinitely; the app's sweep is the retention."""
    values = _live_values()
    async with _client(values) as client:
        response = await client.get("/api/public/projects")
    response.raise_for_status()
    projects = response.json().get("data", [])
    assert len(projects) == 1
    project = projects[0]
    assert project.get("retentionDays") in (None, 0), (
        "server-side retention is reported; re-check whether the app sweep is still needed"
    )
    _record_evidence(
        "retention",
        {
            "project_keys": sorted(project),
            "reported_retention_days": project.get("retentionDays"),
            "server_retention": "not_available_on_community_edition",
            "enforced_by": "rfa langfuse-retention (P1-006F)",
        },
    )


async def test_live_closed_loopback_port_is_a_failed_export(tmp_path: Path, principal) -> None:
    values = _live_values()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    container = build_container(
        _settings(tmp_path, values, langfuse_base_url=f"http://127.0.0.1:{closed_port}")
    )
    await container.startup()
    try:
        result = await container.service.run(
            WorkRequest(
                query=QUERY_TEXT,
                domain_id=DomainId.TRIV3,
                target=DraftTarget(audience=Audience.PUBLIC),
            ),
            principal,
        )
    finally:
        await container.shutdown()
    assert result.status == WorkStatus.COMPLETED, result.errors
    receipts = container.trace.export_receipts()
    assert receipts
    assert {(r.status, r.reason) for r in receipts} == {("failed", "connection_error")}
    _record_evidence(
        "closed_port",
        {"receipts": len(receipts), "statuses": ["failed:connection_error"]},
    )


def _otlp_foreign_span(trace_id: str, span_id: str) -> dict[str, Any]:
    now_ns = time.time_ns()
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": "other-app"}}]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "other-app"},
                        "spans": [
                            {
                                "traceId": trace_id,
                                "spanId": span_id,
                                "name": "other.work",
                                "kind": 1,
                                "startTimeUnixNano": str(now_ns - 1_000_000),
                                "endTimeUnixNano": str(now_ns),
                                "attributes": [],
                                "status": {"code": 1},
                            }
                        ],
                    }
                ],
            }
        ]
    }


async def _wait_queryable(client, trace_id: str, *, present: bool) -> float:
    started = time.monotonic()
    while time.monotonic() - started < READBACK_SECONDS:
        rows = await _observations(client, "traceId", trace_id)
        if bool(rows) == present:
            return time.monotonic() - started
        await asyncio.sleep(3)
    pytest.fail(f"trace {'never appeared' if present else 'still queryable'} within bound")


async def test_live_retention_sweep_deletes_only_expired_own_traces(
    tmp_path: Path, principal
) -> None:
    """P1-006F: community Langfuse has no retention, so the app's own sweep enforces it.

    Real clock: fresh traces are inside retention and nothing is deleted. Clock advanced by
    retention+1 days (injected, test-only): the app's traces are deleted by ID and confirmed
    gone by query, while a foreign app's trace in the same project is never touched.
    """
    from rfa_mas.adapters.langfuse import LangfuseEgress, LangfuseRetentionSweeper

    values = _live_values()
    _, _, receipts = await _export_canary_run(tmp_path, values, principal)
    (own_trace,) = {r.otel_trace_id for r in receipts}
    foreign_trace, foreign_span = os.urandom(16).hex(), os.urandom(8).hex()
    settings = _settings(tmp_path, values, trace_retention_days=7)
    async with _client(values) as client:
        await _read_back(client, receipts)
        sent = await client.post(
            "/api/public/otel/v1/traces",
            json=_otlp_foreign_span(foreign_trace, foreign_span),
            headers={"x-langfuse-ingestion-version": "4"},
        )
        assert 200 <= sent.status_code < 300
        await _wait_queryable(client, own_trace, present=True)
        await _wait_queryable(client, foreign_trace, present=True)

        def sweeper() -> LangfuseRetentionSweeper:
            return LangfuseRetentionSweeper(
                client=client,
                egress=LangfuseEgress(values["LANGFUSE_BASE_URL"], True),
                public_key=settings.langfuse_public_key,
                secret_key=settings.langfuse_secret_key,
                retention_days=settings.trace_retention_days,
            )

        real_now = datetime.now(UTC)
        inside = await sweeper().sweep(now=real_now)
        assert (inside.status, inside.expired_traces, inside.deleted_traces) == ("completed", 0, 0)
        assert await _observations(client, "traceId", own_trace)

        dry = await sweeper().sweep(now=real_now + timedelta(days=8), dry_run=True)
        assert dry.status == "completed" and dry.expired_traces >= 1 and dry.deleted_traces == 0
        assert dry.foreign_observations >= 1
        assert await _observations(client, "traceId", own_trace)

        started = time.monotonic()
        swept = await sweeper().sweep(now=real_now + timedelta(days=8))
        sweep_seconds = time.monotonic() - started
        assert (swept.status, swept.reason) == ("completed", "ok"), swept
        assert swept.deleted_traces == swept.expired_traces == dry.expired_traces
        assert swept.confirmed_gone == swept.deleted_traces and swept.still_queryable == 0
        assert not await _observations(client, "traceId", own_trace)
        assert await _observations(client, "traceId", foreign_trace)
    _record_evidence(
        "retention_sweep",
        {
            "retention_days": 7,
            "real_clock": {"expired": inside.expired_traces, "deleted": inside.deleted_traces},
            "clock_plus_8_days_dry_run": {
                "expired": dry.expired_traces,
                "deleted": dry.deleted_traces,
                "foreign_observations": dry.foreign_observations,
            },
            "clock_plus_8_days": {
                "expired": swept.expired_traces,
                "deleted": swept.deleted_traces,
                "confirmed_gone": swept.confirmed_gone,
                "still_queryable": swept.still_queryable,
                "sweep_seconds": round(sweep_seconds, 1),
            },
            "own_trace_after": "gone",
            "foreign_trace_after": "kept",
            "own_otel_trace_id": own_trace,
        },
    )


def test_live_retention_cli_dry_run_uses_settings_and_prints_counts_only(tmp_path: Path) -> None:
    values = _live_values()
    env_file = tmp_path / "retention.env"
    env_file.write_text(
        "\n".join(
            [
                f"DATABASE_URL=sqlite:///{tmp_path / 'rfa.db'}",
                f"TRACE_DIR={tmp_path / 'traces'}",
                f"LANGFUSE_BASE_URL={values['LANGFUSE_BASE_URL']}",
                f"LANGFUSE_PUBLIC_KEY={values['LANGFUSE_PUBLIC_KEY']}",
                f"LANGFUSE_SECRET_KEY={values['LANGFUSE_SECRET_KEY']}",
                "LANGFUSE_EXPORT_ENABLED=true",
                "TRACE_RETENTION_DAYS=7",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    completed = subprocess.run(
        [sys.executable, "-m", "rfa_mas.cli", "--env-file", str(env_file),
         "langfuse-retention", "--dry-run"],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    output = completed.stdout + completed.stderr
    assert values["LANGFUSE_PUBLIC_KEY"] not in output
    assert values["LANGFUSE_SECRET_KEY"] not in output
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    assert completed.returncode == 0, report
    assert (report["status"], report["retention_days"], report["dry_run"]) == (
        "completed",
        7,
        True,
    )
    assert report["deleted_traces"] == 0
    _record_evidence("retention_cli", {"exit_code": completed.returncode, "report": report})
