from __future__ import annotations

import copy
import json
from datetime import datetime

import pytest
from pydantic import ValidationError

from rfa_mas import contracts as c
from scripts.contract_baseline import (
    BASELINE,
    EXTENDED,
    TraceEvalCase,
    build_baseline,
    build_extended,
    export_extended_fixtures,
    load_extended_cases,
)


def case(category="normal"):
    return next(item for item in load_extended_cases() if item.category == category)


def test_generated_schemas_fixtures_and_legacy_compatibility():
    assert build_baseline() == json.loads(BASELINE.read_text())
    assert build_extended() == json.loads(EXTENDED.read_text())
    assert [
        item.model_dump(mode="json") for item in load_extended_cases()
    ] == export_extended_fixtures()["cases"]
    extended = build_extended()
    assert extended["ports"]["RuntimePort"].keys() >= {
        "run",
        "status",
        "cancel",
        "prepare",
        "cleanup",
    }
    assert "load_context" in extended["ports"]["RetrievalPort"]
    assert "emit_event" in extended["ports"]["TracePort"]
    # P0-015 implements the formerly planned routes. Preserve the frozen 1.0
    # projection above and check the actual additive response contracts here.
    paths = extended["implemented_http_routes"]["core"]["paths"]
    for path, method, code, model in (
        ("/v1/sessions", "post", "201", "SessionRecord"),
        ("/v1/sessions/{session_id}", "get", "200", "SessionDetail"),
        ("/v1/runs/{run_id}", "get", "200", "RunRecord"),
    ):
        response = paths[path][method]["responses"][code]["content"]["application/json"]
        assert response["schema"]["$ref"] == f"#/components/schemas/{model}"


@pytest.mark.parametrize(
    "category",
    [
        "normal",
        "policy_denied",
        "approval_missing",
        "draft_changed",
        "acl_changed",
        "duplicate",
        "outcome_unknown",
    ],
)
def test_seven_synthetic_shapes_roundtrip_without_claiming_execution(category):
    fixture = case(category)
    assert TraceEvalCase.model_validate_json(fixture.model_dump_json()) == fixture
    assert fixture.evaluation.rule_status == "not_run"
    assert fixture.evaluation.judge_status == "not_run"
    assert fixture.trace.input_tokens is fixture.trace.output_tokens is None
    assert fixture.trace.mode == "mock"
    if category == "policy_denied":
        assert fixture.draft is fixture.approval is fixture.receipt is None
    if category == "duplicate":
        assert fixture.receipt == case("normal").receipt
    if category == "outcome_unknown":
        assert fixture.receipt.next_action == "query"


@pytest.mark.parametrize("field", ["request_id", "trace_id", "run_id", "agent_id", "domain_id"])
def test_broken_policy_join_rejected(field):
    data = case().model_dump(mode="json")
    data["policy"][field] = "quantization_research" if field == "domain_id" else "different-id"
    with pytest.raises(ValidationError):
        TraceEvalCase.model_validate(data)


def test_optional_run_links_not_invented_and_trace_is_not_identity():
    context = c.ExecutionContext(
        request_id="req", run_id="run", trace_id="trace", agent_id="assistant-supervisor"
    )
    assert context.session_id is context.task_id is context.team_id is None
    with pytest.raises(ValidationError, match="team requires task"):
        c.ExecutionContext(**context.model_dump(exclude={"team_id"}), team_id="team")
    with pytest.raises(ValidationError):
        c.TrustedPrincipal(trace_id="trace", user_id="owner", authenticated=True)


@pytest.mark.parametrize(
    "field", ["raw_prompt", "document", "tool_arguments", "metadata", "email", "authorization"]
)
def test_trace_raw_canary_fields_rejected(field):
    payload = case().trace.model_dump(mode="json")
    payload[field] = "CANARY_1ON1_Q7"
    with pytest.raises(ValidationError):
        c.TraceEvent.model_validate(payload)


def test_trace_unissued_stage_ids_and_free_text_status_rejected():
    payload = case("policy_denied").trace.model_dump(mode="json")
    with pytest.raises(ValidationError, match="draft reference"):
        c.TraceEvent.model_validate({**payload, "approval_id": "approval-not-issued"})
    with pytest.raises(ValidationError):
        c.TraceEvent.model_validate({**payload, "reason_code": "CANARY_GPU_R9"})
    with pytest.raises(ValidationError):
        c.TraceEvent.model_validate(
            {**payload, "execution": {**payload["execution"], "agent_id": "private@example.com"}}
        )
    # Pattern validation is NOT anonymization; trusted adapters mint/map IDs.


