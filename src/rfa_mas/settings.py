from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlparse

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from rfa_mas.contracts import SecretStatus
from rfa_mas.errors import ConfigurationError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: str = "development"
    app_host: str = "127.0.0.1"
    app_port: int = Field(default=8000, ge=1, le=65535)
    app_api_key: SecretStr | None = None
    database_url: str = "sqlite:///./.local/rfa.db"

    model_provider: Literal["mock", "nvidia"] = "mock"
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    nvidia_model: str | None = None
    nvidia_api_key: SecretStr | None = None

    retriever_backend: Literal["mock", "nemo_cli", "nemo_service"] = "mock"
    retriever_index_dir: Path = Path("./.local/retriever")
    retriever_service_url: str | None = None
    nemo_retriever_api_token: SecretStr | None = None

    response_backend: Literal["mock", "http"] = "mock"
    response_base_url: str | None = None
    response_api_token: SecretStr | None = None

    tool_backend: Literal["mock", "http"] = "mock"
    tool_base_url: str | None = None
    tool_api_token: SecretStr | None = None

    runtime_backend: Literal["local", "http"] = "local"
    runtime_base_url: str | None = None
    runtime_api_token: SecretStr | None = None

    policy_backend: Literal["local", "http"] = "local"
    policy_base_url: str | None = None
    policy_api_token: SecretStr | None = None

    trace_backend: Literal["local", "langfuse"] = "local"
    trace_dir: Path = Path("./.local/traces")
    log_level: str = "INFO"
    langfuse_base_url: str | None = None
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None

    enable_judge: bool = False
    judge_provider: Literal["mock", "nvidia"] = "mock"
    judge_model: str | None = None

    allow_external_writes: bool = False
    enable_debate: bool = False
    enable_auto_domain_creation: bool = False

    max_graph_steps: int = Field(default=12, ge=1, le=1000)
    max_tool_calls: int = Field(default=6, ge=0, le=1000)
    tool_timeout_seconds: float = Field(default=30, gt=0, le=600)
    http_timeout_seconds: float = Field(default=30, gt=0, le=600)
    max_read_retries: int = Field(default=1, ge=0, le=5)

    @field_validator(
        "app_api_key",
        "nvidia_api_key",
        "nemo_retriever_api_token",
        "response_api_token",
        "tool_api_token",
        "runtime_api_token",
        "policy_api_token",
        "langfuse_public_key",
        "langfuse_secret_key",
        mode="before",
    )
    @classmethod
    def empty_secret_is_none(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator(
        "nvidia_model",
        "retriever_service_url",
        "response_base_url",
        "tool_base_url",
        "runtime_base_url",
        "policy_base_url",
        "langfuse_base_url",
        "judge_model",
        mode="before",
    )
    @classmethod
    def empty_string_is_none(cls, value: object) -> object:
        return None if value == "" else value

    @property
    def database_path(self) -> Path:
        parsed = urlparse(self.database_url)
        if parsed.scheme != "sqlite":
            raise ConfigurationError(["DATABASE_URL (sqlite:// only in P0)"])
        if parsed.netloc not in {"", "localhost"}:
            raise ConfigurationError(["DATABASE_URL (local sqlite path required)"])
        raw_path = unquote(parsed.path)
        if self.database_url.startswith("sqlite:///./"):
            return Path(raw_path.lstrip("/"))
        if raw_path.startswith("//"):
            return Path(raw_path[1:])
        return Path(raw_path)

    @property
    def is_loopback_bind(self) -> bool:
        if self.app_host == "localhost":
            return True
        try:
            return ipaddress.ip_address(self.app_host).is_loopback
        except ValueError:
            return False

    def missing_for_selected_modes(self) -> list[str]:
        required = self.required_for_selected_modes()
        configured = {item.name for item in self.doctor_statuses() if item.configured}
        return sorted(required - configured)

    def required_for_selected_modes(self) -> set[str]:
        required: set[str] = set()
        if not self.is_loopback_bind:
            required.add("APP_API_KEY")
        if self.model_provider == "nvidia":
            required.update({"NVIDIA_MODEL", "NVIDIA_API_KEY"})
        if self.retriever_backend == "nemo_service":
            required.update({"RETRIEVER_SERVICE_URL", "NEMO_RETRIEVER_API_TOKEN"})
        for backend, _base_url, _token, base_name, token_name in (
            (
                self.response_backend,
                self.response_base_url,
                self.response_api_token,
                "RESPONSE_BASE_URL",
                "RESPONSE_API_TOKEN",
            ),
            (
                self.tool_backend,
                self.tool_base_url,
                self.tool_api_token,
                "TOOL_BASE_URL",
                "TOOL_API_TOKEN",
            ),
            (
                self.runtime_backend,
                self.runtime_base_url,
                self.runtime_api_token,
                "RUNTIME_BASE_URL",
                "RUNTIME_API_TOKEN",
            ),
            (
                self.policy_backend,
                self.policy_base_url,
                self.policy_api_token,
                "POLICY_BASE_URL",
                "POLICY_API_TOKEN",
            ),
        ):
            if backend == "http":
                required.update({base_name, token_name})
        if self.trace_backend == "langfuse":
            required.update({"LANGFUSE_BASE_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"})
        if self.enable_judge and self.judge_provider == "nvidia":
            required.update({"JUDGE_MODEL", "NVIDIA_API_KEY"})
        return required

    def ensure_ready(self) -> None:
        missing = self.missing_for_selected_modes()
        if missing:
            raise ConfigurationError(missing)
        _ = self.database_path

    def selected_reserved_features(self) -> tuple[str, ...]:
        reserved: list[str] = []
        if self.enable_debate:
            reserved.append("feature:debate")
        if self.enable_auto_domain_creation:
            reserved.append("feature:auto_domain_creation")
        return tuple(reserved)

    @property
    def external_writes_effective(self) -> bool:
        """P0 records the requested gate but never enables an external write."""
        return False

    def secret_values(self) -> tuple[str, ...]:
        values: list[str] = []
        for secret in (
            self.app_api_key,
            self.nvidia_api_key,
            self.nemo_retriever_api_token,
            self.response_api_token,
            self.tool_api_token,
            self.runtime_api_token,
            self.policy_api_token,
            self.langfuse_public_key,
            self.langfuse_secret_key,
        ):
            if secret is not None and secret.get_secret_value():
                values.append(secret.get_secret_value())
        return tuple(values)

    def doctor_statuses(self) -> tuple[SecretStatus, ...]:
        required = self.required_for_selected_modes()
        values: dict[str, bool] = {
            "APP_API_KEY": self.app_api_key is not None,
            "NVIDIA_MODEL": bool(self.nvidia_model),
            "NVIDIA_API_KEY": self.nvidia_api_key is not None,
            "RETRIEVER_SERVICE_URL": bool(self.retriever_service_url),
            "NEMO_RETRIEVER_API_TOKEN": self.nemo_retriever_api_token is not None,
            "RESPONSE_BASE_URL": bool(self.response_base_url),
            "RESPONSE_API_TOKEN": self.response_api_token is not None,
            "TOOL_BASE_URL": bool(self.tool_base_url),
            "TOOL_API_TOKEN": self.tool_api_token is not None,
            "RUNTIME_BASE_URL": bool(self.runtime_base_url),
            "RUNTIME_API_TOKEN": self.runtime_api_token is not None,
            "POLICY_BASE_URL": bool(self.policy_base_url),
            "POLICY_API_TOKEN": self.policy_api_token is not None,
            "LANGFUSE_BASE_URL": bool(self.langfuse_base_url),
            "LANGFUSE_PUBLIC_KEY": self.langfuse_public_key is not None,
            "LANGFUSE_SECRET_KEY": self.langfuse_secret_key is not None,
            "JUDGE_MODEL": bool(self.judge_model),
        }
        return tuple(
            SecretStatus(
                name=name,
                configured=configured,
                required_for_selected_mode=name in required,
            )
            for name, configured in values.items()
        )
