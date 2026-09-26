from __future__ import annotations

import base64
import io
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from rfa_mas import bootstrap
from rfa_mas.adapters.langfuse import (
    ALLOWED_ATTRIBUTE_KEYS,
    METADATA_PREFIX,
    LangfuseEgress,
    LangfuseExportTrace,
    LangfuseOtlpExporter,
    TraceExportReceipt,
)
from rfa_mas.adapters.local import LocalJsonlTrace
from rfa_mas.bootstrap import build_container
from rfa_mas.cli import _doctor
from rfa_mas.contracts import (
    Audience,
    DomainId,
    DraftTarget,
    ExecutionContext,
    ExecutionMode,
    ObservationRecord,
    TraceEvent,
    VersionReferences,
    WorkRequest,
    WorkStatus,
)
from rfa_mas.errors import BackendNotImplementedError, ConfigurationError, RfaError
from rfa_mas.security import RedactingLogFilter, SecretRedactor
from rfa_mas.settings import Settings

KNOWN_FAKE_SECRET = "known-fake-secret-for-redaction-tests-only"


def _settings(**overrides: Any) -> Settings:
    """Build settings from declared defaults without reading a developer's environment."""
    values = {
        name: field.get_default(call_default_factory=True)
        for name, field in Settings.model_fields.items()
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_known_fake_secret_is_absent_from_log_output() -> None:
    settings = _settings(nvidia_api_key=KNOWN_FAKE_SECRET)
    redactor = SecretRedactor(settings.secret_values())
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.addFilter(RedactingLogFilter(redactor))
    logger = logging.getLogger("rfa_mas.tests.secret_redaction")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        logger.error("authorization=%s nested=%s", KNOWN_FAKE_SECRET, {"token": KNOWN_FAKE_SECRET})
    finally:
        logger.removeHandler(handler)
        handler.close()

    rendered = output.getvalue()
    assert KNOWN_FAKE_SECRET not in rendered
    assert "[REDACTED]" in rendered


def test_unregistered_credentials_are_redacted_from_structured_and_bearer_logs() -> None:
    unregistered_bearer = "unregistered-bearer-value-for-test"
    unregistered_token = "unregistered-token-value-for-test"
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.addFilter(RedactingLogFilter(SecretRedactor()))
    logger = logging.getLogger("rfa_mas.tests.unregistered_secret_redaction")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        logger.error(
            "headers=%s raw=%s metrics=%s",
            {
                "Authorization": f"Bearer {unregistered_bearer}",
                "x-api-token": unregistered_token,
            },
            f"Bearer {unregistered_bearer}",
            {"token_count": 17, "latency_ms": 23, "ok": True},
        )
        logger.error(
            {
                "clientSecret": unregistered_token,
                "retry_count": 2,
            }
        )
    finally:
        logger.removeHandler(handler)
        handler.close()

    rendered = output.getvalue()
    assert unregistered_bearer not in rendered
    assert unregistered_token not in rendered
    assert rendered.count("[REDACTED]") >= 4
    assert "'token_count': 17" in rendered
    assert "'latency_ms': 23" in rendered
    assert "'retry_count': 2" in rendered


@pytest.mark.asyncio
async def test_known_fake_secret_is_absent_from_trace_output(tmp_path: Path) -> None:
    settings = _settings(nvidia_api_key=KNOWN_FAKE_SECRET)
    trace = LocalJsonlTrace(
        tmp_path / "traces",
        SecretRedactor(settings.secret_values()),
    )

    with pytest.raises(RfaError, match="recorder"):
        await trace.emit(
            event="redaction_test",
            request_id="req_test",
            trace_id="trace_test",
            run_id="run_test",
            status="completed",
            metadata={
                "authorization": f"Bearer {KNOWN_FAKE_SECRET}",
                "nested": {"token": KNOWN_FAKE_SECRET},
            },
        )

    assert not (tmp_path / "traces").exists()


@pytest.mark.asyncio
async def test_unregistered_credentials_are_redacted_from_trace_without_losing_metrics(
    tmp_path: Path,
) -> None:
    unregistered_bearer = "trace-bearer-not-in-settings"
    unregistered_token = "trace-token-not-in-settings"
    trace = LocalJsonlTrace(tmp_path / "traces", SecretRedactor())

    with pytest.raises(RfaError, match="recorder"):
        await trace.emit(
            event="unregistered_redaction_test",
            request_id="req_test",
            trace_id="trace_test",
            run_id="run_test",
            status="completed",
            metadata={
                "Authorization": f"Bearer {unregistered_bearer}",
                "apiToken": unregistered_token,
                "metrics": {"token_count": 31, "latency_ms": 7, "ok": True},
            },
        )

    # Legacy arbitrary metrics are not silently relabeled as measured usage.
    assert not (tmp_path / "traces").exists()


def test_known_fake_secret_is_absent_from_error_and_doctor_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _settings(
        model_provider="nvidia",
        nvidia_model=None,
        nvidia_api_key=KNOWN_FAKE_SECRET,
    )

    with pytest.raises(ConfigurationError) as captured:
        settings.ensure_ready()
    error_output = f"{captured.value.code}: {captured.value.safe_message}"

    exit_code = _doctor(settings)
    doctor_output = capsys.readouterr().out

    assert exit_code == 1
    assert KNOWN_FAKE_SECRET not in error_output
    assert KNOWN_FAKE_SECRET not in doctor_output
    assert "NVIDIA_API_KEY" in doctor_output
    assert '"configured": true' in doctor_output


@pytest.mark.parametrize(
    ("overrides", "expected_reserved"),
    [
        ({"enable_debate": True}, "feature:debate"),
        ({"enable_auto_domain_creation": True}, "feature:auto_domain_creation"),
    ],
)
def test_doctor_marks_selected_unimplemented_features_as_reserved(
    capsys: pytest.CaptureFixture[str],
    overrides: dict[str, bool],
    expected_reserved: str,
) -> None:
    exit_code = _doctor(_settings(**overrides))
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["ready"] is False
    assert expected_reserved in payload["reserved_not_implemented"]


def test_doctor_reports_external_write_request_as_ineffective(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = _doctor(_settings(allow_external_writes=True))
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["ready"] is True
    assert payload["external_writes_requested"] is True
    assert payload["external_writes_effective"] is False


# --- P1-006C: opt-in Langfuse export (in-process fake OTLP server, no network) ---

LF_PUBLIC = "pk-lf-known-fake-public-for-tests"
LF_SECRET = "sk-lf-known-fake-secret-for-tests"
TRACE_CANARY = "PRIVATE_CANARY_LF9"
LOOPBACK_LANGFUSE = "http://127.0.0.1:3000"
OPAQUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$")


class OtlpSink:
    """In-process fake Langfuse OTLP endpoint that records every received request."""

    def __init__(self, respond) -> None:
        self.requests: list[httpx.Request] = []
        self._respond = respond

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._respond(request)


def _langfuse_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": f"sqlite:///{tmp_path / 'rfa.db'}",
        "trace_dir": tmp_path / "traces",
        "trace_backend": "langfuse",
        "langfuse_base_url": LOOPBACK_LANGFUSE,
        "langfuse_public_key": LF_PUBLIC,
        "langfuse_secret_key": LF_SECRET,
        "langfuse_export_enabled": True,
        "nvidia_api_key": KNOWN_FAKE_SECRET,
    }
    values.update(overrides)
    return _settings(**values)


def _canary_work() -> WorkRequest:
    return WorkRequest(
        query=f"TRIV3 공개 트랙 {TRACE_CANARY}",
        domain_id=DomainId.TRIV3,
        target=DraftTarget(audience=Audience.PUBLIC),
        request_id=TRACE_CANARY,
        trace_id=TRACE_CANARY,
        agent_id=TRACE_CANARY,
    )


async def _run_with_export(tmp_path, principal, respond):
    sink = OtlpSink(respond)
    container = build_container(
        _langfuse_settings(tmp_path), trace_transport=httpx.MockTransport(sink)
    )
    await container.startup()
    try:
        result = await container.service.run(_canary_work(), principal)
        ledger = await container.service.observations.ledger(result.run_id, principal)
    finally:
        await container.shutdown()
    local_lines = [
        line
        for path in sorted((tmp_path / "traces" / "rfa-observations-v1").glob("events-*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    return container, sink, result, ledger, local_lines


def _span(request: httpx.Request) -> dict[str, Any]:
    payload = json.loads(request.content)
    assert set(payload) == {"resourceSpans"} and len(payload["resourceSpans"]) == 1
    resource = payload["resourceSpans"][0]
    assert resource["resource"]["attributes"] == [
        {"key": "service.name", "value": {"stringValue": "rfa-mas"}}
    ]
    (scope,) = resource["scopeSpans"]
    (span,) = scope["spans"]
    return span


def _attr(span: dict[str, Any]) -> dict[str, Any]:
    return {item["key"]: item["value"] for item in span["attributes"]}


def _record(**event_overrides: Any) -> ObservationRecord:
    event = TraceEvent(
        execution=ExecutionContext(
            request_id="req_alias_1", trace_id="trace_alias_1", run_id="run_alias_1",
            agent_id="agent_alias_1",
        ),
        event="model",
        status="succeeded",
        mode=ExecutionMode.MOCK,
        versions=VersionReferences(code="rfa-observations-v1", policy="policy_alias_1"),
        timestamp=datetime.now(UTC),
        duration_ms=1.5,
        **event_overrides,
    )
    return ObservationRecord(
        observation_id="obs_alias_1", sequence=1, origin="port",
        provider_ref="provider_alias_1", transport="returned", event=event,
    )


def _exporter(sink, *, base_url=LOOPBACK_LANGFUSE, enabled=True, public=LF_PUBLIC,
              secret=LF_SECRET):
    client = httpx.AsyncClient(base_url=base_url, transport=httpx.MockTransport(sink))
    exporter = LangfuseOtlpExporter(
        client=client,
        egress=LangfuseEgress(base_url, enabled),
        public_key=SecretStr(public) if public else None,
        secret_key=SecretStr(secret) if secret else None,
        redactor=SecretRedactor((KNOWN_FAKE_SECRET, LF_PUBLIC, LF_SECRET)),
        timeout_seconds=1.0,
    )
    return exporter, client


async def test_langfuse_export_sends_only_allowlisted_metadata_and_never_canary_or_secret(
    tmp_path: Path, principal
) -> None:
    container, sink, result, ledger, local_lines = await _run_with_export(
        tmp_path, principal, lambda request: httpx.Response(200, json={})
    )
    assert result.status == WorkStatus.COMPLETED, result.errors
    rows = ledger.observations
    assert rows and len(sink.requests) == len(rows)
    # The local trace stays the source of truth: every exported record is also local.
    assert len(local_lines) == len(rows)
    expected_auth = "Basic " + base64.b64encode(f"{LF_PUBLIC}:{LF_SECRET}".encode()).decode()
    forbidden = (
        TRACE_CANARY, LF_PUBLIC, LF_SECRET, KNOWN_FAKE_SECRET, principal.user_id,
        result.run_id, result.draft.content, "공개 트랙",
    )
    for request in sink.requests:
        assert request.method == "POST"
        assert (request.url.host, request.url.path) == ("127.0.0.1", "/api/public/otel/v1/traces")
        assert request.headers["authorization"] == expected_auth  # the key is header-only
        assert request.headers["x-langfuse-ingestion-version"] == "4"
        # Decode escapes so non-ASCII body text cannot hide behind \uXXXX in the check.
        decoded = json.dumps(json.loads(request.content), ensure_ascii=False)
        assert not [value for value in forbidden if value in decoded]
        span = _span(request)
        assert set(span) == {
            "traceId", "spanId", "name", "kind", "startTimeUnixNano", "endTimeUnixNano",
            "attributes", "status",
        }
        assert set(span["status"]) == {"code"}  # no free-text status message
        attributes = _attr(span)
        assert set(attributes) <= ALLOWED_ATTRIBUTE_KEYS
        for value in attributes.values():
            scalars = value.get("arrayValue", {}).get("values", [value])
            for scalar in scalars:
                if "stringValue" in scalar:
                    assert OPAQUE.fullmatch(scalar["stringValue"])
        # Tokens were not reported by the mock model: absent, never 0.
        assert "gen_ai.usage.input_tokens" not in attributes
        assert "gen_ai.usage.output_tokens" not in attributes
        assert attributes[METADATA_PREFIX + "token_usage"] == {"stringValue": "not_reported"}
    receipts = container.trace.export_receipts()
    assert {r.observation_id for r in receipts} == {r.observation_id for r in rows}
    assert {(r.status, r.reason) for r in receipts} == {("exported", "ok")}
    assert len({r.otel_trace_id for r in receipts}) == 1  # one Langfuse trace per run
    assert len({r.otel_span_id for r in receipts}) == len(rows)
    adapter = {info.port: info for info in container.adapters}["trace"]
    assert adapter.adapter == "local-jsonl-trace+langfuse-otlp" and adapter.simulated is False


async def test_langfuse_connection_failure_is_recorded_as_failed_never_success(
    tmp_path: Path, principal
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    container, sink, result, ledger, local_lines = await _run_with_export(
        tmp_path, principal, refuse
    )
    # Export is additive: the run and the local trace are unaffected by the outage.
    assert result.status == WorkStatus.COMPLETED, result.errors
    assert ledger.observations and len(local_lines) == len(ledger.observations)
    receipts = container.trace.export_receipts()
    assert len(receipts) == len(sink.requests) == len(ledger.observations)
    assert {(r.status, r.reason) for r in receipts} == {("failed", "connection_error")}


@pytest.mark.parametrize(
    ("respond", "expected"),
    [
        (lambda r: httpx.Response(200, json={}), ("exported", "ok", 200)),
        (lambda r: httpx.Response(200, content=b""), ("exported", "ok", 200)),
        (
            lambda r: httpx.Response(
                200, json={"partialSuccess": {"rejectedSpans": 0, "errorMessage": ""}}
            ),
            ("exported", "ok", 200),
        ),
        (
            lambda r: httpx.Response(
                200,
                json={"partialSuccess": {"rejectedSpans": 1, "errorMessage": TRACE_CANARY}},
            ),
            ("failed", "rejected", 200),
        ),
        (
            # Legacy ingestion-style 207 multi-status is not an OTLP acknowledgement.
            lambda r: httpx.Response(
                207, json={"successes": [], "errors": [{"id": "x", "status": 400}]}
            ),
            ("failed", "invalid_response", 207),
        ),
        (
            lambda r: httpx.Response(207, json={"partialSuccess": {"rejectedSpans": "1"}}),
            ("failed", "rejected", 207),
        ),
        (
            lambda r: httpx.Response(401, json={"message": TRACE_CANARY}),
            ("failed", "auth_error", 401),
        ),
        (lambda r: httpx.Response(500, text=TRACE_CANARY), ("failed", "http_error", 500)),
        (
            lambda r: httpx.Response(200, text="<html>ok</html>"),
            ("failed", "invalid_response", 200),
        ),
    ],
)
async def test_langfuse_export_counts_only_an_acknowledged_2xx_as_exported(
    respond, expected
) -> None:
    sink = OtlpSink(respond)
    exporter, client = _exporter(sink)
    async with client:
        receipt = await exporter.export(_record())
    assert len(sink.requests) == 1  # single attempt, never a retried batch
    assert (receipt.status, receipt.reason, receipt.http_status) == expected
    assert TRACE_CANARY not in repr(receipt)  # server text is never kept


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (httpx.ConnectError, "connection_error"),
        (httpx.ReadTimeout, "timeout"),
        (httpx.RemoteProtocolError, "connection_error"),
    ],
)
async def test_langfuse_transport_errors_are_failures(error, reason) -> None:
    def raise_error(request: httpx.Request) -> httpx.Response:
        raise error("synthetic transport failure", request=request)

    exporter, client = _exporter(OtlpSink(raise_error))
    async with client:
        receipt = await exporter.export(_record())
    assert (receipt.status, receipt.reason, receipt.http_status) == ("failed", reason, None)


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"enabled": False}, "egress_not_permitted"),
        ({"base_url": "https://cloud.langfuse.com"}, "egress_not_permitted"),
        ({"base_url": "http://langfuse.internal.invalid:3000"}, "egress_not_permitted"),
        ({"base_url": "http://user:pass@127.0.0.1:3000"}, "egress_not_permitted"),
        ({"public": None}, "credentials_missing"),
        ({"secret": None}, "credentials_missing"),
    ],
)
async def test_langfuse_without_endpoint_permission_sends_no_request(kwargs, reason) -> None:
    sink = OtlpSink(lambda r: httpx.Response(200, json={}))
    exporter, client = _exporter(sink, **kwargs)
    async with client:
        receipt = await exporter.export(_record())
    assert sink.requests == []
    assert (receipt.status, receipt.reason) == ("not_attempted", reason)


