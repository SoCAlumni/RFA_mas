"""Censor pipeline: regex stage → LLM stage, redact by default, fail-closed.

One function, ``CensorPipeline.run``, serves every censoring point: the egress-proxy (requests
to and responses from the external LLM) and the channel API's final reply. The LLM stage is an
OpenClaw agent in the ``rfa-censor`` sandbox (egress 0, local inference) that returns JSON only;
any timeout, transport or parse failure blocks the text (``on_error: block``).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Literal, Protocol

from rfa_mas.nemoclaw.config import Censors, LlmStage, RegexRule, RegexStage
from rfa_mas.nemoclaw.runner import Runner, extract_json

Verdict = Literal["allow", "redact", "block"]
_ORDER = {"allow": 0, "redact": 1, "block": 2}


@dataclass
class JudgeVerdict:
    verdict: Verdict
    spans: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    raw: str = ""


class JudgeError(RuntimeError):
    """The LLM stage could not produce a verdict (timeout, transport, parse)."""


class LlmJudge(Protocol):
    def classify(self, text: str, stage: LlmStage) -> JudgeVerdict: ...


@dataclass
class StageReport:
    id: str
    type: str
    verdict: Verdict
    ms: int
    detail: dict = field(default_factory=dict)


@dataclass
class CensorResult:
    text: str
    verdict: Verdict
    profile: str
    stages: list[StageReport] = field(default_factory=list)
    redactions: dict[str, int] = field(default_factory=dict)
    blocked_by: str | None = None

    @property
    def redacted_count(self) -> int:
        return sum(self.redactions.values())

    def summary(self) -> dict:
        return {
            "profile": self.profile,
            "verdict": self.verdict,
            "redactions": dict(self.redactions),
            "blocked_by": self.blocked_by,
            "stages": [{"id": s.id, "type": s.type, "verdict": s.verdict, "ms": s.ms, **s.detail} for s in self.stages],
        }


JUDGE_PROMPT = (
    "You are a strict data-leak classifier for a company assistant. Decide whether the TEXT below "
    "may leave the company network. Categories to catch: {categories}. "
    "Reply with ONE line of JSON only, no prose, no markdown: "
    '{{"verdict":"allow"|"redact"|"block","spans":["exact substrings to mask"],"categories":["..."]}}. '
    "Use \"redact\" when masking the spans makes the text safe, \"block\" only for credentials or "
    "when masking cannot make it safe, \"allow\" when nothing sensitive appears.\n"
    "TEXT:\n<<<\n{text}\n>>>"
)


def parse_judge_output(raw: str) -> JudgeVerdict:
    """Accept the first JSON object in a possibly chatty completion; anything else is an error."""
    try:
        data = extract_json(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise JudgeError(f"judge output is not JSON: {raw[:120]!r}") from exc
    if not isinstance(data, dict):
        raise JudgeError("judge output is not an object")
    verdict = str(data.get("verdict", "")).lower()
    if verdict not in _ORDER:
        raise JudgeError(f"judge verdict unknown: {verdict!r}")
    spans = [str(s) for s in (data.get("spans") or []) if str(s).strip()]
    categories = [str(c) for c in (data.get("categories") or [])]
    return JudgeVerdict(verdict, spans, categories, raw)  # type: ignore[arg-type]


class SandboxAgentJudge:
    """LLM stage through ``nemoclaw <censor-sandbox> agent`` (OpenClaw turn, local inference)."""

    def __init__(self, runner: Runner, nemoclaw_bin: str = "nemoclaw"):
        self.runner = runner
        self.bin = nemoclaw_bin

    def classify(self, text: str, stage: LlmStage) -> JudgeVerdict:
        prompt = JUDGE_PROMPT.format(categories=", ".join(stage.categories), text=text)
        result = self.runner.run(
            [self.bin, stage.sandbox, "agent", "--agent", stage.agent, "--thinking", "off", "--json",
             "--timeout", str(stage.timeout_seconds), "--session-id", "rfa-censor-stage", "-m", prompt],
            timeout=stage.timeout_seconds + 15,
        )
        if not result.ok:
            raise JudgeError(f"censor agent rc={result.returncode}: {(result.stderr or result.stdout)[-160:]}")
        try:
            data = extract_json(result.stdout)
            payloads = data.get("result", {}).get("payloads", [])
            answer = "\n".join(p.get("text") or "" for p in payloads)
        except (ValueError, AttributeError, json.JSONDecodeError) as exc:
            raise JudgeError("censor agent returned no payload") from exc
        return parse_judge_output(answer)


class StaticJudge:
    """Deterministic judge for tests and replay mode."""

    def __init__(self, verdict: Verdict = "allow", spans: list[str] | None = None, error: str | None = None,
                 delay: float = 0.0):
        self.verdict, self.spans, self.error, self.delay = verdict, spans or [], error, delay
        self.calls: list[str] = []

    def classify(self, text: str, stage: LlmStage) -> JudgeVerdict:
        self.calls.append(text)
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise JudgeError(self.error)
        return JudgeVerdict(self.verdict, [s for s in self.spans if s in text], ["static"], "")


def _compile(rule: RegexRule) -> re.Pattern[str]:
    if rule.pattern:
        return re.compile(rule.pattern)
    return re.compile("|".join(re.escape(k) for k in rule.keywords))


class CensorPipeline:
    def __init__(self, censors: Censors, judge: LlmJudge | None = None):
        self.censors = censors
        self.judge = judge
        self._compiled: dict[str, re.Pattern[str]] = {}

    def _pattern(self, rule: RegexRule) -> re.Pattern[str]:
        key = f"{rule.id}:{rule.pattern}:{'|'.join(rule.keywords)}"
        if key not in self._compiled:
            self._compiled[key] = _compile(rule)
        return self._compiled[key]

    def run(self, text: str, profile: str, stages: tuple[str, ...] | None = None) -> CensorResult:
        """Censor ``text`` under ``profile``; ``stages`` limits stage types (e.g. regex only for
        tool-call arguments). Unknown profiles block (fail-closed)."""
        spec = self.censors.profiles.get(profile)
        if spec is None:
            return CensorResult("", "block", profile, blocked_by="unknown-profile")
        result = CensorResult(text, "allow", profile)
        if not spec.stages or not text:
            return result
        for stage in spec.stages:
            if stages is not None and stage.type not in stages:
                continue
            if isinstance(stage, RegexStage):
                self._regex(stage, result)
            elif isinstance(stage, LlmStage):
                self._llm(stage, result)
            if result.verdict == "block":
                result.text = ""
                break
        return result

    def _regex(self, stage: RegexStage, result: CensorResult) -> None:
        started = time.monotonic()
        hits: dict[str, int] = {}
        text = result.text
        for rule in stage.rules:
            pattern = self._pattern(rule)
            text, n = pattern.subn(rule.replacement, text)
            if n:
                hits[rule.id] = n
                if rule.action == "block":
                    result.verdict = "block"
                    result.blocked_by = f"{stage.id}:{rule.id}"
                    result.redactions.update(hits)
                    result.stages.append(StageReport(stage.id, "regex", "block", _ms(started), {"rules": hits}))
                    return
        result.text = text
        verdict: Verdict = "redact" if hits else "allow"
        result.redactions.update(hits)
        result.verdict = _max(result.verdict, verdict)
        result.stages.append(StageReport(stage.id, "regex", verdict, _ms(started), {"rules": hits}))

    def _llm(self, stage: LlmStage, result: CensorResult) -> None:
        started = time.monotonic()
        if len(result.text) < stage.skip_if_shorter_than:
            result.stages.append(StageReport(stage.id, "llm", "allow", _ms(started), {"skipped": "short"}))
            return
        if self.judge is None:
            self._llm_error(stage, result, started, "no judge configured")
            return
        try:
            verdict = self.judge.classify(result.text, stage)
        except JudgeError as exc:
            self._llm_error(stage, result, started, str(exc))
            return
        except Exception as exc:  # transport failures are fail-closed too
            self._llm_error(stage, result, started, f"{type(exc).__name__}: {exc}")
            return
        if verdict.verdict == "block":
            result.verdict = "block"
            result.blocked_by = f"{stage.id}:{','.join(verdict.categories) or 'llm'}"
            result.stages.append(StageReport(stage.id, "llm", "block", _ms(started),
                                             {"categories": verdict.categories}))
            return
        masked = 0
        if verdict.verdict == "redact":
            text = result.text
            for span in sorted(set(verdict.spans), key=len, reverse=True):
                if span and span in text:
                    text = text.replace(span, "[REDACTED:llm]")
                    masked += 1
            result.text = text
            if masked:
                result.redactions[f"{stage.id}"] = masked
        effective: Verdict = "redact" if masked else "allow"
        result.verdict = _max(result.verdict, effective)
        result.stages.append(StageReport(stage.id, "llm", effective, _ms(started),
                                         {"categories": verdict.categories, "spans": masked}))

    def _llm_error(self, stage: LlmStage, result: CensorResult, started: float, error: str) -> None:
        if stage.on_error == "block" and self.censors.fail_closed:
            result.verdict = "block"
            result.blocked_by = f"{stage.id}:error"
            result.stages.append(StageReport(stage.id, "llm", "block", _ms(started), {"error": error[:200]}))
        else:
            result.stages.append(StageReport(stage.id, "llm", "allow", _ms(started), {"error": error[:200], "degraded": True}))


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _max(a: Verdict, b: Verdict) -> Verdict:
    return a if _ORDER[a] >= _ORDER[b] else b
