"""P1-006A contract: NVIDIA Judge adapter over httpx.MockTransport (no network, no key).

Deterministic rule checks stay authoritative; these tests pin the egress gate, the wire
payload, retry/deadline bounds, response validation and the not_run/error mapping in
evaluate_case. A MockTransport pass is never live Judge evidence (that is the opt-in
tests/integration/test_judge_live.py).
"""

from __future__ import annotations

import json
import traceback

import httpx
import pytest
from pydantic import SecretStr

from rfa_mas.adapters.nvidia import NvidiaChatConfig
from rfa_mas.adapters.nvidia_judge import (
    PROMPT_VERSION,
    RUBRIC_VERSION,
    NvidiaJudge,
    SyntheticPublicJudgeGate,
)
from rfa_mas.application.evaluation import evaluate_case
from rfa_mas.contracts import (
    AdapterInfo,
    Audience,
    DomainId,
    DraftBundle,
    DraftTarget,
    EvaluationCase,
    EvaluationCaseV11,
    EvidenceRef,
    RunResult,
    SourceLocation,
    WorkStatus,
    sha256_text,
)
from rfa_mas.errors import RfaError

BASE = "https://integrate.api.nvidia.com/v1"
ENDPOINT = BASE + "/chat/completions"
MODEL = "nvidia/nemotron-3-ultra-550b-a55b"
KEY = "nvapi-SYNTHETIC-JUDGE-KEY-0000000000000000"
CANARY = "SYNTHETIC_PRIVATE_CANARY_JUDGE_J1"


class Clock:
    def __init__(self) -> None:
        self.now = 500.0

    def __call__(self) -> float:
        return self.now


class Sleeper:
    def __init__(self, clock: Clock) -> None:
        self.clock, self.slept = clock, []

    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.clock.now += seconds


class Server:
    def __init__(self, *script) -> None:
        self.script, self.requests = list(script), []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(step, Exception):
            raise step
        return step


def completion(content, finish="stop", **message) -> httpx.Response:
    body = {
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "finish_reason": finish,
                "message": {"role": "assistant", "content": content, **message},
            }
        ],
    }
    return httpx.Response(200, json=body)


def scored(**values) -> httpx.Response:
    payload = {
        "evidence_faithfulness": 0.9,
        "question_resolution": 0.8,
        "task_candidate_usefulness": 0.6,
        "expression_and_team_fit": 0.7,
        "reason": "근거 라벨을 인용했고 요청에 답했다.",
    } | values
    return completion(json.dumps(payload, ensure_ascii=False))


def case(**changes) -> EvaluationCase:
    values = {
        "schema_version": "1.1",
        "case_id": "J01",
        "persona": "external",
        "input": "TRIV-DEMO SDK 공식 출시일을 알려줘.",
        "material_scope": {
            "domain_id": "triv3",
            "authenticated_principal": "fixture-external-001",
            "memberships": [],
            "requested_audience": "public",
            "allowed_source_ids": ["triv3-public-overview"],
        },
        "expected_evidence": ["triv3-public-overview"],
        "forbidden_information": [CANARY],
        "expected_behavior": "answer_with_public_evidence",
        "scenario_id": "J01-judge",
        "identity_fixture_id": "external",
        "fixture_ref": "judge-contract-J01",
        "dataset_version": "judge-contract-v1",
        "seed": 1,
        "expected_observations": ["identity"],
    } | changes
    return EvaluationCaseV11.model_validate(values)