def test_langfuse_keys_alone_grant_no_export_and_non_loopback_stays_reserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("no client may be built without egress permission")

    monkeypatch.setattr(bootstrap.httpx, "AsyncClient", forbidden)
    disabled = _langfuse_settings(tmp_path, langfuse_export_enabled=False)
    assert disabled.missing_for_selected_modes() == ["LANGFUSE_EXPORT_ENABLED"]
    with pytest.raises(ConfigurationError) as missing:
        build_container(disabled)
    assert missing.value.missing == ("LANGFUSE_EXPORT_ENABLED",)
    remote = _langfuse_settings(tmp_path, langfuse_base_url="https://cloud.langfuse.com")
    with pytest.raises(BackendNotImplementedError) as reserved:
        build_container(remote)
    assert "endpoint:LANGFUSE_BASE_URL:non_loopback" in reserved.value.safe_message
    for error in (missing.value, reserved.value):
        assert LF_SECRET not in error.safe_message and LF_PUBLIC not in error.safe_message


async def test_langfuse_blocks_a_payload_containing_a_configured_secret() -> None:
    sink = OtlpSink(lambda r: httpx.Response(200, json={}))
    exporter, client = _exporter(sink)
    leaked = _record().model_copy(update={"provider_ref": LF_SECRET})
    async with client:
        receipt = await exporter.export(leaked)
    assert sink.requests == []
    assert (receipt.status, receipt.reason) == ("not_attempted", "redaction_blocked")


