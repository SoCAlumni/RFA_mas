from __future__ import annotations

import json
from pathlib import Path

from rfa_mas.contracts import EvalResult, EvaluationCase, RunResult
from rfa_mas.ports import JudgePort


def load_evaluation_cases(path: Path) -> list[EvaluationCase]:
    cases: list[EvaluationCase] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            cases.append(EvaluationCase.model_validate(json.loads(line)))
        except Exception as exc:
            raise ValueError(f"Invalid evaluation fixture {path}:{line_number}") from exc
    return cases


def deterministic_rule_checks(
    case: EvaluationCase,
    result: RunResult | None,
) -> dict[str, bool]:
    """Evaluate authoritative privacy, access, evidence, and binding rules."""

    draft = result.draft if result else None
    draft_available = draft is not None
    content = draft.content if draft else ""
    evidence_ids = {item.source_id for item in draft.allowed_evidence} if draft else set()
    allowed_source_ids = set(case.material_scope.allowed_source_ids)

    return {
        "run_result_available": result is not None,
        "draft_available": draft_available,
        "forbidden_information_absent": draft_available
        and all(marker not in content for marker in case.forbidden_information),
        "evidence_within_allowed_scope": draft_available
        and evidence_ids.issubset(allowed_source_ids),
        "expected_evidence_present": draft_available
        and set(case.expected_evidence).issubset(evidence_ids),
        "target_audience_matches_scope": bool(
            draft and draft.target.audience == case.material_scope.requested_audience
        ),
        "domain_matches_scope": bool(result and result.domain_id == case.material_scope.domain_id),
    }


async def evaluate_case(
    judge: JudgePort | None,
    case: EvaluationCase,
    result: RunResult | None,
) -> EvalResult:
    """Keep deterministic rules authoritative and add an optional Judge assessment."""

    rule_checks = deterministic_rule_checks(case, result)
    if judge is None or result is None:
        return EvalResult(
            case_id=case.case_id,
            run_id=result.run_id if result else None,
            trace_id=result.trace_id if result else None,
            rule_checks=rule_checks,
            judge_kind="not_run",
            judge_reason=(
                "Judge가 비활성화되어 보조 평가를 실행하지 않았습니다."
                if result
                else "실행 결과가 없어 Judge를 호출하지 않았습니다."
            ),
            executed=False,
            simulated=False,
        )

    assessment = await judge.evaluate(case, result)
    return EvalResult(
        case_id=case.case_id,
        run_id=result.run_id,
        trace_id=result.trace_id,
        rule_checks=rule_checks,
        judge_kind=assessment.kind,
        judge_score=assessment.score,
        judge_reason=assessment.reason,
        judge_dimensions=assessment.dimensions,
        judge_adapter=assessment.adapter,
        executed=True,
        simulated=assessment.simulated,
    )
