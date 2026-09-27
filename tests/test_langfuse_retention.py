"""P1-006F: self-run Langfuse retention sweep against an in-process fake Langfuse.

Community (OSS) Langfuse ignores project retention without the Enterprise entitlement, so
the app deletes its own expired traces by ID. These tests use httpx.MockTransport only.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from rfa_mas.adapters.langfuse import (
    EXPORT_SCHEMA,
    SERVICE_NAME,
    LangfuseEgress,
    LangfuseRetentionSweeper,
)
from rfa_mas.bootstrap import run_langfuse_retention
from rfa_mas.settings import Settings

NOW = datetime(2026, 9, 27, 3, 0, tzinfo=UTC)
PUBLIC = "pk-lf-known-fake-public"
SECRET = "sk-lf-known-fake-secret"
BASE = "http://127.0.0.1:3000"


def _tid(n: int) -> str:
    return f"{n:032x}"


def _own(obs_id: str, trace: str, age: timedelta) -> dict:
    return {
        "id": obs_id,
        "traceId": trace,
        "startTime": NOW - age,
        "metadata": {"schema": EXPORT_SCHEMA, "resourceAttributes.service.name": SERVICE_NAME},
    }


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class FakeLangfuse:
    """Minimal v2 observations query + DELETE /api/public/traces/{id} with cursor paging."""

    def __init__(self, rows: list[dict], *, sticky: set[str] = frozenset(), status: int = 200):
        self.rows = rows
        self.sticky = set(sticky)  # acknowledged DELETE, but still queryable
        self.status = status
        self.requests: list[httpx.Request] = []
        self.deleted: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"message": "server text is never kept"})
        path = request.url.path
        if request.method == "DELETE" and path.startswith("/api/public/traces/"):
            trace = path.rsplit("/", 1)[1]
            self.deleted.append(trace)
            if trace not in self.sticky:
                self.rows = [row for row in self.rows if row["traceId"] != trace]
            return httpx.Response(200, json={"message": "Trace deleted successfully"})
        assert request.method == "GET" and path == "/api/public/v2/observations"
        params = request.url.params
        start, end = _parse(params["fromStartTime"]), _parse(params["toStartTime"])
        rows = [row for row in self.rows if start <= row["startTime"] <= end]
        if "filter" in params:
            (condition,) = json.loads(params["filter"])
            rows = [row for row in rows if row[condition["column"]] == condition["value"]]
        rows.sort(key=lambda row: row["id"])
        offset = int(params.get("cursor", "0"))
        limit = int(params["limit"])
        page = rows[offset : offset + limit]
        more = offset + limit < len(rows)
        data = [
            {**row, "startTime": row["startTime"].isoformat().replace("+00:00", "Z")}
            for row in page
        ]
        return httpx.Response(
            200, json={"data": data, "meta": {"cursor": str(offset + limit) if more else None}}
        )


def _sweeper(fake: FakeLangfuse, **overrides) -> tuple[LangfuseRetentionSweeper, httpx.AsyncClient]:
    client = httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(fake.handler))
    clock = {"t": 0.0}

    async def sleep(seconds: float) -> None:
        clock["t"] += seconds

    options = {
        "client": client,
        "egress": LangfuseEgress(BASE, True),
        "public_key": SecretStr(PUBLIC),
        "secret_key": SecretStr(SECRET),
        "retention_days": 7,
        "page_limit": 2,
        "confirm_seconds": 30,
        "poll_seconds": 5,
        "sleep": sleep,
        "monotonic": lambda: clock["t"],
    }
    options.update(overrides)
    return LangfuseRetentionSweeper(**options), client


def _mixed_rows() -> list[dict]:
    foreign_meta = {"schema": "someone-else", "resourceAttributes.service.name": "other"}
    return [
        # expired own trace, 3 observations (paged across cursor pages)
        _own("a1", _tid(1), timedelta(days=9)),
        _own("a2", _tid(1), timedelta(days=9, minutes=-1)),
        _own("a3", _tid(1), timedelta(days=8)),
        # second expired own trace
        _own("b1", _tid(2), timedelta(days=30)),
        # recent own trace
        _own("c1", _tid(3), timedelta(days=1)),
        # own trace spanning the cutoff: old start, newest observation inside retention
        _own("d1", _tid(4), timedelta(days=8)),
        _own("d2", _tid(4), timedelta(days=6)),
        # foreign traces: never ours, even when old
        {"id": "e1", "traceId": _tid(5), "startTime": NOW - timedelta(days=40),
         "metadata": foreign_meta},
        {"id": "f1", "traceId": _tid(6), "startTime": NOW - timedelta(days=40),
         "metadata": {"schema": EXPORT_SCHEMA, "resourceAttributes.service.name": "other"}},
        # malformed ID is never put into a URL path
        _own("g1", "../../api/public/projects", timedelta(days=40)),
    ]


async def test_sweep_deletes_only_own_expired_traces_and_confirms_by_query() -> None:
    fake = FakeLangfuse(_mixed_rows())
    sweeper, client = _sweeper(fake)
    async with client:
        report = await sweeper.sweep(now=NOW)
    assert (report.status, report.reason) == ("completed", "ok")
    assert sorted(fake.deleted) == [_tid(1), _tid(2)]
    assert report.expired_traces == 2 and report.deleted_traces == 2
    assert report.confirmed_gone == 2 and report.still_queryable == 0
    assert report.kept_recent_traces == 1  # the trace spanning the cutoff
    assert report.foreign_observations == 2  # the malformed own-schema row is just ignored
    remaining = {row["traceId"] for row in fake.rows}
    assert {_tid(3), _tid(4), _tid(5), _tid(6)} <= remaining
    allowed_deletes = {f"/api/public/traces/{_tid(n)}" for n in (1, 2)}
    assert all(
        request.method == "GET" or request.url.path in allowed_deletes
        for request in fake.requests
    )
    assert report.cutoff == "2026-09-20T03:00:00Z"


async def test_dry_run_sends_no_delete() -> None:
    fake = FakeLangfuse(_mixed_rows())
    sweeper, client = _sweeper(fake)
    async with client:
        report = await sweeper.sweep(now=NOW, dry_run=True)
    assert (report.status, report.expired_traces, report.deleted_traces) == ("completed", 2, 0)
    assert not [r for r in fake.requests if r.method != "GET"] and not fake.deleted


@pytest.mark.parametrize(
    ("egress", "public", "reason"),
    [
        (LangfuseEgress(BASE, False), PUBLIC, "egress_not_permitted"),
        (LangfuseEgress("https://cloud.langfuse.example", True), PUBLIC, "egress_not_permitted"),
        (LangfuseEgress(BASE, True), "", "credentials_missing"),
    ],
)
async def test_no_permission_or_keys_means_zero_requests(egress, public, reason) -> None:
    fake = FakeLangfuse(_mixed_rows())
    sweeper, client = _sweeper(fake, egress=egress, public_key=SecretStr(public))
    async with client:
        report = await sweeper.sweep(now=NOW)
    assert (report.status, report.reason) == ("not_attempted", reason)
    assert fake.requests == []


@pytest.mark.parametrize("days", [0, 366, True, "7"])
async def test_invalid_retention_is_not_attempted(days) -> None:
    fake = FakeLangfuse(_mixed_rows())
    sweeper, client = _sweeper(fake, retention_days=days)
    async with client:
        report = await sweeper.sweep(now=NOW)
    assert (report.status, report.reason) == ("not_attempted", "invalid_retention")
    assert fake.requests == []


async def test_acknowledged_but_still_queryable_is_partial_never_gone() -> None:
    fake = FakeLangfuse(_mixed_rows(), sticky={_tid(2)})
    sweeper, client = _sweeper(fake)
    async with client:
        report = await sweeper.sweep(now=NOW)
    assert (report.status, report.reason) == ("partial", "delete_unconfirmed")
    assert (report.deleted_traces, report.confirmed_gone, report.still_queryable) == (2, 1, 1)


@pytest.mark.parametrize(
    ("status", "reason"), [(401, "auth_error"), (500, "http_error"), (503, "http_error")]
)
async def test_server_errors_are_failed_with_fixed_codes(status, reason) -> None:
    fake = FakeLangfuse(_mixed_rows(), status=status)
    sweeper, client = _sweeper(fake)
    async with client:
        report = await sweeper.sweep(now=NOW)
    assert (report.status, report.reason, report.deleted_traces) == ("failed", reason, 0)


async def test_connection_failure_is_failed_not_success() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(refuse))
    sweeper = LangfuseRetentionSweeper(
        client=client,
        egress=LangfuseEgress(BASE, True),
        public_key=SecretStr(PUBLIC),
        secret_key=SecretStr(SECRET),
        retention_days=7,
    )
    async with client:
        report = await sweeper.sweep(now=NOW)
    assert (report.status, report.reason) == ("failed", "connection_error")


async def test_page_budget_exhaustion_is_partial() -> None:
    fake = FakeLangfuse(_mixed_rows())
    sweeper, client = _sweeper(fake, max_pages=1)
    async with client:
        report = await sweeper.sweep(now=NOW, dry_run=True)
    assert (report.status, report.reason) == ("partial", "budget_exhausted")


async def test_report_and_settings_wiring_hold_no_keys_or_ids(tmp_path: Path) -> None:
    fake = FakeLangfuse(_mixed_rows())
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'rfa.db'}",
        trace_dir=tmp_path / "traces",
        langfuse_base_url=BASE,
        langfuse_public_key=PUBLIC,
        langfuse_secret_key=SECRET,
        langfuse_export_enabled=True,
        trace_retention_days=3,
    )
    report = await run_langfuse_retention(
        settings, dry_run=True, transport=httpx.MockTransport(fake.handler)
    )
    assert report.retention_days == 3 and report.status == "completed"
    rendered = json.dumps(report.as_dict())
    assert not [value for value in (PUBLIC, SECRET, _tid(1), _tid(2)) if value in rendered]
    auth = fake.requests[0].headers["Authorization"]
    assert auth.startswith("Basic ") and PUBLIC not in str(fake.requests[0].url)


def test_retention_days_setting_is_bounded(tmp_path: Path) -> None:
    for bad in (0, 366):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, trace_retention_days=bad)
    assert Settings(_env_file=None).trace_retention_days == 7


async def test_missing_service_marker_and_mixed_foreign_trace_are_never_deleted():
    own = _own("a", _tid(11), timedelta(days=9))
    foreign = {**_own("z", _tid(11), timedelta(days=8)), "metadata": {"schema": "other-app"}}
    missing = _own("m", _tid(12), timedelta(days=9))
    del missing["metadata"]["resourceAttributes.service.name"]
    fake = FakeLangfuse([own, foreign, missing])
    sweeper, client = _sweeper(fake, page_limit=1)
    async with client:
        report = await sweeper.sweep(now=NOW)
    assert report.status == "completed" and report.expired_traces == 0
    assert fake.deleted == [] and len(fake.rows) == 3


async def test_incomplete_scan_never_deletes_even_in_write_mode():
    fake = FakeLangfuse(_mixed_rows())
    sweeper, client = _sweeper(fake, max_pages=1)
    async with client:
        report = await sweeper.sweep(now=NOW)
    assert (report.status, report.reason) == ("partial", "budget_exhausted")
    assert report.deleted_traces == 0 and fake.deleted == []
