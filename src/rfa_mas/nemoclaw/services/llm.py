"""Hosted LLM provider and model of the egress-proxy, switchable while running (D-18).

The proxy, the censor's direct judge and the chat ranker all share one ``Routing`` object and one
``backend_keys`` dict; switching rewrites them in place (``config.apply_llm_provider``), so the next
call already goes to the new provider. The choice is written to the backend's env file
(``RFA_LLM_PROVIDER`` + the preset's model key) so a restart keeps it. Local models are never
offered: the only local option is listed as unselectable."""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path

from rfa_mas.nemoclaw import audit, logs
from rfa_mas.nemoclaw.config import (
    LLM_PROVIDERS,
    PROVIDER_ENV,
    ConfigError,
    Routing,
    apply_llm_provider,
)

LABELS = {"nvidia": "NVIDIA Endpoints", "gemini": "Google Gemini"}
LOCAL = {"provider": "ollama_local", "label": "Ollama 로컬", "selectable": False,
         "reason": "로컬 LLM을 확인할 수 없습니다.", "models": []}


class LlmError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _read(env_file: Path | None, key: str) -> str | None:
    value = os.environ.get(key)
    if value is not None:
        return value.strip() or None
    if env_file is None or not env_file.exists():
        return None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    return None


def write_env(env_file: Path, values: dict[str, str]) -> None:
    """Set ``KEY=value`` lines in place (append missing ones); keeps every other line and the file mode."""
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    seen = set()
    for i, line in enumerate(lines):
        m = re.match(r"^([A-Z0-9_]+)=", line)
        if m and m.group(1) in values:
            lines[i] = f"{m.group(1)}={values[m.group(1)]}"
            seen.add(m.group(1))
    lines += [f"{k}={v}" for k, v in values.items() if k not in seen]
    mode = env_file.stat().st_mode & 0o777 if env_file.exists() else 0o600
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    env_file.chmod(mode)


class LlmControl:
    def __init__(self, routing: Routing, backend_keys: dict[str, str], root: Path):
        self.routing, self.backend_keys, self.root = routing, backend_keys, root
        self._lock = threading.Lock()

    def _backend(self) -> tuple[str, Path | None]:
        for name, backend in self.routing.backends.items():
            if backend.auth == "bearer":
                return name, (self.root / backend.env_file if backend.env_file else None)
        raise LlmError("no_backend", "호스팅 LLM 백엔드가 설정되지 않았습니다.")

    def current(self) -> dict:
        model = next(iter(self.routing.aliases.values())).model if self.routing.aliases else ""
        return {"provider": self.routing.provider, "label": LABELS.get(self.routing.provider, self.routing.provider),
                "model": model}

    def options(self) -> list[dict]:
        _, env_file = self._backend()
        out = []
        for name, preset in LLM_PROVIDERS.items():
            has_key = bool(_read(env_file, preset["credential_env"]))
            configured = _read(env_file, preset["model_env"])
            models = list(preset["models"] or dict.fromkeys(m for m in (configured, preset["model"]) if m))
            out.append({"provider": name, "label": LABELS.get(name, name), "selectable": has_key,
                        "reason": None if has_key else f"{preset['credential_env']} 가 등록되지 않았습니다.",
                        "models": models})
        return [*out, LOCAL]

    def switch(self, provider: str, model: str | None) -> dict:
        option = next((o for o in self.options() if o["provider"] == provider), None)
        if option is None:
            raise LlmError("unknown_provider", "없는 추론 제공자입니다.")
        if not option["selectable"]:
            raise LlmError("not_selectable", option["reason"] or "선택할 수 없는 제공자입니다.")
        preset = LLM_PROVIDERS[provider]
        model = model or (option["models"][0] if option["models"] else preset["model"])
        if preset["models"] and model not in preset["models"]:
            raise LlmError("unknown_model", f"{option['label']} 는 {', '.join(preset['models'])} 만 제공합니다.")
        name, env_file = self._backend()
        before = self.current()
        with self._lock:
            os.environ[PROVIDER_ENV], os.environ[preset["model_env"]] = provider, model
            try:
                apply_llm_provider(self.routing, self.root)
            except ConfigError as exc:
                raise LlmError("invalid_provider", str(exc)[:200]) from exc
            key = _read(env_file, self.routing.backends[name].credential_env)
            if key:
                self.backend_keys[name] = key
            if env_file is not None:
                write_env(env_file, {PROVIDER_ENV: provider, preset["model_env"]: model})
        after = self.current()
        logs.event("llm_switch", before=before, after=after)
        audit.record(kind="admin", verdict="applied", action="llm-provider", detail={"before": before, "after": after})
        return after