def test_policy_review_is_not_allow_and_model_provenance_is_not_trusted():
    data = case().policy.model_dump(mode="json")
    with pytest.raises(ValidationError, match="review/deny"):
        c.PolicyDecisionV11.model_validate({**data, "decision": "review"})
    with pytest.raises(ValidationError):
        c.PolicyDecisionV11.model_validate({**data, "llm_says_allow": True})
    with pytest.raises(ValidationError, match="validity"):
        c.PolicyDecisionV11.model_validate({**data, "expires_at": data["issued_at"]})


@pytest.mark.parametrize("change", ["body", "attachment", "target", "policy", "acl", "revision"])
def test_exact_approved_payload_invalidated_on_every_change(change):
    fixture = case()
    data = fixture.draft.model_dump(mode="python")
    if change == "body":
        data["content"] += " "
        data["content_hash"] = c.sha256_text(data["content"])
    elif change == "attachment":
        data["attachments"] = [c.AttachmentRef(attachment_id="a1", content_hash="0" * 64)]
    elif change == "target":
        data["target"]["destination"] = "other-public-channel"
    elif change == "policy":
        data["policy_version"] = "policy-v2"
    elif change == "acl":
        data["sources"][0]["acl_revision"] = "acl2"
    else:
        data["sources"][0]["source_revision"] = "r2"
        data["allowed_evidence"][0]["source_revision"] = "r2"
    with pytest.raises(ValidationError, match="payload_hash"):
        c.DraftBundleV11.model_validate(data)
    # Compute the new binding using typed nested objects; old approval must fail.
    draft = fixture.draft.model_copy(deep=True)
    for name in ("content", "content_hash", "policy_version"):
        object.__setattr__(draft, name, data[name])
    object.__setattr__(
        draft, "attachments", tuple(c.AttachmentRef.model_validate(x) for x in data["attachments"])
    )
    object.__setattr__(draft, "target", c.DraftTarget.model_validate(data["target"]))
    object.__setattr__(
        draft, "sources", tuple(c.SourceRevisionRef.model_validate(x) for x in data["sources"])
    )
    object.__setattr__(
        draft,
        "allowed_evidence",
        tuple(c.EvidenceRef.model_validate(x) for x in data["allowed_evidence"]),
    )
    object.__setattr__(draft, "payload_hash", draft.calculated_payload_hash())
    draft = c.DraftBundleV11.model_validate_json(draft.model_dump_json())
    assert not fixture.approval.matches(draft, fixture.trace.timestamp)


def test_approval_expiry_and_unknown_publication():
    fixture = case()
    assert fixture.approval.matches(fixture.draft, fixture.trace.timestamp)
    assert not fixture.approval.matches(fixture.draft, fixture.approval.expires_at)
    with pytest.raises(ValueError):
        fixture.approval.matches(fixture.draft, datetime(2026, 9, 26))
    data = case("outcome_unknown").receipt.model_dump(mode="json")
    with pytest.raises(ValidationError, match="query"):
        c.PublicationReceipt.model_validate({**data, "next_action": "none"})
    with pytest.raises(ValidationError, match="mock"):
        c.ApprovalReference.model_validate({**fixture.approval.model_dump(), "mode": "real"})


def test_context_selective_read_and_multi_parent_restrictions():
    fixture = case()
    request = c.ContextRequest(
        request_id="req",
        trace_id="trace",
        run_id="run",
        agent_id="scout",
        domain_id="triv3",
        query="FAQ",
        allowed_audiences=(c.Audience.PUBLIC,),
        principal=c.TrustedPrincipal(user_id="user", authenticated=True),
        goal="answer public question",
        role="scout",
        target=c.DraftTarget(audience="public"),
        endpoint_id="local-model",
    )
    assert request.level == "L0"
    with pytest.raises(ValidationError, match="explicit sources"):
        c.ContextRequest.model_validate({**request.model_dump(), "level": "L2"})
    source = fixture.draft.sources[0]
    parent2 = source.model_copy(update={"source_id": "parent-2"})
    item = c.ContextItem(
        **{**fixture.draft.allowed_evidence[0].model_dump(), "schema_version": "1.1"},
        excerpt="public fact",
        policy_version="policy-v1",
        level="L1",
        parents=(source, parent2),
        policies=c.PolicyBindings(read_decision_id="read-both", share_decision_id="share-both"),
    )
    assert len(item.parents) == 2
    bundle = c.ContextBundle(
        request_id="req",
        trace_id="trace",
        run_id="run",
        agent_id="scout",
        domain_id="triv3",
        policy_version="policy-v1",
        simulated=True,
        adapter="mock",
        items=(item,),
        loaded_characters=11,
    )
    assert bundle.measured_tokens is None
    with pytest.raises(ValidationError, match="measured"):
        c.ContextBundle.model_validate({**bundle.model_dump(), "loaded_characters": 0})


