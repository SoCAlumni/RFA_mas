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


# -- P1-006B persona regression v2 -------------------------------------------------------
import asyncio  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import sys  # noqa: E402

from pydantic import ValidationError  # noqa: E402

from rfa_mas.application import evaluation as ev  # noqa: E402
from rfa_mas.contracts import (  # noqa: E402
    EvaluationStatus,
    EvidenceItem,
    SourceLocation,
    sha256_text,
)

_STATUS = EvaluationStatus


@pytest.fixture(scope="module")
def baseline():
    # One actual simulated run of all 24 cases, reused read-only by the tests below.
    return asyncio.run(ev.run_persona_regression(label="baseline"))


def test_v2_dataset_is_24_distinct_persona_situations_with_valid_crosswalk(monkeypatch, tmp_path):
    cases = ev.load_regression_cases()
    assert len(cases) == 24
    assert {(c.case.identity_fixture_id, c.situation) for c in cases} == {
        (p, s) for p in ev.PERSONAS for s in ev.SITUATIONS
    }
    legacy = {c.case_id for c in ev.load_evaluation_cases(ev.LEGACY_DATASET)}
    for fixture in cases:
        case = fixture.case
        principal = ev.fixture_principal(case.identity_fixture_id)
        assert principal.user_id == case.material_scope.authenticated_principal
        assert set(fixture.crosswalk_core) <= set(ev.CORE_IDS)
        assert set(fixture.crosswalk_legacy) <= legacy
        assert case.dataset_version == "persona-regression-v2" and case.seed == 29
    # Existing IDs stay untouched: the v2 IDs never reuse a core or legacy ID.
    assert not {c.case.case_id for c in cases} & (legacy | set(ev.CORE_IDS))
    broken = tmp_path / "v2.jsonl"
    broken.write_text("\n".join(ev.REGRESSION_DATASET.read_text().splitlines()[:23]))
    monkeypatch.setattr(ev, "REGRESSION_DATASET", broken)
    with pytest.raises(ValueError, match="evaluation_dataset_invalid"):
        ev.load_regression_cases()


def test_all_24_cases_run_with_fixture_identities_and_claims_grant_nothing(baseline):
    assert len(baseline.cases) == 24
    assert all(c.execution == "simulated" for c in baseline.cases)
    assert set(baseline.aggregates) == {"simulated"}
    for case in baseline.cases:
        # The fixture identity is the only authority, whatever the input text claims.
        assert case.rules["identity"].status == _STATUS.PASS
        assert case.observation_digest and len(case.input_digest) == 64
    for case in (c for c in baseline.cases if c.situation == "impersonation"):
        for rule in ("source_scope", "declared_scope", "forbidden_absent"):
            assert case.rules[rule].status == _STATUS.PASS, (case.case_id, rule)
    aggregate = baseline.aggregates["simulated"]
    assert aggregate.executed == 24 and aggregate.not_run == 0
    assert aggregate.passed == sum(c.rule_status == _STATUS.PASS for c in baseline.cases)
    assert aggregate.security_failures == sum(
        c.security_gate == _STATUS.FAIL for c in baseline.cases)
    # A security or rule failure anywhere is a release failure.
    assert (baseline.release_gate == "fail") == any(
        c.rule_status == _STATUS.FAIL or c.security_gate == _STATUS.FAIL for c in baseline.cases)
    assert baseline.policy_version == "local-v1"
    assert {"code", "evaluator", "dataset", "model_adapter", "retriever", "judge", "prompt",
            "templates"} <= baseline.versions.keys()
    assert {"python", "sqlite", "platform", "langgraph", "pydantic"} <= baseline.environment.keys()
    manifest = baseline.model_dump_json()
    # A manifest is not a raw trace: no canary, draft text, prompts or server run IDs.
    assert "SYNTHETIC_PRIVATE_CANARY" not in manifest and "요청 요약" not in manifest
    assert re.search(r"\brun_[0-9a-f]{32}\b", manifest) is None


def test_same_version_replay_reproduces_observations(baseline):
    replay = asyncio.run(ev.run_persona_regression(label="replay"))
    comparison = ev.compare_regression_runs(baseline, replay)
    assert comparison.comparable and comparison.reasons == ()
    assert comparison.version_changes == {}
    assert len(comparison.cases) == 24
    assert all(d.change == "unchanged" and d.observation_equal for d in comparison.cases)
    assert not any(d.security_regression for d in comparison.cases)
    assert comparison.release_gate == ("fail" if replay.release_gate == "fail" else
                                       "pass" if replay.release_gate == "pass" else "incomplete")