def run_result(
    audience=Audience.PUBLIC,
    content="공식 출시일은 2026-10-20입니다. [E1]",
    evidence_audience=Audience.PUBLIC,
    draft=True,
) -> RunResult:
    bundle = (
        DraftBundle(
            request_id="req-1",
            trace_id="trace-1",
            run_id="run-1",
            agent_id="assistant",
            domain_id=DomainId.TRIV3,
            content_hash=sha256_text(content),
            target=DraftTarget(audience=audience),
            audience=audience,
            policy_version="local-v1",
            allowed_evidence=(
                EvidenceRef(
                    source_id="triv3-public-overview",
                    source_revision="1",
                    location=SourceLocation(uri="fixture://doc", section="public-overview"),
                    audience=evidence_audience,
                    content_hash="0" * 64,
                ),
            ),
            content=content,
            simulated=True,
            adapter="mock-model",
        )
        if draft
        else None
    )
    return RunResult(
        request_id="req-1",
        trace_id="trace-1",
        run_id="run-1",
        agent_id="assistant",
        domain_id=DomainId.TRIV3,
        status=WorkStatus.COMPLETED,
        draft=bundle,
        stop_reason="completed",
        simulated=True,
        adapters=(AdapterInfo(port="model", adapter="mock-model", simulated=True),),
    )


def judge(server: Server, clock: Clock | None = None, **gate_values):
    clock = clock or Clock()
    sleeper = Sleeper(clock)
    config = NvidiaChatConfig(base_url=BASE, model=MODEL, api_key=SecretStr(KEY))
    gate = SyntheticPublicJudgeGate(
        **(
            {
                "endpoint": ENDPOINT,
                "model": MODEL,
                "max_output_tokens": 256,
                "budget_seconds": 60,
                "clock": clock,
            }
            | gate_values
        )
    )
    adapter = NvidiaJudge(
        config, gate, transport=httpx.MockTransport(server), sleep=sleeper, clock=clock
    )
    return adapter, sleeper


async def test_actual_assessment_from_exact_payload():
    server = Server(scored())
    adapter, _ = judge(server)
    assessment = await adapter.evaluate(case(), run_result())
    assert assessment.kind == "actual" and assessment.simulated is False
    assert assessment.adapter == "nvidia-judge"
    assert assessment.dimensions.model_dump(exclude={"schema_version"}) == {
        "evidence_faithfulness": 0.9,
        "question_resolution": 0.8,
        "task_candidate_usefulness": 0.6,
    }
    assert assessment.score == pytest.approx((0.9 + 0.8 + 0.6) / 3)
    assert RUBRIC_VERSION in assessment.reason and PROMPT_VERSION in assessment.reason
    assert "expression_and_team_fit=0.70" in assessment.reason
    assert "권한·개인정보 판정" in assessment.reason
    (sent,) = server.requests
    assert str(sent.url) == ENDPOINT and sent.headers["authorization"] == f"Bearer {KEY}"
    body = json.loads(sent.content)
    assert body["model"] == MODEL and body["stream"] is False and body["max_tokens"] == 256
    assert body["response_format"] == {"type": "json_object"}
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    user = body["messages"][1]["content"]
    assert RUBRIC_VERSION in user and "triv3-public-overview@1" in user
    assert "공식 출시일은 2026-10-20입니다" in user
    assert CANARY not in json.dumps(body, ensure_ascii=False)  # forbidden strings never shipped


@pytest.mark.parametrize(
    "variant",
    [
        "legacy_case",
        "internal_audience",
        "private_evidence",
        "marker_in_draft",
        "marker_in_input",
        "no_draft",
        "other_model",
    ],
)
async def test_gate_refuses_without_sending(variant):
    server = Server(scored())
    c, r, gate_values = case(), run_result(), {}
    if variant == "legacy_case":
        c = EvaluationCase.model_validate(
            case().model_dump(
                exclude={
                    "schema_version",
                    "scenario_id",
                    "identity_fixture_id",
                    "fixture_ref",
                    "dataset_version",
                    "seed",
                    "synthetic",
                    "expected_observations",
                }
            )
        )
    elif variant == "internal_audience":
        c = case(
            material_scope=case().material_scope.model_dump()
            | {"requested_audience": "business_unit", "memberships": ["triv3-team"]}
        )
        r = run_result(audience=Audience.BUSINESS_UNIT)
    elif variant == "private_evidence":
        r = run_result(evidence_audience=Audience.PRIVATE)
    elif variant == "marker_in_draft":
        r = run_result(content=f"공식 출시일 {CANARY}")
    elif variant == "marker_in_input":
        c = case(input=f"일정 알려줘 {CANARY}")
    elif variant == "no_draft":
        r = run_result(draft=False)
    else:
        gate_values = {"model": "other/model"}
    adapter, _ = judge(server, **gate_values)
    with pytest.raises(RfaError) as error:
        await adapter.evaluate(c, r)
    assert error.value.code == "judge_egress_not_permitted"
    assert server.requests == []


