"""Opt-in live check of the Langfuse trace export against a LOCAL self-hosted Langfuse v4.

What it verifies (P1-006C AC1-AC3), with synthetic data only:

* a canary work request runs through the product container with TRACE_BACKEND=langfuse;
  every observation is written locally first and its export receipt is 'exported' only
  after the server acknowledged it;
* each exported span is read back BY ID from the v2 observations API, carries only the
  allowlisted metadata, and neither the canary, the query text, the draft body nor any key
  appears in what Langfuse stored;
* unreported tokens are not turned into measured usage;
* the project retention setting reported by Langfuse equals the requested value;
* a closed loopback port is a failed export, never a success.

Trace deletion and actual expiry are NOT asserted here (asynchronous, not verifiable in one
session); see docs/evidence/llmops.md.

Run explicitly (a skipped run is not evidence):
    RFA_LANGFUSE_LIVE=1 RFA_LANGFUSE_ENV_FILE=/abs/tmp/langfuse-live.env \
    .venv/bin/python -m pytest -q tests/integration/test_langfuse_live.py

The env file lives OUTSIDE the repository and holds only LANGFUSE_BASE_URL (loopback),
LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY and optionally RFA_LANGFUSE_EXPECTED_RETENTION_DAYS.
Values are never printed. RFA_LANGFUSE_EVIDENCE_OUT optionally receives a value-free JSON
summary (IDs are opaque aliases/hex span ids, no keys).
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
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
    "RFA_LANGFUSE_EXPECTED_RETENTION_DAYS",
}
READBACK_SECONDS = 120.0

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
    missing = sorted({"LANGFUSE_BASE_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"} - {
        name for name, value in values.items() if value
    })
    if missing:
        pytest.fail(f"live env file is missing: {missing}")
    return values


def _settings_kwargs(tmp_path: Path, values: dict[str, str], **overrides: Any) -> dict[str, Any]:
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
    return kwargs


def _auth(values: dict[str, str]) -> httpx.BasicAuth:
    return httpx.BasicAuth(values["LANGFUSE_PUBLIC_KEY"], values["LANGFUSE_SECRET_KEY"])


def _record_evidence(section: str, payload: dict[str, Any]) -> None:
    if not EVIDENCE_OUT:
        return
    path = Path(EVIDENCE_OUT)
    current = json.loads(path.read_text()) if path.exists() else {}
    current[section] = payload
    path.write_text(json.dumps(current, indent=2, sort_keys=True, ensure_ascii=False))


async def _read_observation(
    client: httpx.AsyncClient, span_id: str, start: datetime, end: datetime
) -> dict[str, Any] | None:
    response = await client.get(
        "/api/public/v2/observations",
        params={
            "filter": json.dumps(
                [{"type": "string", "column": "id", "operator": "=", "value": span_id}]
            ),
            "fromStartTime": start.isoformat().replace("+00:00", "Z"),
            "toStartTime": end.isoformat().replace("+00:00", "Z"),
            "fields": "core,basic,time,metadata,usage,io",
            "limit": 10,
        },
    )
    response.raise_for_status()
    rows = [row for row in response.json().get("data", []) if row.get("id") == span_id]
    return rows[0] if rows else None


async def test_live_export_is_read_back_by_id_without_canary_or_secret(
    tmp_path: Path, principal
) -> None:
    values = _live_values()
    container = build_container(Settings(**_settings_kwargs(tmp_path, values)))
    started = datetime.now(UTC) - timedelta(minutes=5)
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

    ended = datetime.now(UTC) + timedelta(minutes=5)
    forbidden = (
        CANARY,
        QUERY_TEXT,
        result.run_id,
        result.draft.content,
        principal.user_id,
        values["LANGFUSE_PUBLIC_KEY"],
        values["LANGFUSE_SECRET_KEY"],
    )
    found: dict[str, dict[str, Any]] = {}
    deadline = time.monotonic() + READBACK_SECONDS
    async with httpx.AsyncClient(
        base_url=values["LANGFUSE_BASE_URL"], auth=_auth(values), timeout=10
    ) as client:
        while time.monotonic() < deadline and len(found) < len(receipts):
            for receipt in receipts:
                if receipt.otel_span_id in found:
                    continue
                row = await _read_observation(client, receipt.otel_span_id, started, ended)
                if row is not None:
                    found[receipt.otel_span_id] = row
            if len(found) < len(receipts):
                await asyncio.sleep(3)
    elapsed = READBACK_SECONDS - max(0.0, deadline - time.monotonic())
    assert len(found) == len(receipts), f"read back {len(found)}/{len(receipts)} by id"

    stored = json.dumps(list(found.values()), ensure_ascii=False)
    assert not [value for value in forbidden if value in stored]
    metadata_keys: set[str] = set()
    usage_seen: set[str] = set()
    for receipt in receipts:
        row = found[receipt.otel_span_id]
        assert row["traceId"] == receipt.otel_trace_id
        metadata = row.get("metadata") or {}
        assert metadata.get("observation_id") == receipt.observation_id
        assert metadata.get("token_usage") == "not_reported"
        metadata_keys |= set(metadata)
        usage = row.get("usageDetails") or {}
        usage_seen |= {key for key, value in usage.items() if value}
        assert row.get("input") in (None, "", {}) and row.get("output") in (None, "", {})
    allowed_metadata = {
        key.removeprefix(METADATA_PREFIX)
        for key in ALLOWED_ATTRIBUTE_KEYS
        if key.startswith(METADATA_PREFIX)
    }
    # Langfuse may add its own bookkeeping keys (resource/scope attributes); record them.
    extra_metadata = sorted(metadata_keys - allowed_metadata)
    assert not usage_seen, f"unreported tokens became measured usage: {sorted(usage_seen)}"
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
            "metadata_keys_outside_allowlist": extra_metadata,
            "canary_or_secret_found": False,
            "usage_keys_with_values": sorted(usage_seen),
            "sample_row_keys": sorted(next(iter(found.values())).keys()),
        },
    )


async def test_live_project_retention_matches_requested_days(tmp_path: Path) -> None:
    values = _live_values()
    expected = values.get("RFA_LANGFUSE_EXPECTED_RETENTION_DAYS")
    if not expected:
        pytest.fail("RFA_LANGFUSE_EXPECTED_RETENTION_DAYS is required for the retention check")
    async with httpx.AsyncClient(
        base_url=values["LANGFUSE_BASE_URL"], auth=_auth(values), timeout=10
    ) as client:
        response = await client.get("/api/public/projects")
    response.raise_for_status()
    projects = response.json().get("data", [])
    assert len(projects) == 1
    project = projects[0]
    assert "retentionDays" in project, f"no retentionDays in {sorted(project)}"
    assert project["retentionDays"] == int(expected)
    _record_evidence(
        "retention",
        {
            "requested_days": int(expected),
            "reported_retention_days": project["retentionDays"],
            "project_keys": sorted(project),
            "expiry_observed": "not_verified",
        },
    )


async def test_live_closed_loopback_port_is_a_failed_export(tmp_path: Path, principal) -> None:
    values = _live_values()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    container = build_container(
        Settings(
            **_settings_kwargs(
                tmp_path, values, langfuse_base_url=f"http://127.0.0.1:{closed_port}"
            )
        )
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
    assert receipts and {(r.status, r.reason) for r in receipts} == {
        ("failed", "connection_error")
    }
    _record_evidence(
        "closed_port",
        {"receipts": len(receipts), "statuses": ["failed:connection_error"]},
    )
