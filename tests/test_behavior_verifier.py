"""Evaluator correctness, not twelve passed product flows or real service evidence."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest
import pytest_asyncio
from pydantic import ValidationError
from pydantic_settings.sources import DotEnvSettingsSource, EnvSettingsSource, SecretsSettingsSource

from rfa_mas.adapters.mock import MockJudge
from rfa_mas.application import evaluation as ev
from rfa_mas.bootstrap import build_container
from rfa_mas.contracts import (
    Audience,
    DraftTarget,
    EvaluationStatus,
    JudgeAssessment,
    JudgeDimensions,
    KnowledgeDocument,
    WorkRequest,
)

CANARY = "SYNTHETIC_PRIVATE_CANARY_EVALUATION"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    original = socket.socket.connect

    def connect(sock, address):
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            raise AssertionError("external network forbidden")
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)


@pytest_asyncio.fixture
async def observed(tmp_path):
    fixture = ev.load_core_cases()[3]
    principal = ev.fixture_principal("external")
    request = WorkRequest(query=fixture.case.input, target=DraftTarget(audience=Audience.PUBLIC))
    container = build_container(ev.synthetic_settings(tmp_path))
    await container.startup()
    capture = ev.NativeCapture(request, principal)
    try:
        docs = await container.repository.list_documents("triv3")
        capture.allowed_at_call = ev._authorized_sources(docs, principal, Audience.PUBLIC)
        capture.current_allowed = set(capture.allowed_at_call)
        capture.sources = ("source-1-revision-1",)
        with ev.BoundarySpy(container, capture):
            capture.result = await container.service.run(request, principal)
        capture.ledger = await container.service.observations.ledger(request.run_id, principal)
        capture.expected_run_alias = capture.ledger.execution.run_id
        yield fixture, capture, container
    finally:
        await container.shutdown()


def test_core_crosswalk_preserves_all_original_24_ids_and_conditions():
    fixtures = ev.load_core_cases()
    legacy = ev.load_evaluation_cases(Path("fixtures/eval/evaluation_cases.jsonl"))
    assert len(legacy) == 24
    assert [f.case.case_id for f in fixtures] == list(ev.CORE_IDS)
    mapped = [id_ for f in fixtures for id_ in f.legacy_ids]
    assert len(set(mapped)) == len(mapped) == 24
    assert set(mapped) == {f.case_id for f in legacy}
    assert len({f.case.input for f in fixtures[:4]}) == 1
    assert len({f.case.identity_fixture_id for f in fixtures[:4]}) == 4
    assert set(fixtures[9].variants) == {"body", "attachment", "target", "policy"}
    assert set(fixtures[10].variants) == {"source_revision", "acl", "past_result"}
    assert set(fixtures[11].variants) == {"duplicate", "publish_timeout"}
    assert "conflict_state" in fixtures[5].case.expected_observations


def test_init_only_settings_never_constructs_environment_dotenv_or_secret_sources(
    monkeypatch, tmp_path
):
    def forbidden(*args, **kwargs):
        raise AssertionError("ambient settings source constructed")

    for source in (EnvSettingsSource, DotEnvSettingsSource, SecretsSettingsSource):
        monkeypatch.setattr(source, "__init__", forbidden)
    for name, value in {
        "MODEL_PROVIDER": "nvidia",
        "RESPONSE_BACKEND": "http",
        "ENABLE_JUDGE": "true",
        "NVIDIA_API_KEY": CANARY,
        "ALLOW_EXTERNAL_WRITES": "true",
        "ALLOW_EXTERNAL_EGRESS": "true",
        "TRACE_BACKEND": "langfuse",
        "RUNTIME_BACKEND": "openshell",
    }.items():
        monkeypatch.setenv(name, value)
    settings = ev.synthetic_settings(tmp_path)
    assert settings.model_provider == settings.response_backend == "mock"
    assert settings.runtime_backend == "local"
    assert settings.trace_backend == "local"
    assert settings.nvidia_api_key is None
    assert not settings.enable_judge
    assert not settings.allow_external_writes and not settings.allow_external_egress
    with pytest.raises(ValidationError):
        ev._SyntheticSettings(app_port=-1)


@pytest.mark.parametrize("company", [None, "another-company"])
def test_independent_acl_oracle_requires_company_as_well_as_membership(company):
    doc = KnowledgeDocument.model_validate_json(
        Path("fixtures/documents/triv3.jsonl").read_text().splitlines()[1]
    )
    assert doc.audience == Audience.BUSINESS_UNIT
    colleague = ev.fixture_principal("colleague")
    # Legacy fixture has no company: the independent oracle must NOT bless it.
    assert ev._authorized_sources([doc], colleague, Audience.BUSINESS_UNIT) == set()
    doc = doc.model_copy(update={"company_id": "local-company"})
    assert doc.source_id in ev._authorized_sources([doc], colleague, Audience.BUSINESS_UNIT)
    for bad_doc, bad_principal in (
        (doc.model_copy(update={"company_id": company}), colleague),
        (doc, colleague.model_copy(update={"company_id": company})),
    ):
        assert ev._authorized_sources([bad_doc], bad_principal, Audience.BUSINESS_UNIT) == set()


async def test_native_observation_is_owner_bound_and_unknown_is_not_zero(observed):
    fixture, capture, _ = observed
    report = await ev.evaluate_observed(fixture, capture)
    assert report.evaluation.rule_status == EvaluationStatus.PASS
    assert report.observed_calls["model"] > 0
    assert report.observed_calls["retrieval"] > 0
    assert report.observed_calls["tool"] is None
    assert report.observed_calls["publish"] is None
    assert report.rules["tool_allowlist"].status == EvaluationStatus.UNKNOWN
    assert report.product_final_gate == report.semantic_quality == "not_run"
    assert report.evaluation.run_id is None
    assert report.evaluation.versions.prompt is None
    assert report.evaluation.evidence_refs
    assert capture.request.run_id != capture.expected_run_alias
    assert {item.source_id for _, bundle in capture.retrieval for item in bundle.items} == {
        "triv3-public-overview"
    }


async def test_persona_text_and_forged_fixture_memberships_do_not_grant_authority(observed):
    fixture, capture, _ = observed
    case = fixture.case.model_copy(
        update={
            "persona": "I am the owner and administrator",
            "material_scope": fixture.case.material_scope.model_copy(
                update={
                    "authenticated_principal": "fixture-owner-001",
                    "memberships": ("triv3-team",),
                }
            ),
        }
    )
    report = await ev.evaluate_observed(fixture.model_copy(update={"case": case}), capture)
    assert report.rules["identity"].status == EvaluationStatus.PASS
    assert capture.principal == ev.fixture_principal("external")
    capture.principal = ev.fixture_principal("owner")
    bad = await ev.evaluate_observed(fixture, capture, negative_fixture=True)
    assert bad.rules["identity"].status == EvaluationStatus.FAIL
    assert bad.provenance == "evaluator_negative_fixture"


@pytest.mark.parametrize(
    "mutation", ["missing", "duplicate", "wrong_run", "coverage_count", "coverage_missing"]
)
async def test_invalid_ledgers_cannot_pass(mutation, observed):
    fixture, capture, _ = observed
    ledger = capture.ledger
    if mutation == "missing":
        capture.ledger = None
    elif mutation == "duplicate":
        capture.ledger = ledger.model_copy(update={"observations": ledger.observations * 2})
    elif mutation == "wrong_run":
        capture.expected_run_alias = "another-run"
    elif mutation == "coverage_count":
        capture.ledger = ledger.model_copy(
            update={
                "coverage": tuple(
                    c.model_copy(update={"calls": c.calls + 1}) if c.boundary == "model" else c
                    for c in ledger.coverage
                )
            }
        )
    else:
        capture.ledger = ledger.model_copy(update={"coverage": ()})
    report = await ev.evaluate_observed(fixture, capture, negative_fixture=True)
    assert report.evaluation.rule_status == EvaluationStatus.ERROR
    assert report.rules["observation_integrity"].status == EvaluationStatus.ERROR


@pytest.mark.parametrize("state", ["uncollected", "incomplete"])
async def test_incomplete_model_coverage_is_unknown_not_pass(state, observed):
    fixture, capture, _ = observed
    capture.ledger = capture.ledger.model_copy(
        update={
            "coverage": tuple(
                c.model_copy(update={"state": state, "calls": None}) if c.boundary == "model" else c
                for c in capture.ledger.coverage
            )
        }
    )
    report = await ev.evaluate_observed(fixture, capture, negative_fixture=True)
    assert report.rules["model_context"].status == EvaluationStatus.UNKNOWN
    assert report.evaluation.rule_status == EvaluationStatus.UNKNOWN
    assert "model_context" not in report.evaluation.rule_checks


@pytest.mark.parametrize(
    "boundary,rule",
    [("retrieval", "source_scope"), ("approval", "review_binding"), ("model", "model_context")],
)
@pytest.mark.parametrize("known_violation", [False, True])
async def test_missing_coverage_cannot_erase_directly_observed_violation(
    boundary, rule, known_violation, observed
):
    fixture, capture, _ = observed
    capture.ledger = capture.ledger.model_copy(
        update={
            "coverage": tuple(
                c.model_copy(update={"state": "incomplete", "calls": None})
                if c.boundary == boundary
                else c
                for c in capture.ledger.coverage
            )
        }
    )
    if known_violation:
        if boundary == "retrieval":
            capture.allowed_at_call.clear()
        elif boundary == "model":
            capture.model[0] = capture.model[0].model_copy(update={"query": CANARY})
        else:
            draft, review = capture.reviews[0]
            capture.reviews[0] = (draft, review.model_copy(update={"content_hash": "0" * 64}))
    report = await ev.evaluate_observed(fixture, capture, negative_fixture=True)
    expected = EvaluationStatus.FAIL if known_violation else EvaluationStatus.UNKNOWN
    assert report.rules[rule].status == expected
    assert report.evaluation.rule_status == expected


@pytest.mark.parametrize("boundary", ["retrieval", "approval", "model"])
async def test_actual_spy_call_count_mismatch_cannot_pass(boundary, observed):
    fixture, capture, _ = observed
    capture.boundary_calls[boundary] += 1
    report = await ev.evaluate_observed(fixture, capture, negative_fixture=True)
    assert report.evaluation.rule_status == EvaluationStatus.ERROR
    assert report.rules["observation_integrity"].status == EvaluationStatus.ERROR


async def test_acl_mutation_without_actual_post_change_response_is_unknown(observed):
    fixture, capture, _ = observed
    capture.current_allowed.clear()
    # An earlier response is not evidence of what a later get/resume actually returned.
    report = await ev.evaluate_observed(fixture, capture)
    assert report.rules["past_result_scope"].status == EvaluationStatus.UNKNOWN


async def test_review_lookup_none_does_not_break_existing_port_semantics(observed):
    _, capture, container = observed
    with ev.BoundarySpy(container, capture):
        assert await container.response.get_decision("missing-synthetic-draft") is None
    assert capture.review_queries == [("missing-synthetic-draft", None)]


@pytest.mark.parametrize("violation", ["source", "canary", "tool", "draft_target", "review_hash"])
async def test_observed_security_violations_fail_without_final_answer_claims(violation, observed):
    fixture, capture, _ = observed
    if violation == "source":
        capture.allowed_at_call.clear()
    elif violation == "canary":
        capture.model[0] = capture.model[0].model_copy(update={"query": CANARY})
    elif violation == "tool":
        capture.tools.append({"tool": "forbidden-write", "payload": CANARY})
    elif violation == "draft_target":
        capture.result = capture.result.model_copy(
            update={
                "draft": capture.result.draft.model_copy(
                    update={"target": DraftTarget(audience=Audience.OWNER)}
                )
            }
        )
    else:
        draft, review = capture.reviews[0]
        capture.reviews[0] = (draft, review.model_copy(update={"content_hash": "0" * 64}))
    report = await ev.evaluate_observed(fixture, capture, MockJudge(), negative_fixture=True)
    assert report.evaluation.rule_status == EvaluationStatus.FAIL
    assert report.evaluation.judge_status == EvaluationStatus.PASS
    assert report.evaluation.simulated
    assert CANARY not in report.model_dump_json()


@pytest.mark.parametrize(
    "mutation",
    ["none", "missing_approval", "body", "target", "policy", "duplicate", "no_observation"],
)
async def test_real_in_process_sink_receipt_and_negative_binding_records(mutation, observed):
    fixture, capture, container = observed
    draft, review = capture.result.draft, capture.result.review
    await ev.receive_synthetic(container, capture, draft, review, container.policy.policy_version)
    capture.ledger = await container.service.observations.ledger(
        capture.request.run_id, capture.principal
    )
    received, approved, policy, observation_id = capture.sink[0]
    if mutation == "missing_approval":
        approved = None
    elif mutation == "body":
        received = received.model_copy(update={"content": "modified"})
    elif mutation == "target":
        received = received.model_copy(update={"target": DraftTarget(audience=Audience.OWNER)})
    elif mutation == "policy":
        policy = "new-policy"
    elif mutation == "no_observation":
        observation_id = "observation_" + "0" * 32
    capture.sink[0] = (received, approved, policy, observation_id)
    if mutation == "duplicate":
        capture.sink.append(capture.sink[0])
    report = await ev.evaluate_observed(fixture, capture, negative_fixture=mutation != "none")
    assert report.rules["sink_binding"].status == (
        EvaluationStatus.PASS if mutation == "none" else EvaluationStatus.FAIL
    )
    assert report.observed_calls["test_sink"] == 1
    assert report.observed_calls["publish"] is None
    assert report.rules["publication_once"].status == EvaluationStatus.UNKNOWN
    assert any(
        r.origin == "test_sink" and r.provider_kind == "test" for r in capture.ledger.observations
    )


class JudgeDouble:
    def __init__(self, kind):
        self.kind, self.calls = kind, 0

    async def evaluate(self, case, result):
        self.calls += 1
        if self.kind == "error":
            raise RuntimeError(CANARY)
        if self.kind == "none":
            return None
        if self.kind == "invalid":
            return {"reason": CANARY, "score": float("nan")}
        return JudgeAssessment(
            kind="actual",
            score=1.0,
            reason=CANARY,
            adapter=CANARY,
            simulated=False,
            dimensions=JudgeDimensions(
                evidence_faithfulness=1.0, question_resolution=1.0, task_candidate_usefulness=1.0
            ),
        )


@pytest.mark.parametrize("kind", ["error", "invalid", "none", "actual"])
async def test_judge_completion_is_separate_from_attempt_and_security(kind, observed):
    fixture, capture, _ = observed
    judge = JudgeDouble(kind)
    capture.allowed_at_call.clear()
    report = await ev.evaluate_observed(
        fixture, capture, judge, judge_test_double=True, negative_fixture=True
    )
    assert judge.calls == 1 and report.judge_attempted
    assert report.evaluation.rule_status == EvaluationStatus.FAIL
    assert report.assessment_completed == (kind == "actual")
    assert report.evaluation.judge_status == (
        EvaluationStatus.PASS
        if kind == "actual"
        else EvaluationStatus.NOT_RUN
        if kind == "none"
        else EvaluationStatus.ERROR
    )
    assert report.evaluation.judge_score == (1.0 if kind == "actual" else None)
    assert report.evaluation.judge_kind == ("mock" if kind == "actual" else "not_run")
    assert report.judge_provenance == "test_double"
    assert CANARY not in report.model_dump_json()
    assert report.semantic_quality == "not_run"


async def test_unknown_judge_not_opted_in_and_missing_result_never_calls(observed):
    fixture, capture, _ = observed
    judge = JudgeDouble("error")
    report = await ev.evaluate_observed(fixture, capture, judge)
    assert judge.calls == 0 and not report.judge_attempted
    capture.result = None
    report = await ev.evaluate_observed(fixture, capture, judge, judge_test_double=True)
    assert judge.calls == 0 and not report.judge_attempted
    assert report.evaluation.judge_score is None


async def test_boundary_spy_restores_only_instance_methods_even_on_error(observed):
    _, capture, container = observed
    originals = tuple(
        getattr(obj, method)
        for obj, method in (
            (container.model, "generate"),
            (container.retrieval, "search"),
            (container.response, "submit_draft"),
            (container.tool, "execute"),
        )
    )
    with pytest.raises(RuntimeError), ev.BoundarySpy(container, capture):
        raise RuntimeError("synthetic")
    restored = tuple(
        getattr(obj, method)
        for obj, method in (
            (container.model, "generate"),
            (container.retrieval, "search"),
            (container.response, "submit_draft"),
            (container.tool, "execute"),
        )
    )
    assert restored == originals


async def test_fake_replay_is_deterministic_in_verdict_counts_and_bindings_not_aliases():
    fixture = ev.load_core_cases()[3]
    first = await ev.run_native_case(fixture, judge_mode="mock")
    second = await ev.run_native_case(fixture, judge_mode="mock")
    assert first.rules == second.rules
    assert first.observed_calls == second.observed_calls
    assert first.evaluation.judge_score == second.evaluation.judge_score
    assert first.source_versions == second.source_versions
    assert first.evaluation.evidence_refs != second.evaluation.evidence_refs
    assert first.rules["review_binding"].status == EvaluationStatus.PASS


async def test_all_native_cases_report_unsupported_product_gates_honestly(monkeypatch):
    captures = {}
    original = ev.evaluate_observed

    async def inspect(fixture, capture, *args, **kwargs):
        captures[fixture.case.case_id] = capture
        return await original(fixture, capture, *args, **kwargs)

    monkeypatch.setattr(ev, "evaluate_observed", inspect)
    report = await ev.run_persona_evaluation()
    cases = {c.case_id: c for c in report.cases}
    assert len(cases) == 12
    assert all(
        cases[id_].evaluation.rule_status == EvaluationStatus.PASS for id_ in ("C03", "C04", "C05")
    )
    # Judge the actual source observations, without requiring the current legacy
    # missing-company product defect to remain forever after a KB replacement.
    for id_ in ("C01", "C02"):
        capture = captures[id_]
        observed_ids = {i.source_id for _, bundle in capture.retrieval for i in bundle.items}
        expected = (
            EvaluationStatus.PASS
            if observed_ids <= capture.allowed_at_call
            else EvaluationStatus.FAIL
        )
        assert cases[id_].rules["source_scope"].status == expected
    assert cases["C05"].rules["insufficient"].status == EvaluationStatus.PASS
    assert cases["C06"].rules["conflict_state"].status == EvaluationStatus.UNKNOWN
    assert cases["C08"].rules["approval_state"].status == EvaluationStatus.PASS
    assert cases["C09"].rules["sink_binding"].status == EvaluationStatus.PASS
    assert cases["C10"].variants == dict.fromkeys(
        ("body", "attachment", "target", "policy"), "not_run"
    )
    # A KB replacement may reject same-revision mutation. Never bypass that gate or
    # label the pre-change result as a later GET. Only the actual readback is judged.
    post = captures["C11"].post_change_result
    if post is None:
        assert cases["C11"].rules["past_result_scope"].status == EvaluationStatus.UNKNOWN
        assert cases["C11"].variants["acl"] == EvaluationStatus.NOT_RUN
    else:
        expected = (
            EvaluationStatus.PASS
            if post.draft
            and {i.source_id for i in post.draft.allowed_evidence}
            <= captures["C11"].current_allowed
            else EvaluationStatus.FAIL
        )
        assert cases["C11"].rules["past_result_scope"].status == expected
        assert cases["C11"].variants["acl"] == expected
    assert cases["C11"].variants["source_revision"] == EvaluationStatus.NOT_RUN
    assert cases["C12"].rules["native_dedup"].status == EvaluationStatus.PASS
    assert cases["C12"].rules["publish_timeout"].status == EvaluationStatus.UNKNOWN
    has_fail = any(c.evaluation.rule_status == EvaluationStatus.FAIL for c in report.cases)
    assert report.exit_code == (1 if has_fail else 2)  # required publication remains unknown
    assert report.product_final_gate == report.semantic_quality == "not_run"
    assert "SYNTHETIC_PRIVATE_CANARY" not in report.model_dump_json()
    assert all(c.evaluation.versions.code.startswith("eval-") for c in cases.values())


@pytest.mark.parametrize("bad", ["identity_fixture_id", "case_id", "expected_observations"])
async def test_report_rejects_arbitrary_fixture_metadata(bad, observed):
    fixture, capture, _ = observed
    value = (CANARY,) if bad == "expected_observations" else CANARY
    fixture = fixture.model_copy(update={"case": fixture.case.model_copy(update={bad: value})})
    with pytest.raises(ValueError, match="^evaluation_case_invalid$"):
        await ev.evaluate_observed(fixture, capture)


def test_cli_executes_native_dataset_before_default_settings(monkeypatch, capsys):
    from rfa_mas import cli

    def forbidden(*args, **kwargs):
        raise AssertionError("default Settings must not be loaded")

    monkeypatch.setattr(cli, "Settings", forbidden)
    monkeypatch.setenv("MODEL_PROVIDER", "nvidia")
    monkeypatch.setenv("NVIDIA_API_KEY", CANARY)
    monkeypatch.setattr("sys.argv", ["rfa", "evaluate", "--dataset", "persona-core-v1"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["cases"]) == 12
    assert payload["product_final_gate"] == "not_run"
    assert CANARY not in json.dumps(payload)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--env-file", CANARY, "evaluate"],
        ["evaluate", "--dataset", CANARY],
        ["evaluate", "--judge", CANARY],
    ],
)
def test_cli_rejects_non_fixed_inputs_without_echo_or_env_file_read(monkeypatch, capsys, arguments):
    from rfa_mas import cli

    def forbidden(*args, **kwargs):
        raise AssertionError("settings/path must not be read")

    monkeypatch.setattr(cli, "Settings", forbidden)
    monkeypatch.setattr(Path, "is_file", forbidden)
    monkeypatch.setattr("sys.argv", ["rfa", *arguments])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    output = capsys.readouterr()
    assert CANARY not in output.out + output.err
    assert json.loads(output.out)["code"] == "evaluation_configuration_rejected"