async def test_evaluate_case_keeps_rules_and_maps_failures_to_not_run():
    ok, _ = judge(Server(scored()))
    result = await evaluate_case(ok, case(), run_result())
    assert result.judge_kind == "actual" and result.executed and not result.simulated
    assert result.judge_adapter == "nvidia-judge" and result.rule_checks
    refused, _ = judge(Server(scored()))
    result = await evaluate_case(refused, case(), run_result(evidence_audience=Audience.PRIVATE))
    assert result.judge_kind == "not_run" and result.judge_reason == "judge_error"
    assert result.judge_score is None and result.rule_checks
    failing, _ = judge(Server(httpx.Response(500)))
    result = await evaluate_case(failing, case(), run_result())
    assert result.judge_kind == "not_run" and not result.executed
    assert result.judge_score is None and result.judge_dimensions is None


async def test_retries_are_bounded_by_grant_and_deadline():
    server = Server(httpx.Response(429, headers={"Retry-After": "3"}), scored())
    adapter, sleeper = judge(server)
    assessment = await adapter.evaluate(case(), run_result())
    assert assessment.kind == "actual" and sleeper.slept == [3.0] and len(server.requests) == 2
    stuck = Server(httpx.Response(503))
    adapter, sleeper = judge(stuck)
    with pytest.raises(RfaError) as error:
        await adapter.evaluate(case(), run_result())
    assert error.value.code == "judge_unavailable" and error.value.retryable
    assert len(stuck.requests) == 3 and sleeper.slept == [1.0, 2.0]
    clock = Clock()
    slow = Server(httpx.Response(429, headers={"Retry-After": "120"}))
    adapter, sleeper = judge(slow, clock)
    with pytest.raises(RfaError) as error:
        await adapter.evaluate(case(), run_result())
    assert error.value.code == "judge_rate_limited" and sleeper.slept == []
    assert len(slow.requests) == 1


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(401), "judge_auth_failed"),
        (httpx.Response(422), "judge_request_rejected"),
        (httpx.Response(202, json={"status": "queued"}), "judge_pending"),
        (
            httpx.Response(307, headers={"Location": "https://attacker.invalid/x"}),
            "judge_request_rejected",
        ),
        (httpx.Response(200, content=b"nope"), "judge_invalid_response"),
        (completion("plain text"), "judge_invalid_response"),
        (
            completion(
                '{"evidence_faithfulness": 1.5, "question_resolution": 1, '
                '"task_candidate_usefulness": 1, "expression_and_team_fit": 1, "reason": "x"}'
            ),
            "judge_invalid_response",
        ),
        (
            completion('{"evidence_faithfulness": 1, "question_resolution": 1}'),
            "judge_invalid_response",
        ),
        (
            completion(
                '{"evidence_faithfulness": 1, "question_resolution": 1, '
                '"task_candidate_usefulness": 1, "expression_and_team_fit": 1, "reason": "x", '
                '"privacy_ok": true}'
            ),
            "judge_invalid_response",
        ),
        (
            completion(None, finish="tool_calls", tool_calls=[{"type": "function"}]),
            "judge_invalid_response",
        ),
        (
            completion(
                '{"evidence_faithfulness": 1, "question_resolution": 1, '
                '"task_candidate_usefulness": 1, "expression_and_team_fit": 1, "reason": "x"}',
                finish="length",
            ),
            "judge_invalid_response",
        ),
    ],
)
async def test_bad_responses_are_distinct_errors_sent_once(response, code):
    server = Server(response)
    adapter, sleeper = judge(server)
    with pytest.raises(RfaError) as error:
        await adapter.evaluate(case(), run_result())
    assert error.value.code == code and len(server.requests) == 1 and sleeper.slept == []


