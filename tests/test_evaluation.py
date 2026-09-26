from pathlib import Path

import pytest

from rfa_mas.application.evaluation import evaluate_case, load_evaluation_cases
from rfa_mas.contracts import (
    DraftTarget,
    JudgeAssessment,
    JudgeDimensions,
    RunResult,
    WorkRequest,
)


class FullScoreActualJudge:
    adapter_name = "actual-test-judge"
    simulated = False

    async def evaluate(self, case, result: RunResult) -> JudgeAssessment:
        return JudgeAssessment(
            kind="actual",
            score=1.0,
            reason="보조 품질 평가가 만점이어도 결정적 규칙을 변경하지 않습니다.",
            dimensions=JudgeDimensions(
                evidence_faithfulness=1.0,
                question_resolution=1.0,
                task_candidate_usefulness=1.0,
            ),
            simulated=False,
            adapter=self.adapter_name,
        )


class MustNotRunJudge:
    adapter_name = "must-not-run"
    simulated = False

    async def evaluate(self, case, result: RunResult) -> JudgeAssessment:
        raise AssertionError("Judge must not be called without a run result")


async def test_mock_judge_distinguishes_mock_score_from_not_run(container, principal) -> None:
    case = load_evaluation_cases(Path("fixtures/eval/evaluation_cases.jsonl"))[0]
    result = await container.service.run(
        WorkRequest(
            query=case.input,
            domain_id=case.material_scope.domain_id,
            target=DraftTarget(audience=case.material_scope.requested_audience),
        ),
        principal,
    )

    mock_evaluation = await evaluate_case(container.judge, case, result)
    not_run = await evaluate_case(container.judge, case, None)

    assert mock_evaluation.executed is True
    assert mock_evaluation.simulated is True
    assert mock_evaluation.judge_kind == "mock"
    assert mock_evaluation.judge_score is not None
    assert mock_evaluation.judge_dimensions is not None
    assert mock_evaluation.judge_adapter == "mock-judge"
    assert mock_evaluation.rule_checks["evidence_within_allowed_scope"] is True
    assert not_run.executed is False
    assert not_run.simulated is False
    assert not_run.judge_kind == "not_run"
    assert not_run.judge_score is None
    assert not_run.judge_dimensions is None
    assert not_run.judge_adapter is None
    assert all(value is False for value in not_run.rule_checks.values())


async def test_not_run_does_not_call_judge() -> None:
    case = load_evaluation_cases(Path("fixtures/eval/evaluation_cases.jsonl"))[0]

    evaluation = await evaluate_case(MustNotRunJudge(), case, None)

    assert evaluation.judge_kind == "not_run"
    assert evaluation.executed is False


async def test_disabled_judge_keeps_authoritative_rule_results(container, principal) -> None:
    case = load_evaluation_cases(Path("fixtures/eval/evaluation_cases.jsonl"))[0]
    result = await container.service.run(
        WorkRequest(
            query=case.input,
            domain_id=case.material_scope.domain_id,
            target=DraftTarget(audience=case.material_scope.requested_audience),
        ),
        principal,
    )

    evaluation = await evaluate_case(None, case, result)

    assert evaluation.judge_kind == "not_run"
    assert evaluation.executed is False
    assert evaluation.run_id == result.run_id
    assert evaluation.rule_checks["forbidden_information_absent"] is True
    assert evaluation.rule_checks["evidence_within_allowed_scope"] is True


async def test_actual_judge_cannot_override_authoritative_rules(container, principal) -> None:
    case = load_evaluation_cases(Path("fixtures/eval/evaluation_cases.jsonl"))[0]
    result = await container.service.run(
        WorkRequest(
            query=case.input,
            domain_id=case.material_scope.domain_id,
            target=DraftTarget(audience=case.material_scope.requested_audience),
        ),
        principal,
    )
    restricted_case = case.model_copy(
        update={
            "forbidden_information": ("요청 요약",),
            "material_scope": case.material_scope.model_copy(update={"allowed_source_ids": ()}),
        }
    )

    evaluation = await evaluate_case(FullScoreActualJudge(), restricted_case, result)

    assert evaluation.judge_kind == "actual"
    assert evaluation.judge_score == 1.0
    assert evaluation.simulated is False
    assert evaluation.rule_checks["forbidden_information_absent"] is False
    assert evaluation.rule_checks["evidence_within_allowed_scope"] is False


@pytest.mark.parametrize("invalid", [False, True])
async def test_legacy_judge_error_preserves_rules_without_fabricating_score(
    container, principal, invalid
):
    class BrokenJudge:
        async def evaluate(self, case, result):
            if invalid:
                return {"score": float("nan"), "reason": "SYNTHETIC_PRIVATE_CANARY_ERROR"}
            raise RuntimeError("SYNTHETIC_PRIVATE_CANARY_ERROR")

    case = load_evaluation_cases(Path("fixtures/eval/evaluation_cases.jsonl"))[0]
    result = await container.service.run(WorkRequest(query=case.input), principal)
    baseline = await evaluate_case(None, case, result)
    actual = await evaluate_case(BrokenJudge(), case, result)
    assert actual.rule_checks == baseline.rule_checks
    assert actual.judge_kind == "not_run" and not actual.executed
    assert actual.judge_score is None
    assert "SYNTHETIC_PRIVATE_CANARY" not in actual.model_dump_json()
