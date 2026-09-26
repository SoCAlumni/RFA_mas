from __future__ import annotations

import os
import re
import secrets
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ASSIGNMENT = re.compile(r"^(?P<name>[A-Z][A-Z0-9_]*)=(?P<value>.*)$")
ENV_TARGET_NAME = re.compile(r"^\.env(?:\.[A-Za-z0-9_-]+)*$")

GENERATED_SECRET_NAMES = (
    "APP_API_KEY",
    "NEMO_RETRIEVER_API_TOKEN",
    "RESPONSE_API_TOKEN",
    "TOOL_API_TOKEN",
    "RUNTIME_API_TOKEN",
    "POLICY_API_TOKEN",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
)

_SERVICE_PREFIXES = {
    "APP_API_KEY": "rfa-app",
    "NEMO_RETRIEVER_API_TOKEN": "rfa-retriever",
    "RESPONSE_API_TOKEN": "rfa-response",
    "TOOL_API_TOKEN": "rfa-tool",
    "RUNTIME_API_TOKEN": "rfa-runtime",
    "POLICY_API_TOKEN": "rfa-policy",
}


@dataclass(frozen=True)
class DevEnvInitialization:
    path: str
    created: bool
    generated: tuple[str, ...]
    preserved: tuple[str, ...]
    requires_external_input: tuple[str, ...]


def _new_secret(name: str) -> str:
    if prefix := _SERVICE_PREFIXES.get(name):
        return f"{prefix}-{secrets.token_hex(32)}"
    if name == "LANGFUSE_PUBLIC_KEY":
        return f"pk-lf-{uuid.uuid4()}"
    if name == "LANGFUSE_SECRET_KEY":
        return f"sk-lf-{uuid.uuid4()}"
    raise ValueError(f"Unsupported generated secret name: {name}")


def _resolve_target(project_root: Path, target_name: str) -> Path:
    if not ENV_TARGET_NAME.fullmatch(target_name) or target_name == ".env.example":
        raise ValueError("target_name must be .env or an ignored .env.<profile> filename")
    return project_root / target_name


def initialize_dev_env(
    project_root: Path = PROJECT_ROOT,
    *,
    target_name: str = ".env",
) -> DevEnvInitialization:
    """Create or complete a private env profile without exposing or overwriting secrets."""

    template_path = project_root / ".env.example"
    target_path = _resolve_target(project_root, target_name)
    if template_path.is_symlink() or not template_path.is_file():
        raise ValueError(".env.example must be a regular file inside the project root")
    if target_path.is_symlink() or (target_path.exists() and not target_path.is_file()):
        raise ValueError("env profile target must be a regular file, not a symlink or directory")
    created = not target_path.exists()
    source_path = template_path if created else target_path

    lines = source_path.read_text(encoding="utf-8").splitlines(keepends=True)
    generated: list[str] = []
    preserved: list[str] = []
    found: set[str] = set()
    rewritten: list[str] = []

    for line in lines:
        content = line.rstrip("\r\n")
        ending = line[len(content) :]
        match = ASSIGNMENT.fullmatch(content)
        if match is None or match.group("name") not in GENERATED_SECRET_NAMES:
            rewritten.append(line)
            continue
        name = match.group("name")
        found.add(name)
        if match.group("value"):
            preserved.append(name)
            rewritten.append(line)
            continue
        generated.append(name)
        rewritten.append(f"{name}={_new_secret(name)}{ending}")

    missing_fields = sorted(set(GENERATED_SECRET_NAMES) - found)
    if missing_fields:
        raise ValueError(f"Environment template is missing fields: {', '.join(missing_fields)}")

    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=project_root,
        prefix=f"{target_name}.",
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(file_descriptor, 0o600)
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write("".join(rewritten))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, target_path)
        target_path.chmod(0o600)
    finally:
        temporary_path.unlink(missing_ok=True)

    nvidia_key_configured = False
    for line in rewritten:
        content = line.rstrip("\r\n")
        match = ASSIGNMENT.fullmatch(content)
        if match and match.group("name") == "NVIDIA_API_KEY":
            nvidia_key_configured = bool(match.group("value"))
            break

    return DevEnvInitialization(
        path=str(target_path),
        created=created,
        generated=tuple(sorted(generated)),
        preserved=tuple(sorted(preserved)),
        requires_external_input=() if nvidia_key_configured else ("NVIDIA_API_KEY",),
    )
