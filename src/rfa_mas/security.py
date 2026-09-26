from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any


class SecretRedactor:
    _BEARER_PATTERN = re.compile(r"(?i)(?P<scheme>\bbearer\s+)(?P<credential>[^\s,;\]\[{}()\"']+)")
    _NON_SECRET_TOKEN_KEYS = frozenset(
        {
            "input_tokens",
            "max_tokens",
            "output_tokens",
            "token_budget",
            "token_count",
            "token_limit",
            "token_usage",
            "total_tokens",
        }
    )

    def __init__(self, secrets: Sequence[str] = ()) -> None:
        self._secrets = tuple(value for value in secrets if value)

    def text(self, value: object) -> str:
        redacted = str(value)
        for secret in self._secrets:
            redacted = redacted.replace(secret, "[REDACTED]")
        redacted = self._BEARER_PATTERN.sub(r"\g<scheme>[REDACTED]", redacted)
        return redacted

    @classmethod
    def _is_sensitive_key(cls, key: str) -> bool:
        snake_case = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
        normalized = re.sub(r"[^a-z0-9]+", "_", snake_case.casefold()).strip("_")
        if normalized in cls._NON_SECRET_TOKEN_KEYS:
            return False
        components = normalized.split("_")
        return (
            normalized == "apikey"
            or "authorization" in components
            or "api_key" in normalized
            or "token" in components
            or "secret" in components
            or "password" in components
            or "credential" in components
            or "credentials" in components
        )

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            redacted: dict[Any, Any] = {}
            for key, item in value.items():
                safe_key = self.text(key) if isinstance(key, str) else key
                redacted[safe_key] = (
                    "[REDACTED]"
                    if isinstance(key, str) and self._is_sensitive_key(key)
                    else self.value(item)
                )
            return redacted
        if isinstance(value, tuple):
            return tuple(self.value(item) for item in value)
        if isinstance(value, list):
            return [self.value(item) for item in value]
        return value

    def json(self, value: Any) -> str:
        return json.dumps(self.value(value), ensure_ascii=False, sort_keys=True)


class RedactingLogFilter(logging.Filter):
    def __init__(self, redactor: SecretRedactor) -> None:
        super().__init__()
        self._redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._redactor.value(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(self._redactor.value(value) for value in record.args)
        elif isinstance(record.args, dict):
            record.args = self._redactor.value(record.args)
        return True


def configure_logging(level: str, redactor: SecretRedactor) -> None:
    handler = logging.StreamHandler()
    handler.addFilter(RedactingLogFilter(redactor))
    logging.basicConfig(level=level.upper(), handlers=[handler], force=True)