async def test_score_cannot_encode_privacy_or_access_decisions():
    """A perfect Judge score changes nothing about rule checks or the wire contract."""
    server = Server(
        scored(
            evidence_faithfulness=1.0,
            question_resolution=1.0,
            task_candidate_usefulness=1.0,
            reason="완벽하다.",
        )
    )
    adapter, _ = judge(server)
    assessment = await adapter.evaluate(case(), run_result())
    assert set(assessment.dimensions.model_dump(exclude={"schema_version"})) == {
        "evidence_faithfulness",
        "question_resolution",
        "task_candidate_usefulness",
    }
    result = await evaluate_case(adapter, case(), run_result(content="근거 없이 단정한다."))
    assert result.judge_score == 1.0 and result.rule_checks  # rules are computed independently


async def test_errors_never_carry_key_draft_or_provider_text():
    servers = [
        Server(httpx.Response(401, text=f"bad key {KEY}")),
        Server(httpx.ConnectError(f"refused {KEY}")),
        Server(completion(f"garbage {CANARY}")),
    ]
    for server in servers:
        adapter, _ = judge(server)
        with pytest.raises(RfaError) as error:
            await adapter.evaluate(case(), run_result())
        rendered = "".join(traceback.format_exception(error.value)) + repr(error.value)
        assert KEY not in rendered and CANARY not in rendered and "출시일" not in rendered
        assert error.value.__cause__ is None


async def test_bootstrap_nvidia_judge_opt_in_wires_gate_and_closes_client(tmp_path):
    """Proposal wiring (shared bootstrap; coordinator applies)."""
    from rfa_mas.bootstrap import build_container
    from rfa_mas.settings import Settings

    base = {
        "_env_file": None,
        "database_url": f"sqlite:///{tmp_path / 'j.db'}",
        "trace_dir": (tmp_path / "traces").resolve(),
    }
    default = build_container(Settings(**base))
    assert default.judge.adapter_name == "mock-judge"
    disabled = build_container(
        Settings(**base, judge_provider="nvidia", judge_model=MODEL, nvidia_api_key=SecretStr(KEY))
    )
    assert disabled.judge.adapter_name == "mock-judge"  # ENABLE_JUDGE is the opt-in
    enabled = build_container(
        Settings(
            **base,
            enable_judge=True,
            judge_provider="nvidia",
            judge_model=MODEL,
            nvidia_api_key=SecretStr(KEY),
        )
    )
    await enabled.startup()
    try:
        assert isinstance(enabled.judge, NvidiaJudge) and enabled.judge.simulated is False
        assert enabled.judge.config.model == MODEL and enabled.judge.config.endpoint == ENDPOINT
        assert isinstance(enabled.judge.gate, SyntheticPublicJudgeGate)
        # Key presence never sends: a legacy 1.0 case is refused before any request.
        legacy = EvaluationCase.model_validate(
            case().model_dump(
                exclude={
                    "schema_version",
                    "scenario_id",
                    "identity_fixture_id",
                    "fixture_ref",
                    "dataset_version",
                    "seed",
                    "synthetic",
                    "expected_observations",
                }
            )
        )
        result = await evaluate_case(enabled.judge, legacy, run_result())
        assert result.judge_kind == "not_run" and result.judge_reason == "judge_error"
    finally:
        await enabled.shutdown()
    assert enabled.judge._client.is_closed
