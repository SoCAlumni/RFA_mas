"""Censor pipeline: regex stage, LLM stage, redact default, fail-closed, judge parsing."""

from __future__ import annotations

import pytest

from rfa_mas.nemoclaw import config as cfg
from rfa_mas.nemoclaw.censor import (
    CensorPipeline,
    JudgeError,
    SandboxAgentJudge,
    StaticJudge,
    parse_judge_output,
)
from rfa_mas.nemoclaw.runner import CommandResult

TEXT = ("오로라 프로젝트의 P95 지연은 12.5 ms 이고 예산은 1,200,000 원 입니다. "
        "담당자 mail: lead@example.com. SYNTHETIC_PRIVATE_CANARY_ABC12 는 내부 표식입니다.")


@pytest.fixture
def censors() -> cfg.Censors:
    return cfg.load_censors()


def test_none_profile_is_a_no_op(censors):
    result = CensorPipeline(censors, StaticJudge("block")).run(TEXT, "none")
    assert result.verdict == "allow" and result.text == TEXT and result.stages == []


def test_regex_stage_redacts_numbers_keywords_and_canary(censors):
    judge = StaticJudge("allow")
    result = CensorPipeline(censors, judge).run(TEXT, "external")
    assert result.verdict == "redact"
    for marker in ("[REDACTED:project]", "[REDACTED:metric]", "[REDACTED:amount]",
                   "[REDACTED:email]", "[REDACTED:canary]"):
        assert marker in result.text, marker
    assert "12.5" not in result.text and "1,200,000" not in result.text and "오로라" not in result.text
    assert result.redactions["internal-project"] == 1 and result.redactions["canary"] == 1
    assert [s.id for s in result.stages] == ["regex", "llm"]
    assert judge.calls and "[REDACTED:project]" in judge.calls[0]  # LLM sees the regex-masked text


def test_credential_rule_blocks_instead_of_redacting(censors):
    result = CensorPipeline(censors, StaticJudge("allow")).run("token: nvapi-abcdefghijklmnop123456", "external")
    assert result.verdict == "block" and result.text == "" and result.blocked_by == "regex:credential"
    assert [s.id for s in result.stages] == ["regex"]  # LLM stage never runs after a block


def test_llm_stage_redacts_spans_and_blocks(censors):
    text = "네뷸라 후속 코드네임 Zephyr 는 아직 미공개입니다. 일정은 다음 분기입니다."
    redacting = CensorPipeline(censors, StaticJudge("redact", spans=["Zephyr", "다음 분기"]))
    result = redacting.run(text, "external")
    assert result.verdict == "redact" and "Zephyr" not in result.text and "[REDACTED:llm]" in result.text
    assert result.redactions == {"internal-project": 1, "llm": 2}
    blocking = CensorPipeline(censors, StaticJudge("block")).run(text, "external")
    assert blocking.verdict == "block" and blocking.text == ""


def test_llm_failure_is_fail_closed_by_default(censors):
    text = "미공개 로드맵 문서 초안 내용이 여기에 길게 이어집니다. 내부 검토 필요."
    result = CensorPipeline(censors, StaticJudge(error="timeout after 45s")).run(text, "external")
    assert result.verdict == "block" and result.blocked_by == "llm:error" and result.text == ""
    assert result.stages[-1].detail["error"].startswith("timeout")
    no_judge = CensorPipeline(censors, None).run(text, "external")
    assert no_judge.verdict == "block"


def test_llm_failure_degrades_open_only_when_declared(censors):
    relaxed = censors.model_copy(deep=True)
    stage = relaxed.profiles["external"].stages[1]
    assert isinstance(stage, cfg.LlmStage)
    stage.on_error = "allow"
    text = "미공개 로드맵 문서 초안 내용이 여기에 길게 이어집니다. 내부 검토 필요."
    result = CensorPipeline(relaxed, StaticJudge(error="boom")).run(text, "external")
    assert result.verdict == "allow" and result.stages[-1].detail.get("degraded") is True


def test_short_text_skips_llm_stage(censors):
    result = CensorPipeline(censors, StaticJudge("block")).run("안녕하세요", "external")
    assert result.verdict == "allow" and result.stages[-1].detail == {"skipped": "short"}


def test_unknown_profile_blocks(censors):
    assert CensorPipeline(censors).run("x", "does-not-exist").verdict == "block"


def test_judge_output_parsing_accepts_chatty_json_and_rejects_garbage():
    parsed = parse_judge_output('Sure! Here is the result:\n{"verdict": "redact", "spans": ["Zephyr"], "categories": ["internal_project"]}')
    assert parsed.verdict == "redact" and parsed.spans == ["Zephyr"]
    with pytest.raises(JudgeError):
        parse_judge_output("I think it is fine.")
    with pytest.raises(JudgeError):
        parse_judge_output('{"verdict": "maybe"}')


def test_sandbox_agent_judge_calls_nemoclaw_agent_and_parses_payload(censors):
    class Runner:
        def __init__(self):
            self.calls = []

        def run(self, argv, *, timeout=300, env=None, input_text=None, check=False):
            self.calls.append(list(argv))
            return CommandResult(list(argv), 0,
                                 '✓ Active gateway set\n{"status":"ok","result":{"payloads":[{"text":"{\\"verdict\\":\\"allow\\",\\"spans\\":[],\\"categories\\":[]}"}]}}', "")

    runner = Runner()
    stage = censors.profiles["external"].stages[1]
    verdict = SandboxAgentJudge(runner).classify("some text", stage)
    assert verdict.verdict == "allow"
    argv = runner.calls[0]
    assert argv[:4] == ["nemoclaw", "rfa-censor", "agent", "--agent"] and "--json" in argv
    assert argv[argv.index("--timeout") + 1] == str(stage.timeout_seconds)
    assert "some text" in argv[-1] and "JSON" in argv[-1]


def test_direct_judge_uses_ollama_native_chat_for_ollama_backends(monkeypatch, censors):
    from rfa_mas.nemoclaw.serve import DirectJudge

    routing = cfg.load_routing()
    stage = censors.profiles["external"].stages[1]
    seen = {}

    class Resp:
        status_code = 200

        def json(self):
            return {"message": {"role": "assistant", "content": '{"verdict":"redact","spans":["Zephyr"],"categories":["internal_project"]}'}}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.update({"url": url, "json": json, "timeout": timeout})
        return Resp()

    monkeypatch.setattr("rfa_mas.nemoclaw.serve.httpx.post", fake_post)
    verdict = DirectJudge(routing, {}).classify("코드네임 Zephyr", stage)
    assert verdict.verdict == "redact" and verdict.spans == ["Zephyr"]
    assert seen["url"] == "http://127.0.0.1:11434/api/chat" and seen["json"]["think"] is False
    assert seen["json"]["model"] == "nemotron-3-nano:4b" and seen["json"]["options"]["num_ctx"] == 32768
    assert seen["timeout"] == stage.timeout_seconds
