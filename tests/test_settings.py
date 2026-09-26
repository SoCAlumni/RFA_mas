from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from rfa_mas.bootstrap import build_container
from rfa_mas.errors import BackendNotImplementedError, ConfigurationError
from rfa_mas.settings import Settings

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
            {"LANGFUSE_BASE_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"},
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
                "model_provider": "nvidia",
                "nvidia_model": "reference-model-id",
                "nvidia_api_key": "known-fake-nvidia-key",
            },
            "model:nvidia",
        ),
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
            },
            "trace:langfuse",
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