def test_experiment_not_run_is_not_a_measurement():
    assert c.ExperimentEvidence(status="not_run").metrics == {}
    with pytest.raises(ValidationError):
        c.ExperimentEvidence(status="not_run", metrics={"latency": 10}, units={"latency": "ms"})
    with pytest.raises(ValidationError, match="evidence"):
        c.ExperimentEvidence(status="measured", metrics={"latency": 10}, units={"latency": "ms"})


def test_evaluator_error_disabled_and_rule_failure_are_independent():
    baseline = case().evaluation.model_dump(mode="json")
    data = {
        **baseline,
        "rule_checks": {"no_unauthorized_write": False},
        "rule_status": "fail",
        "evidence_refs": ["sink/record-1"],
    }
    assert c.EvalResultV11.model_validate(data).rule_status == "fail"
    for state in ("error", "unknown", "not_run"):
        result = c.EvalResultV11.model_validate({**data, "judge_status": state})
        assert result.judge_score is None and result.rule_status == "fail"
    with pytest.raises(ValidationError, match="all observed"):
        c.EvalResultV11.model_validate({**data, "rule_status": "pass"})
    with pytest.raises(ValidationError, match="executed assessment"):
        c.EvalResultV11.model_validate({**data, "judge_status": "pass"})
    high_judge = {
        **data,
        "judge_kind": "mock",
        "judge_status": "pass",
        "judge_score": 1,
        "judge_reason": "synthetic good wording",
        "judge_dimensions": c.JudgeDimensions(
            evidence_faithfulness=1, question_resolution=1, task_candidate_usefulness=1
        ).model_dump(),
        "judge_adapter": "mock",
        "executed": True,
        "simulated": True,
    }
    assert c.EvalResultV11.model_validate(high_judge).rule_status == "fail"


def test_wrong_receipt_reference_rejected():
    payload = copy.deepcopy(case().model_dump(mode="json"))
    payload["receipt"]["approval_id"] = "forged-approval"
    with pytest.raises(ValidationError, match="binding mismatch"):
        TraceEvalCase.model_validate(payload)


@pytest.mark.parametrize(
    "section,field,value",
    [
        ("trace", "policy_decision_id", "different-policy"),
        ("draft", "request_id", "other-request"),
        ("draft", "trace_id", "other-trace"),
        ("evaluation", "trace_id", "other-trace"),
        ("evaluation", "case_id", "other-case"),
        ("approval", "decision", "rejected"),
        ("trace", "mode", "real"),
        ("receipt", "mode", "real"),
    ],
)
def test_review_discovered_cross_fixture_contradictions_rejected(section, field, value):
    payload = case().model_dump(mode="json")
    payload[section][field] = value
    with pytest.raises(ValidationError):
        TraceEvalCase.model_validate(payload)


def test_unknown_case_cannot_hide_a_successful_receipt():
    payload = case("outcome_unknown").model_dump(mode="json")
    payload["receipt"].update(status="succeeded", external_result_ref="sink/1")
    with pytest.raises(ValidationError, match="uncertain receipt"):
        TraceEvalCase.model_validate(payload)


@pytest.mark.parametrize("method", ["assignment", "copy"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("content", "MUTATED_SYNTHETIC_BODY"),
        ("attachments", (c.AttachmentRef(attachment_id="new-attachment", content_hash="0" * 64),)),
    ],
)
def test_mutation_cannot_reuse_old_approval_even_with_stale_hashes(method, field, value):
    fixture = case()
    if method == "assignment":
        draft = fixture.draft.model_copy(deep=True)
        setattr(draft, field, value)
    else:
        draft = fixture.draft.model_copy(update={field: value})
    assert not fixture.approval.matches(draft, fixture.trace.timestamp)


@pytest.mark.parametrize("nonfinite", [float("nan"), float("inf"), float("-inf")])
def test_trace_and_experiment_must_report_finite_measurements(nonfinite):
    with pytest.raises(ValidationError):
        c.TraceEvent.model_validate({**case().trace.model_dump(), "duration_ms": nonfinite})
    with pytest.raises(ValidationError):
        c.ExperimentEvidence(
            status="measured",
            metrics={"latency": nonfinite},
            units={"latency": "ms"},
            evidence_ref="measure-1",
            conditions_ref="cpu-1",
        )


def test_all_extended_schema_references_resolve():
    schema = build_extended()["json_schema"]

    def walk(value):
        if isinstance(value, dict):
            if "$ref" in value:
                target = schema
                for segment in value["$ref"][2:].split("/"):
                    target = target[segment]
                assert isinstance(target, dict)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(schema)
