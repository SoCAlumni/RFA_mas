"""Subprocess boundary for the NemoClaw / OpenShell / openssl CLIs.

Only argv is ever logged; environment values (credentials) are passed to the child and never
recorded. Tests substitute a scripted runner through the same ``Runner`` protocol.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

EXTRA_PATH = (Path.home() / ".hermes" / "node" / "bin", Path.home() / ".local" / "bin")


class CommandError(RuntimeError):
    def __init__(self, result: CommandResult):
        self.result = result
        super().__init__(
            f"{' '.join(result.argv[:4])} failed rc={result.returncode}: "
            f"{(result.stderr or result.stdout).strip()[-400:]}"
        )


@dataclass
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class Runner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float = 300,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
        check: bool = False,
    ) -> CommandResult: ...


def resolve_binary(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    for directory in EXTRA_PATH:
        candidate = directory / name
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError(f"{name} not found on PATH or in {[str(p) for p in EXTRA_PATH]}")


@dataclass
class SubprocessRunner:
    """Runs commands with ``~/.hermes/node/bin`` on PATH (NemoClaw's npm bin on this host)."""

    log: list[CommandResult] = field(default_factory=list)
    cwd: Path | None = None

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float = 300,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
        check: bool = False,
    ) -> CommandResult:
        argv = list(argv)
        merged = dict(os.environ)
        merged["PATH"] = os.pathsep.join([str(p) for p in EXTRA_PATH] + [merged.get("PATH", "")])
        merged.setdefault("NEMOCLAW_NON_INTERACTIVE", "1")
        if env:
            merged.update(env)
        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=merged,
                input=input_text,
                cwd=str(self.cwd) if self.cwd else None,
                check=False,
            )
            result = CommandResult(argv, proc.returncode, proc.stdout, proc.stderr)
        except subprocess.TimeoutExpired as exc:
            result = CommandResult(
                argv,
                124,
                (exc.stdout or b"").decode() if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
                f"timeout after {timeout}s",
            )
        except FileNotFoundError as exc:
            result = CommandResult(argv, 127, "", str(exc))
        result.duration_seconds = round(time.monotonic() - started, 3)
        self.log.append(result)
        if check and not result.ok:
            raise CommandError(result)
        return result


def strip_warnings(text: str) -> str:
    """Drop Node/undici warning lines that NemoClaw prints before JSON output."""
    keep = []
    for line in text.splitlines():
        if "UNDICI-EHPA" in line or "--trace-warnings" in line or "Active gateway set" in line:
            continue
        keep.append(line)
    return "\n".join(keep)


def extract_json(text: str):
    """Parse the first JSON document in noisy CLI output (arrays or objects)."""
    import json

    cleaned = strip_warnings(text)
    starts = [i for i in (cleaned.find("{"), cleaned.find("[")) if i >= 0]
    if not starts:
        raise ValueError("no JSON in output")
    return json.loads(cleaned[min(starts) :])
