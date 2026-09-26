from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from rfa_mas.cli import _parser, main
from rfa_mas.dev_env import GENERATED_SECRET_NAMES, initialize_dev_env


def _write_template(project_root: Path, *, nvidia_api_key: str = "") -> None:
    assignments = {
        "APP_API_KEY": "",
        "NVIDIA_API_KEY": nvidia_api_key,
        "NEMO_RETRIEVER_API_TOKEN": "",
        "RESPONSE_API_TOKEN": "",
        "TOOL_API_TOKEN": "",
        "RUNTIME_API_TOKEN": "",
        "POLICY_API_TOKEN": "",
        "LANGFUSE_PUBLIC_KEY": "",
        "LANGFUSE_SECRET_KEY": "",
    }
    (project_root / ".env.example").write_text(
        "\n".join(f"{name}={value}" for name, value in assignments.items()) + "\n",
        encoding="utf-8",
    )


def _assignments(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )


def test_initialize_dev_env_generates_distinct_secrets_without_nvidia_key(
    tmp_path: Path,
) -> None:
    _write_template(tmp_path)

    result = initialize_dev_env(tmp_path)
    values = _assignments(tmp_path / ".env")
    generated_values = [values[name] for name in GENERATED_SECRET_NAMES]

    assert result.created is True
    assert set(result.generated) == set(GENERATED_SECRET_NAMES)
    assert result.preserved == ()
    assert result.requires_external_input == ("NVIDIA_API_KEY",)
    assert all(generated_values)
    assert len(generated_values) == len(set(generated_values))
    assert values["LANGFUSE_PUBLIC_KEY"].startswith("pk-lf-")
    assert values["LANGFUSE_SECRET_KEY"].startswith("sk-lf-")
    assert values["NVIDIA_API_KEY"] == ""
    assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600


def test_initialize_dev_env_preserves_existing_credentials(tmp_path: Path) -> None:
    _write_template(tmp_path, nvidia_api_key="known-fake-external-key")
    first = initialize_dev_env(tmp_path)
    first_content = (tmp_path / ".env").read_text(encoding="utf-8")

    second = initialize_dev_env(tmp_path)

    assert first.requires_external_input == ()
    assert second.created is False
    assert second.generated == ()
    assert set(second.preserved) == set(GENERATED_SECRET_NAMES)
    assert second.requires_external_input == ()
    assert (tmp_path / ".env").read_text(encoding="utf-8") == first_content
    assert "known-fake-external-key" not in repr(second)


def test_initialize_named_profile_without_copying_default_env(tmp_path: Path) -> None:
    _write_template(tmp_path)

    result = initialize_dev_env(tmp_path, target_name=".env.dev")

    assert result.path == str(tmp_path / ".env.dev")
    assert (tmp_path / ".env.dev").is_file()
    assert not (tmp_path / ".env").exists()
    assert stat.S_IMODE((tmp_path / ".env.dev").stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "target_name",
    (".env.example", "env.dev", "../.env.dev", "/tmp/.env.dev"),
)
def test_initialize_dev_env_rejects_unsafe_target_names(
    tmp_path: Path,
    target_name: str,
) -> None:
    _write_template(tmp_path)

    with pytest.raises(ValueError, match="target_name"):
        initialize_dev_env(tmp_path, target_name=target_name)


def test_initialize_dev_env_rejects_symlink_target(tmp_path: Path) -> None:
    _write_template(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("do-not-replace\n", encoding="utf-8")
    (tmp_path / ".env.dev").symlink_to(outside)

    with pytest.raises(ValueError, match="regular file"):
        initialize_dev_env(tmp_path, target_name=".env.dev")

    assert outside.read_text(encoding="utf-8") == "do-not-replace\n"


def test_cli_accepts_named_init_output_and_runtime_env_file() -> None:
    init_args = _parser().parse_args(["init-env", "--output", ".env.dev"])
    doctor_args = _parser().parse_args(["--env-file", ".env.dev", "doctor"])

    assert init_args.output == ".env.dev"
    assert doctor_args.env_file == Path(".env.dev")


def test_cli_rejects_explicit_missing_env_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = tmp_path / ".env.dev"
    monkeypatch.setattr(sys, "argv", ["rfa", "--env-file", str(missing), "doctor"])

    with pytest.raises(SystemExit) as captured:
        main()

    assert captured.value.code == 2