@pytest.mark.parametrize(
    ("tokens", "expected_attributes", "usage"),
    [
        ({}, {}, "not_reported"),
        ({"input_tokens": 0}, {"gen_ai.usage.input_tokens": {"intValue": "0"}}, "partial"),
        (
            {"input_tokens": 12, "output_tokens": 3},
            {
                "gen_ai.usage.input_tokens": {"intValue": "12"},
                "gen_ai.usage.output_tokens": {"intValue": "3"},
            },
            "reported",
        ),
    ],
)
async def test_langfuse_unreported_tokens_stay_null_and_measured_zero_is_kept(
    tokens, expected_attributes, usage
) -> None:
    sink = OtlpSink(lambda r: httpx.Response(200, json={}))
    exporter, client = _exporter(sink)
    async with client:
        receipt = await exporter.export(_record(**tokens))
    assert receipt.status == "exported"
    attributes = _attr(_span(sink.requests[0]))
    usage_attributes = {k: v for k, v in attributes.items() if k.startswith("gen_ai.usage.")}
    assert usage_attributes == expected_attributes
    assert attributes[METADATA_PREFIX + "token_usage"] == {"stringValue": usage}


async def test_langfuse_exports_only_records_verified_by_the_local_trace(tmp_path: Path) -> None:
    sink = OtlpSink(lambda r: httpx.Response(200, json={}))
    exporter, client = _exporter(sink)
    trace = LangfuseExportTrace(LocalJsonlTrace(tmp_path / "traces", SecretRedactor()), exporter)
    async with client:
        with pytest.raises(RfaError):
            await trace.emit_observation(_record())
    assert sink.requests == [] and trace.export_receipts() == ()


def test_export_receipt_cannot_claim_success_for_a_failed_request() -> None:
    with pytest.raises(ValueError):
        TraceExportReceipt("obs_alias_1", "exported", "connection_error")
    with pytest.raises(ValueError):
        TraceExportReceipt("obs_alias_1", "failed", "ok")
    with pytest.raises(ValueError):
        TraceExportReceipt("obs_alias_1", "not_attempted", "timeout")
