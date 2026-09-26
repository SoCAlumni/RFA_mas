"""Opt-in direct hosted NVIDIA live smoke for P1-002A.

Only fixed synthetic Korean text is sent to the build.nvidia.com hosted endpoint.
This proves a real NVIDIA model call from this repository's settings boundary; it is
not evidence for the product ModelPort/bootstrap/graph path (P1-002).

Run explicitly (skip is not success):
    RFA_NVIDIA_LIVE=1 RFA_NVIDIA_ENV_FILE=/abs/path/.env.dev \
    RFA_NVIDIA_EVIDENCE_OUT=/abs/path/evidence.json \
    .venv/bin/python -m pytest -q tests/integration/test_nvidia_live.py

The four synthetic requests are sent concurrently once per module. Hosted latency
varies widely, so each case gets at most one retry for timeout/429/502/503/504; every
attempt, including failures, is recorded in the evidence file.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from rfa_mas.settings import Settings

LIVE_ENABLED = os.environ.get("RFA_NVIDIA_LIVE") == "1"
ENV_FILE = os.environ.get("RFA_NVIDIA_ENV_FILE")
EVIDENCE_OUT = os.environ.get("RFA_NVIDIA_EVIDENCE_OUT")
ALLOWED_HOST = "integrate.api.nvidia.com"
REQUEST_TIMEOUT_SECONDS = 170.0
MAX_ATTEMPTS = 2
RETRYABLE_STATUS = frozenset({429, 502, 503, 504})

pytestmark = pytest.mark.skipif(
    not (LIVE_ENABLED and ENV_FILE),
    reason="opt-in live NVIDIA smoke; skipped run is not evidence",
)

EVIDENCE_TEXT = (
    "[E1] 2026-10-02 합성 회의록: 베타 출시일은 11월 14일로 확정했다. 담당자는 김하늘이다.\n"
    "[E2] 합성 예산 메모: 3분기 GPU 예산은 1200만 원이다."
)
GROUNDED_SYSTEM = (
    "너는 사내 업무 비서다. 아래 근거만 사용해 한국어로 짧게 답한다. "
    "사용한 근거 ID를 [E1] 형식으로 답변에 인용한다. "
    "근거에 답이 없으면 추측하지 말고 정확히 '근거 부족'이라고 답한다.\n\n근거:\n" + EVIDENCE_TEXT
)
INSUFFICIENT_MARKER = "근거부족"

CASES: dict[str, tuple[list[str], dict[str, Any]]] = {
    "grounded_korean_answer": (
        ["AC1", "AC3"],
        {
            "temperature": 0,
            "max_tokens": 2048,
            "messages": [
                {"role": "system", "content": GROUNDED_SYSTEM},
                {"role": "user", "content": "베타 출시일과 담당자는 누구인가요?"},
            ],
        },
    ),
    "out_of_evidence_marker": (
        ["AC3"],
        {
            "temperature": 0,
            "max_tokens": 2048,
            "messages": [
                {"role": "system", "content": GROUNDED_SYSTEM},
                {"role": "user", "content": "베타 출시 행사 장소는 어디인가요?"},
            ],
        },
    ),
    "json_object_structured": (
        ["AC2"],
        {
            "temperature": 0,
            "max_tokens": 512,
            "response_format": {"type": "json_object"},
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [
                {
                    "role": "system",
                    "content": GROUNDED_SYSTEM
                    + "\n\n출력은 JSON 객체 하나다. 키는 answer(문자열), "
                    'evidence_ids(문자열 배열, 예: ["E1"]), supported(불리언)만 사용한다.',
                },
                {"role": "user", "content": "베타 출시일은 언제인가요?"},
            ],
        },
    ),
    "named_tool_choice_proposal": (
        ["AC2"],
        {
            "temperature": 0,
            "max_tokens": 256,
            "chat_template_kwargs": {"enable_thinking": False},
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "lookup_schedule",
                        "description": "합성 프로젝트 일정 조회",
                        "parameters": {
                            "type": "object",
                            "properties": {"project": {"type": "string"}},
                            "required": ["project"],
                            "additionalProperties": False,
                        },
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": "lookup_schedule"}},
            "messages": [{"role": "user", "content": "프로젝트 알파의 일정을 조회해줘."}],
        },
    ),
}


class GroundedAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    answer: str
    evidence_ids: list[str]
    supported: bool


class ScheduleArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    project: str


def _sha256(value: bytes | str) -> str:
    data = value.encode() if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def _compact(text: str) -> str:
    return "".join(text.split())


def _hangul_ratio(text: str) -> float:
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return 0.0
    return sum("\uac00" <= ch <= "\ud7a3" for ch in letters) / len(letters)


class HostedChat:
    """Minimal hosted chat/completions caller; credential stays inside SecretStr."""

    def __init__(self, settings: Settings) -> None:
        base = str(settings.nvidia_base_url or "").rstrip("/")
        parts = urlsplit(base)
        if parts.scheme != "https" or parts.hostname != ALLOWED_HOST:
            pytest.fail("NVIDIA_BASE_URL must be the https hosted endpoint for this smoke")
        if settings.nvidia_api_key is None or not settings.nvidia_model:
            pytest.fail("NVIDIA_API_KEY/NVIDIA_MODEL missing in the explicit env file")
        self.endpoint = f"{parts.scheme}://{parts.hostname}{parts.path}/chat/completions"
        self.model = settings.nvidia_model
        self._secret = settings.nvidia_api_key
        self._http = httpx.Client(
            timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False
        )

    def close(self) -> None:
        self._http.close()

    def secret_in(self, text: str) -> bool:
        return self._secret.get_secret_value() in text

    def run_case(self, case: str) -> dict[str, Any]:
        ac_ids, payload = CASES[case]
        body = {"model": self.model, "stream": False, **payload}
        raw = json.dumps(body, ensure_ascii=False, sort_keys=True).encode()
        record: dict[str, Any] = {
            "case": case,
            "ac_ids": ac_ids,
            "request_sha256": _sha256(raw),
            "model_requested": self.model,
            "attempts": [],
        }
        for _ in range(MAX_ATTEMPTS):
            attempt, response = self._post(raw)
            record["attempts"].append(attempt)
            if response is not None and response.status_code == 200:
                return self._success(record, response)
            retryable = response is None or response.status_code in RETRYABLE_STATUS
            if not retryable:
                break
        record["outcome"] = "failed"
        return {"record": record}

    def _post(self, raw: bytes) -> tuple[dict[str, Any], httpx.Response | None]:
        started = time.monotonic()
        try:
            response = self._http.post(
                self.endpoint,
                content=raw,
                headers={
                    "Authorization": "Bearer " + self._secret.get_secret_value(),
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
        except httpx.HTTPError as error:
            latency = round(time.monotonic() - started, 2)
            return {"error": type(error).__name__, "latency_seconds": latency}, None
        latency = round(time.monotonic() - started, 2)
        # 202 pending and every non-200 status are failures for this smoke; no body is kept.
        return {"http_status": response.status_code, "latency_seconds": latency}, response

    def _success(self, record: dict[str, Any], response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
            choice = data["choices"][0]
            message = choice["message"]
        except (ValueError, KeyError, IndexError, TypeError):
            record["outcome"] = "unexpected_shape"
            return {"record": record}
        content = message.get("content") or ""
        record.update(
            {
                "outcome": "http_200",
                "model_echo": data.get("model"),
                "finish_reason": choice.get("finish_reason"),
                "usage": data.get("usage"),
                "response_content_sha256": _sha256(content),
                "response_content_chars": len(content),
                "response_excerpt": content[:160],
                "reasoning_present": bool(message.get("reasoning_content")),
                "tool_call_count": len(message.get("tool_calls") or []),
            }
        )
        return {"record": record, "message": message, "content": content}


@pytest.fixture(scope="module")
def hosted() -> Iterator[tuple[HostedChat, dict[str, dict[str, Any]]]]:
    env_file = Path(ENV_FILE or "")
    if env_file.is_symlink() or not env_file.is_file():
        pytest.fail("RFA_NVIDIA_ENV_FILE must be an explicit regular file")
    client = HostedChat(Settings(_env_file=env_file))
    started = time.monotonic()
    try:
        with ThreadPoolExecutor(max_workers=len(CASES)) as pool:
            results = dict(zip(CASES, pool.map(client.run_case, CASES), strict=True))
        wall = round(time.monotonic() - started, 2)
        yield client, results
    finally:
        client.close()
    _write_evidence(client, results, wall)


def _write_evidence(client: HostedChat, results: dict[str, dict[str, Any]], wall: float) -> None:
    if not EVIDENCE_OUT:
        return
    document = {
        "task": "P1-002A",
        "kind": "direct_hosted_api_smoke",
        "simulated": False,
        "product_model_port_path": "not_run",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "endpoint": client.endpoint,
        "model": client.model,
        "concurrent_wall_seconds": wall,
        "calls": [result["record"] for result in results.values()],
        "stored": "hashes, counts and synthetic excerpts only; no credential or reasoning text",
    }
    text = json.dumps(document, ensure_ascii=False, indent=2)
    if client.secret_in(text):
        pytest.fail("evidence would contain the credential; not written")
    Path(EVIDENCE_OUT).write_text(text + "\n", encoding="utf-8")


def _completed(hosted: tuple[HostedChat, dict[str, dict[str, Any]]], case: str) -> dict[str, Any]:
    client, results = hosted
    result = results[case]
    record = result["record"]
    if record.get("outcome") != "http_200":
        pytest.fail(f"{case}: {record.get('outcome')} after {len(record['attempts'])} attempts")
    assert record["model_echo"] == client.model
    assert (record["usage"] or {}).get("total_tokens", 0) > 0
    return result


def _assert_text_answer(result: dict[str, Any]) -> None:
    assert result["record"]["finish_reason"] == "stop"
    assert result["content"].strip()


def test_grounded_korean_answer_cites_evidence(hosted) -> None:
    result = _completed(hosted, "grounded_korean_answer")
    _assert_text_answer(result)
    content = result["content"]
    checks = {
        "release_date": "11월14일" in _compact(content),
        "owner": "김하늘" in content,
        "cites_e1": "[E1]" in content,
        "korean": _hangul_ratio(content) >= 0.5,
    }
    result["record"]["checks"] = checks
    assert all(checks.values()), checks


def test_out_of_evidence_question_marks_insufficient(hosted) -> None:
    result = _completed(hosted, "out_of_evidence_marker")
    _assert_text_answer(result)
    checks = {"insufficient_marker": INSUFFICIENT_MARKER in _compact(result["content"])}
    result["record"]["checks"] = checks
    assert all(checks.values()), checks


def test_json_object_response_validates_with_pydantic(hosted) -> None:
    result = _completed(hosted, "json_object_structured")
    _assert_text_answer(result)
    try:
        parsed = GroundedAnswer.model_validate_json(result["content"])
    except ValidationError as error:
        result["record"]["checks"] = {"pydantic_valid": False}
        pytest.fail(f"json_object content failed schema: {error.error_count()} errors")
    ids = {item.strip("[] ") for item in parsed.evidence_ids}
    checks = {
        "pydantic_valid": True,
        "supported": parsed.supported is True,
        "cites_e1": "E1" in ids,
        "release_date": "11월14일" in _compact(parsed.answer),
    }
    result["record"]["checks"] = checks
    assert all(checks.values()), checks


def test_named_tool_choice_returns_proposal_without_execution(hosted) -> None:
    result = _completed(hosted, "named_tool_choice_proposal")
    record = result["record"]
    calls = result["message"].get("tool_calls") or []
    assert record["finish_reason"] == "tool_calls"
    assert len(calls) == 1
    function = calls[0].get("function") or {}
    try:
        args = ScheduleArgs.model_validate_json(function.get("arguments") or "")
    except ValidationError as error:
        record["checks"] = {"arguments_valid": False}
        pytest.fail(f"tool arguments failed schema: {error.error_count()} errors")
    checks = {
        "named_function": function.get("name") == "lookup_schedule",
        "arguments_valid": True,
        "project_mentions_alpha": "알파" in args.project,
    }
    record["checks"] = checks
    record["tool_executions"] = 0
    assert all(checks.values()), checks
