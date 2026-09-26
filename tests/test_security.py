from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from rfa_mas.adapters.local import LocalJsonlTrace
from rfa_mas.cli import _doctor
from rfa_mas.errors import ConfigurationError, RfaError
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