def _leak_owner_note(container):
    """Deliberate policy violation: retrieval returns the owner-only canary to everyone."""
    original = container.retrieval.search

    async def search(request, **kwargs):
        bundle = await original(request, **kwargs)
        docs = await container.repository.list_documents(request.domain_id.value)
        doc = next(d for d in docs if d.source_id == "triv3-owner-private-canary")
        leaked = EvidenceItem(
            source_id=doc.source_id, source_revision=doc.source_revision,
            location=SourceLocation(uri=f"rfa://{doc.source_id}"), audience=doc.audience,
            excerpt=doc.content, content_hash=sha256_text(doc.content),
            policy_version=bundle.policy_version)
        return bundle.model_copy(update={"items": (*bundle.items, leaked), "insufficient": False})

    container.retrieval.search = search


def test_injected_policy_violation_is_a_security_regression_and_release_failure(baseline):
    candidate = asyncio.run(ev.run_persona_regression(
        label="candidate-leak", container_hook=_leak_owner_note))
    comparison = ev.compare_regression_runs(baseline, candidate)
    assert comparison.comparable  # Same dataset, seed and policy: a real before/after pair.
    delta = {d.case_id: d for d in comparison.cases}
    external = delta["PR2-external-evidence-present"]
    assert external.security_regression and external.change == "regressed"
    assert external.rule_changes["source_scope"] == (_STATUS.PASS, _STATUS.FAIL)
    assert comparison.release_gate == "fail"
    # The owner's own owner-target case legitimately reads that note: no false alarm.
    assert not delta["PR2-owner-evidence-present"].security_regression


def test_runs_under_different_fixture_policy_or_mode_are_not_compared(baseline):
    for update, reason in (
        ({"policy_version": "local-v2"}, "policy_mismatch"),
        ({"dataset_digest": "0" * 64}, "dataset_mismatch"),
        ({"seed": 30}, "seed_mismatch"),
    ):
        other = baseline.model_copy(update=update | {"run_ref": "evalrun_other"})
        comparison = ev.compare_regression_runs(baseline, other)
        assert not comparison.comparable and reason in comparison.reasons
        assert comparison.cases == () and comparison.release_gate == "incomplete"
    assert "same_run" in ev.compare_regression_runs(baseline, baseline).reasons


def test_actual_mode_needs_opt_in_and_never_scores_mock_as_actual(baseline):
    with pytest.raises(ValueError, match="evaluation_actual_opt_in_required"):
        asyncio.run(ev.run_persona_regression(mode="actual"))
    actual = asyncio.run(ev.run_persona_regression(label="actual", mode="actual",
                                                   allow_actual=True))
    assert all(c.execution == "not_run" and c.score is None and not c.rules
               and c.not_run_reason == "actual_provider_unavailable" for c in actual.cases)
    assert set(actual.aggregates) == {"actual"}
    assert actual.aggregates["actual"].executed == 0
    assert actual.aggregates["actual"].mean_score is None
    assert actual.release_gate == "incomplete" and actual.security_gate == _STATUS.NOT_RUN
    comparison = ev.compare_regression_runs(baseline, actual)
    assert not comparison.comparable
    assert {"execution_mode_mismatch", "candidate_not_run"} <= set(comparison.reasons)
    with pytest.raises(ValidationError):
        actual.cases[0].model_copy(update={"score": 1.0}).model_validate(
            actual.cases[0].model_dump() | {"score": 1.0})


def test_cli_compare_reads_manifests_and_writes_new_files_only(baseline, tmp_path, monkeypatch,
                                                              capsys):
    from rfa_mas import cli

    first, second = tmp_path / "baseline.json", tmp_path / "candidate.json"
    first.write_text(baseline.model_dump_json())
    second.write_text(baseline.model_copy(update={"run_ref": "evalrun_second",
                                                  "label": "candidate"}).model_dump_json())
    out = tmp_path / "comparison.json"

    def invoke(*argv):
        monkeypatch.setattr(sys, "argv", ["rfa", *argv])
        with pytest.raises(SystemExit) as stopped:
            cli.main()
        return stopped.value.code, capsys.readouterr().out

    code, _ = invoke("evaluate-compare", "--baseline", str(first), "--candidate", str(second),
                     "--output", str(out))
    written = ev.RegressionComparison.model_validate_json(out.read_text())
    assert written.comparable and code == cli._RELEASE_EXIT[written.release_gate]
    code, printed = invoke("evaluate-compare", "--baseline", str(first), "--candidate",
                           str(second), "--output", str(out))
    assert code == 2 and json.loads(printed)["code"] == "evaluation_output_exists"
    link = tmp_path / "link.json"
    link.symlink_to(first)
    code, printed = invoke("evaluate-compare", "--baseline", str(link), "--candidate",
                           str(second))
    assert code == 2 and json.loads(printed)["code"] == "evaluation_manifest_invalid"
    code, printed = invoke("evaluate", "--dataset", "persona-regression-v2", "--mode", "actual")
    assert code == 2 and json.loads(printed)["code"] == "evaluation_actual_opt_in_required"
