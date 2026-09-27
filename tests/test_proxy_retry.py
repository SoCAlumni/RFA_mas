"""Gemini's MALFORMED_FUNCTION_CALL finish is retried once by the egress-proxy (it made OpenClaw fail the
turn with "LLM request failed.": npu-sdk supervisor, 2026-09-28 03:12)."""

from __future__ import annotations

import json

import httpx
import pytest
from test_egress_proxy import client_for, make_proxy

from rfa_mas.nemoclaw import config as cfg
from rfa_mas.nemoclaw.proxy import malformed_function_call


class FlakyUpstream:
    """First call(s) end with Gemini's malformed-call finish, then a normal answer."""

    def __init__(self, malformed_times: int):
        self.malformed_times, self.calls = malformed_times, 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        body = json.loads(request.content)
        if self.calls <= self.malformed_times:
            choice = {"index": 0, "message": {"role": "assistant", "content": None},
                      "finish_reason": "function_call_filter: MALFORMED_FUNCTION_CALL"}
        else:
            choice = {"index": 0, "message": {"role": "assistant", "content": "정상 답"}, "finish_reason": "stop"}
        return httpx.Response(200, json={"id": "up", "object": "chat.completion", "created": 1, "model": body["model"],
                                         "choices": [choice],
                                         "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})


@pytest.fixture
def routing() -> cfg.Routing:
    return cfg.load_routing()


@pytest.fixture(autouse=True)
def audit_db(tmp_path, monkeypatch):
    monkeypatch.setenv("RFA_SG_AUDIT_DB", str(tmp_path / "audit.db"))


REQUEST = {"model": "rfa-auto", "messages": [{"role": "user", "content": "안녕"}]}


async def test_one_malformed_function_call_is_retried(routing):
    upstream = FlakyUpstream(malformed_times=1)
    async with client_for(make_proxy(routing, upstream)) as c:
        r = await c.post("/v1/chat/completions", json=REQUEST)
    choice = r.json()["choices"][0]
    assert r.status_code == 200 and upstream.calls == 2
    assert choice["message"]["content"] == "정상 답" and choice["finish_reason"] == "stop"


async def test_a_second_malformed_finish_is_passed_through(routing):
    upstream = FlakyUpstream(malformed_times=5)
    async with client_for(make_proxy(routing, upstream)) as c:
        r = await c.post("/v1/chat/completions", json=REQUEST)
    assert r.status_code == 200 and upstream.calls == 2  # one retry only
    assert "MALFORMED_FUNCTION_CALL" in r.json()["choices"][0]["finish_reason"]


def test_malformed_detection():
    assert malformed_function_call({"choices": [{"finish_reason": "function_call_filter: MALFORMED_FUNCTION_CALL"}]})
    assert malformed_function_call({"choices": [{"finish_reason": "MALFORMED_FUNCTION_CALL"}]})
    assert not malformed_function_call({"choices": [{"finish_reason": "stop"}]})
    assert not malformed_function_call({})
