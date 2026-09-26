from __future__ import annotations

import contextlib
import importlib.abc
import io
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import SecretStr

from rfa_mas import bootstrap, cli
from rfa_mas.bootstrap import build_container, inspect_configuration
from rfa_mas.cli import _doctor, sqlite_runtime_status
from rfa_mas.dev_env import DevEnvInitialization
from rfa_mas.errors import BackendNotImplementedError, ConfigurationError
from rfa_mas.settings import PLANNED_SETTINGS, Settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_ASSIGNMENT = re.compile(r"^(?P<name>[A-Z][A-Z0-9_]*)=")


def _settings(**overrides: Any) -> Settings:
    """Build settings from declared defaults without reading a developer's environment."""
    values = {
        name: field.get_default(call_default_factory=True)
        for name, field in Settings.model_fields.items()
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _example_names() -> list[str]:
    names: list[str] = []
    for line in (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if match := ENV_ASSIGNMENT.match(line.strip()):
            names.append(match.group("name"))
    return names


def test_env_example_and_settings_names_have_exact_parity() -> None:
    example_names = _example_names()
    setting_names = {name.upper() for name in Settings.model_fields}

    assert len(example_names) == len(set(example_names)), ".env.example has duplicate names"
    assert set(example_names) == setting_names


def test_env_example_exists_and_is_explicitly_exempted_from_secret_ignores() -> None:
    example = PROJECT_ROOT / ".env.example"
    ignore_lines = {
        line.strip()
        for line in (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert example.is_file()
    assert ".env" in ignore_lines
    assert ".env.*" in ignore_lines
    assert "!.env.example" in ignore_lines


def test_default_mock_local_modes_are_ready_without_keys() -> None:
    settings = _settings()

    assert settings.secret_values() == ()
    assert settings.missing_for_selected_modes() == []
    settings.ensure_ready()


@pytest.mark.parametrize(
    ("overrides", "expected_missing"),
    [
        (
            {"model_provider": "nvidia"},
            {"NVIDIA_API_KEY", "NVIDIA_MODEL"},
        ),
        (
            {"retriever_backend": "nemo_service"},
            {"NEMO_RETRIEVER_API_TOKEN", "RETRIEVER_SERVICE_URL"},
        ),
        (
            {"response_backend": "http"},
            {"RESPONSE_API_TOKEN", "RESPONSE_BASE_URL"},
        ),
        (
            {"tool_backend": "http"},
            {"TOOL_API_TOKEN", "TOOL_BASE_URL"},
        ),
        (
            {"runtime_backend": "http"},
            {"RUNTIME_API_TOKEN", "RUNTIME_BASE_URL"},
        ),
        (
            {"policy_backend": "http"},
            {"POLICY_API_TOKEN", "POLICY_BASE_URL"},
        ),
        (
            {"trace_backend": "langfuse"},
            # P1-006C: keys are not egress permission; the explicit gate is required too.
            {
                "LANGFUSE_BASE_URL",
                "LANGFUSE_PUBLIC_KEY",
                "LANGFUSE_SECRET_KEY",
                "LANGFUSE_EXPORT_ENABLED",
            },
        ),
        (
            {"enable_judge": True, "judge_provider": "nvidia"},
            {"JUDGE_MODEL", "NVIDIA_API_KEY"},
        ),
    ],
)
def test_selected_real_modes_report_only_their_missing_variable_names(
    overrides: dict[str, Any], expected_missing: set[str]
) -> None:
    settings = _settings(**overrides)

    assert set(settings.missing_for_selected_modes()) == expected_missing
    with pytest.raises(ConfigurationError) as captured:
        settings.ensure_ready()

    assert set(captured.value.missing) == expected_missing
    assert captured.value.code == "configuration_error"


def test_external_bind_requires_authentication_key() -> None:
    unauthenticated = _settings(app_host="0.0.0.0")

    assert unauthenticated.missing_for_selected_modes() == ["APP_API_KEY"]
    with pytest.raises(ConfigurationError) as captured:
        unauthenticated.ensure_ready()
    assert captured.value.missing == ("APP_API_KEY",)

    authenticated = _settings(app_host="0.0.0.0", app_api_key="known-fake-app-key")
    assert authenticated.missing_for_selected_modes() == []
    authenticated.ensure_ready()


@pytest.mark.parametrize(
    ("overrides", "backend_name"),
    [
        (
            {
                "retriever_backend": "nemo_service",
                "retriever_service_url": "https://reference.invalid",
                "nemo_retriever_api_token": "known-fake-retriever-token",
            },
            "retriever:nemo_service",
        ),
        (
            {
                "trace_backend": "langfuse",
                "langfuse_base_url": "https://reference.invalid",
                "langfuse_public_key": "known-fake-public-key",
                "langfuse_secret_key": "known-fake-secret-key",
                "langfuse_export_enabled": True,
            },
            # P1-006C implements loopback export only; a remote endpoint stays reserved.
            "endpoint:LANGFUSE_BASE_URL:non_loopback",
        ),
        (
            {
                "enable_judge": True,
                "judge_provider": "nvidia",
                "judge_model": "reference-judge-id",
                "nvidia_api_key": "known-fake-judge-key",
            },
            "judge:nvidia",
        ),
    ],
)
def test_configured_reserved_backend_fails_instead_of_falling_back(
    tmp_path: Path, overrides: dict[str, Any], backend_name: str
) -> None:
    settings = _settings(
        database_url=f"sqlite:///{tmp_path / 'rfa.db'}",
        trace_dir=tmp_path / "traces",
        **overrides,
    )
    settings.ensure_ready()

    with pytest.raises(BackendNotImplementedError) as captured:
        build_container(settings)

    assert captured.value.code == "not_implemented"
    assert backend_name in captured.value.safe_message


@pytest.mark.parametrize(
    ("feature_override", "feature_name"),
    [
        ({"enable_debate": True}, "feature:debate"),
        ({"enable_auto_domain_creation": True}, "feature:auto_domain_creation"),
    ],
)
def test_selected_reserved_feature_fails_instead_of_being_ignored(
    tmp_path: Path,
    feature_override: dict[str, bool],
    feature_name: str,
) -> None:
    settings = _settings(
        database_url=f"sqlite:///{tmp_path / 'rfa.db'}",
        trace_dir=tmp_path / "traces",
        **feature_override,
    )

    assert settings.missing_for_selected_modes() == []
    assert settings.selected_reserved_features() == (feature_name,)
    with pytest.raises(BackendNotImplementedError) as captured:
        build_container(settings)

    assert captured.value.code == "not_implemented"
    assert feature_name in captured.value.safe_message


def test_external_write_gate_is_recorded_but_cannot_become_effective(tmp_path: Path) -> None:
    settings = _settings(
        allow_external_writes=True,
        database_url=f"sqlite:///{tmp_path / 'rfa.db'}",
        trace_dir=tmp_path / "traces",
    )

    container = build_container(settings)

    assert container.settings.allow_external_writes is True
    assert container.settings.external_writes_effective is False


def doctor_payload(settings: Settings) -> tuple[int, dict[str, Any], str]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exit_code = _doctor(settings)
    return exit_code, json.loads(output.getvalue()), output.getvalue()


def test_example_secret_values_are_empty_and_planned_names_are_not_active() -> None:
    assignments = dict(
        line.split("=", 1)
        for line in (PROJECT_ROOT / ".env.example").read_text().splitlines()
        if ENV_ASSIGNMENT.match(line)
    )
    secret_names = {
        name.upper()
        for name, field in Settings.model_fields.items()
        if SecretStr in get_args(field.annotation)
    }
    assert secret_names and all(assignments[name] == "" for name in secret_names)
    names = [item.proposed_name for item in PLANNED_SETTINGS if item.proposed_name]
    assert len(names) == len(set(names))
    assert set(names).isdisjoint(assignments)
    assert all(
        item.status == "planned_not_read" and item.owner_tasks and item.reason
        for item in PLANNED_SETTINGS
    )
    assert all(set(item.reuses) <= assignments.keys() for item in PLANNED_SETTINGS)
    assert not any(item.purpose == "trace_retention" for item in PLANNED_SETTINGS)
    assert assignments["TRACE_RETENTION_DAYS"] == "7"
    assert "SESSION_DB" not in assignments and "CHECKPOINT_DATABASE_URL" not in assignments


def test_doctor_reports_preflight_without_creating_files_or_probing(tmp_path, monkeypatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("No client or database in doctor")

    monkeypatch.setattr(bootstrap.httpx, "AsyncClient", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    settings = _settings(
        database_url=f"sqlite:///{tmp_path / 'absent' / 'rfa.db'}",
        trace_dir=tmp_path / "absent" / "traces",
    )
    code, report, _ = doctor_payload(settings)
    assert code == 0 and report["ready"]
    assert report["ready_scope"] == "preflight_only"
    assert report["configuration_ready"] and report["implementation_ready"]
    assert report["local_lifecycle"] == report["provider_probe"] == "not_run"
    assert report["external_writes_effective"] is report["external_egress_effective"] is False
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize(
    "overrides,error_class",
    [
        ({"database_url": "postgresql://private-CANARY.invalid/db"}, ConfigurationError),
        (
            {
                "response_backend": "http",
                "response_base_url": "https://private-CANARY.invalid",
                "response_api_token": "private-CANARY",
            },
            BackendNotImplementedError,
        ),
        (
            {
                "response_backend": "http",
                "response_base_url": "ftp://127.0.0.1",
                "response_api_token": "private-CANARY",
            },
            ConfigurationError,
        ),
        (
            {
                "response_backend": "http",
                "response_base_url": "http://private-CANARY@127.0.0.1",
                "response_api_token": "private-CANARY",
            },
            ConfigurationError,
        ),
        (
            {
                "tool_backend": "http",
                "tool_base_url": "http://127.0.0.1/?token=private-CANARY",
                "tool_api_token": "private-CANARY",
            },
            ConfigurationError,
        ),
        (
            {
                "runtime_backend": "http",
                "runtime_base_url": "http://127.0.0.1/#private-CANARY",
                "runtime_api_token": "private-CANARY",
            },
            ConfigurationError,
        ),
        (
            {
                "policy_backend": "http",
                "policy_base_url": "http://[private-CANARY",
                "policy_api_token": "private-CANARY",
            },
            ConfigurationError,
        ),
        ({"response_backend": "http"}, ConfigurationError),
        (
            {
                "response_backend": "http",
                "response_base_url": "http://",
                "response_api_token": "private-CANARY",
            },
            ConfigurationError,
        ),
        (
            {
                "model_provider": "nvidia",
                "nvidia_api_key": "private-CANARY",
                # P1-002: the adapter exists, so only an INVALID selection fails here.
                "nvidia_model": "private CANARY model",
            },
            ConfigurationError,
        ),
        (
            {
                "model_provider": "nvidia",
                "nvidia_api_key": "private-CANARY",
                "nvidia_model": "reference-model-id",
                "nvidia_base_url": "http://private-CANARY.invalid/v1",
            },
            ConfigurationError,
        ),
    ],
)
def test_doctor_and_bootstrap_agree_without_exposing_inputs_or_allocating_clients(
    overrides, error_class, tmp_path, monkeypatch
) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("Unavailable selection must fail before allocating clients")

    monkeypatch.setattr(bootstrap.httpx, "AsyncClient", forbidden)
    defaults = {"database_url": f"sqlite:///{tmp_path / 'db'}", "trace_dir": tmp_path / "traces"}
    settings = _settings(**(defaults | overrides))
    code, report, text = doctor_payload(settings)
    assert code == 1 and report["ready"] is False
    with pytest.raises(error_class) as caught:
        build_container(settings)
    assert "private-CANARY" not in text + str(caught.value)
    assert not (tmp_path / "db").exists()


@pytest.mark.parametrize("port", ["response", "tool", "runtime", "policy"])
def test_local_http_configuration_is_not_claimed_as_live_readiness(port) -> None:
    settings = _settings(
        **{
            f"{port}_backend": "http",
            f"{port}_base_url": "http://127.0.0.1:1",
            f"{port}_api_token": "synthetic-token",
        }
    )
    code, report, text = doctor_payload(settings)
    assert code == 0 and report["implementation_ready"]
    assert report["provider_probe"] == report["local_lifecycle"] == "not_run"
    assert "synthetic-token" not in text and "127.0.0.1" not in text


@pytest.mark.parametrize("installed", [False, True])
def test_nat_dependency_presence_never_implies_adapter_support(installed, monkeypatch) -> None:
    def version(name):
        assert name == "nvidia-nat-langchain"
        if not installed:
            raise bootstrap.metadata.PackageNotFoundError(name)
        return "1.8.0"

    monkeypatch.setattr(bootstrap.metadata, "version", version)
    disabled = inspect_configuration(_settings())
    assert disabled.ready
    settings = _settings(enable_nat=True)
    code, report, _ = doctor_payload(settings)
    assert code == 1 and report["selected_modes"]["nat"] == "selected_unavailable"
    assert report["nat_dependency"] == ("installed_unverified" if installed else "missing")
    assert "feature:nat_adapter" in report["reserved_not_implemented"]
    assert ("NAT_EXTRA" in report["missing"]) is (not installed)
    with pytest.raises(BackendNotImplementedError if installed else ConfigurationError):
        build_container(settings)


@pytest.mark.parametrize(
    "overrides,feature",
    [
        ({"allow_external_egress": True}, "feature:external_egress_policy"),
    ],
)
def test_gate_selection_is_not_ignored_or_an_egress_grant(overrides, feature) -> None:
    settings = _settings(nvidia_api_key="synthetic-key", **overrides)
    code, report, text = doctor_payload(settings)
    assert code == 1 and feature in report["reserved_not_implemented"]
    assert report["external_egress_effective"] is False
    assert "synthetic-key" not in text
    with pytest.raises(BackendNotImplementedError, match=feature):
        build_container(settings)


async def test_default_boot_is_real_local_and_has_no_nat_import_dependency(tmp_path, monkeypatch):
    class DenyNatImport(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "nat" or fullname.startswith("nat."):
                raise AssertionError("Core must not import NAT")
            return None

    monkeypatch.setattr(sys, "meta_path", [DenyNatImport(), *sys.meta_path])
    settings = _settings(
        database_url=f"sqlite:///{tmp_path / 'core.db'}", trace_dir=tmp_path / "traces"
    )
    container = build_container(settings)
    try:
        await container.startup()
        assert container.ready
        assert settings.database_path.is_file() and settings.resolved_checkpoint_path.is_file()
        assert settings.secret_values() == ()
    finally:
        await container.shutdown()
    assert not container.ready


@pytest.mark.parametrize(
    "version,assessment",
    [
        ("3.44.5", "affected"),
        ("3.44.6", "fixed"),
        ("3.45.0", "affected"),
        ("3.50.4", "affected"),
        ("3.50.6", "affected"),
        ("3.50.7", "fixed"),
        ("3.51.0", "affected"),
        ("3.51.2", "affected"),
        ("3.51.3", "fixed"),
        ("3.53.1", "fixed"),
        ("3.6.0", "unknown"),
        ("4.0.0", "unknown"),
        ("untrusted-CANARY-version", "unknown"),
    ],
)
def test_sqlite_runtime_version_assessment_is_separate_from_integrity(
    version, assessment, monkeypatch
):
    monkeypatch.setattr(sqlite3, "sqlite_version", version)
    report = sqlite_runtime_status()
    assert report["wal_reset_patch"] == assessment
    assert (report["warning"] is None) is (assessment == "fixed")
    assert (
        report["integrity_check"] == report["concurrency_check"] == report["rfa_e2e"] == "not_run"
    )
    assert "CANARY" not in json.dumps(report)


def test_doctor_measures_actual_sqlite_not_python_patch_version():
    _, report, _ = doctor_payload(_settings())
    assert report["runtime_checks"]["sqlite"]["version"] == sqlite3.sqlite_version
    assert report["runtime_checks"]["sqlite"]["assessment_basis"] == (
        "official_release_version_not_binary_attestation"
    )


def test_cli_hides_pydantic_raw_inputs(monkeypatch, capsys):
    def invalid_settings(*args, **kwargs):
        return _settings(app_port="private-CANARY")

    monkeypatch.setattr(cli, "Settings", invalid_settings)
    monkeypatch.setattr(sys, "argv", ["rfa", "doctor"])
    with pytest.raises(SystemExit) as caught:
        cli.main()
    output = capsys.readouterr().out
    assert caught.value.code == 2 and "configuration_error" in output
    assert "private-CANARY" not in output


def test_init_env_advisory_does_not_claim_registration_or_touch_real_env(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "initialize_dev_env",
        lambda **kwargs: DevEnvInitialization(
            path="synthetic-profile",
            created=True,
            generated=(),
            preserved=(),
            requires_external_input=(),
        ),
    )
    monkeypatch.setattr(sys, "argv", ["rfa", "init-env", "--output", ".env.synthetic"])
    cli.main()
    assert "registration is not verified" in capsys.readouterr().out
