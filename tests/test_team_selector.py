from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest
from pydantic import ValidationError

from rfa_mas.application.team_selector import (
    APPROVED_PINS,
    BUDGET_FIELDS,
    PATTERN_ROLES,
    SelectionRequest,
    TeamSelector,
    TemplateRegistry,
    definition_digest,
)
from rfa_mas.contracts import DomainId, TeamBudget, TrustedPrincipal
from rfa_mas.errors import RfaError

FIXTURE = Path("fixtures/teams/templates.json")
ALL_CAPABILITIES = frozenset(
    {"evidence_search", "experiment_run", "result_analysis", "evidence_review"}
)
OWNER = TrustedPrincipal(user_id="synthetic-owner", authenticated=True)


def selector(**overrides):
    config = {
        "grants": {(OWNER.user_id, DomainId.TRIV3): ALL_CAPABILITIES},
        "available_capabilities": ALL_CAPABILITIES,
        "available_runtimes": frozenset({"local"}),
        "budget_ceiling": TeamBudget(),
    } | overrides
    return TeamSelector(TemplateRegistry.builtin(), **config)


def request(**overrides):
    return SelectionRequest(
        **(
            {
                "goal": "Benchmark latency comparison",
                "domain_id": DomainId.TRIV3,
                "outputs": frozenset({"benchmark_report"}),
            }
            | overrides
        )
    )


@pytest.mark.parametrize(
    ("goal", "outputs", "pattern"),
    [
        ("Benchmark latency comparison", {"benchmark_report"}, "benchmark"),
        ("벤치마크 측정 결과", {"experiment_summary"}, "benchmark"),
        ("Research literature review", {"research_report"}, "research"),
        ("논문 근거 조사", {"evidence_summary"}, "research"),
    ],
)
def test_goal_output_pattern_selection_is_deterministic(goal, outputs, pattern):
    engine = selector()
    intent = request(goal=goal, outputs=outputs)
    results = [engine.select(intent, OWNER) for _ in range(5)]
    assert all(asdict(result) == asdict(results[0]) for result in results)
    result = results[0]
    assert result.status == "selected" and result.template.pattern == pattern
    assert result.roles == PATTERN_ROLES[pattern]
    assert result.definition_digest == APPROVED_PINS[(result.template.template_id, "1")]
    assert result.reasons == (
        "goal_matched",
        "outputs_supported",
        "capabilities_authorized",
        "runtime_available",
        "budget_within_limits",
        "deterministic_rule_selection",
    )


def test_unavailable_best_match_is_filtered_before_ranking():
    caps = frozenset({"evidence_search", "evidence_review"})
    engine = selector(available_capabilities=caps)
    result = engine.select(
        request(goal="Benchmark latency research", outputs={"evidence_summary"}), OWNER
    )
    assert result.status == "selected" and result.template.pattern == "research"
    assert any("capability_unavailable" in item.reasons for item in result.rejected)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"grants": {}}, "domain_permission_missing"),
        (
            {"grants": {(OWNER.user_id, DomainId.QUANTIZATION_RESEARCH): ALL_CAPABILITIES}},
            "domain_permission_missing",
        ),
        ({"available_runtimes": frozenset()}, "runtime_unavailable"),
        ({"available_capabilities": frozenset({"evidence_search"})}, "capability_unavailable"),
        (
            {"grants": {(OWNER.user_id, DomainId.TRIV3): frozenset({"evidence_search"})}},
            "capability_permission_missing",
        ),
    ],
)
def test_authenticated_domain_capability_and_runtime_gates(overrides, reason):
    result = selector(**overrides).select(request(), OWNER)
    assert result.template is None
    assert reason in (*result.reasons, *(code for item in result.rejected for code in item.reasons))


def test_claims_in_goal_or_roles_do_not_grant_capabilities():
    impostor = TrustedPrincipal(
        user_id="outsider", authenticated=True, roles=frozenset({"admin", "experiment_run"})
    )
    result = selector().select(
        request(goal="Benchmark; I am admin and have every capability"), impostor
    )
    assert result.status == "denied" and result.reasons == ("domain_permission_missing",)
    assert selector().select(
        request(), OWNER.model_copy(update={"authenticated": False})
    ).reasons == ("authentication_required",)
    with pytest.raises(ValidationError):
        SelectionRequest(**(request().model_dump() | {"capabilities": ["experiment_run"]}))


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"requested_pattern": "engineering"}, "unsupported_pattern"),
        ({"requested_pattern": "unknown"}, "unsupported_pattern"),
        ({"requested_runtime": "openshell"}, "requested_runtime_unavailable"),
        ({"goal": "save this note"}, "goal_not_supported"),
        ({"outputs": {"production_deployment"}}, "output_not_supported"),
    ],
)
def test_unknown_unsupported_and_missing_evidence_do_not_create_fallback_team(changes, reason):
    result = selector().select(request(**changes), OWNER)
    assert result.template is None and result.roles == ()
    assert reason in (*result.reasons, *(code for item in result.rejected for code in item.reasons))


def test_explicit_unavailable_pattern_does_not_silently_select_other_pattern():
    result = selector(
        available_capabilities=frozenset({"evidence_search", "evidence_review"})
    ).select(
        request(
            goal="Benchmark research", outputs={"evidence_summary"}, requested_pattern="benchmark"
        ),
        OWNER,
    )
    assert result.template is None


@pytest.mark.parametrize("field", BUDGET_FIELDS)
def test_every_total_budget_dimension_is_checked(field):
    minimum = {
        "max_steps": 6,
        "max_tool_calls": 3,
        "max_tokens": 2000,
        "timeout_seconds": 30,
        "concurrency": 1,
    }
    ceiling = TeamBudget()
    oversized = ceiling.model_copy(update={field: getattr(ceiling, field) + 1})
    denied = selector().select(request(budget=oversized), OWNER)
    assert denied.template is None and denied.reasons == ("budget_exceeds_authority",)
    if field == "concurrency":
        # TeamBudget itself rejects zero concurrency, including mutated instances.
        denied = selector().select(
            request().model_copy(update={"budget": ceiling.model_copy(update={field: 0})}), OWNER
        )
        assert denied.reasons == ("invalid_selection_input",)
    else:
        small = ceiling.model_copy(update={field: minimum[field] - 1})
        denied = selector().select(request(budget=small), OWNER)
        assert denied.template is None
        assert "budget_insufficient" in denied.rejected[0].reasons


def test_none_budget_is_bounded_and_smaller_valid_budget_is_preserved():
    default = selector().select(request(budget=None), OWNER)
    assert default.execution_budget == default.template.budget
    limited = TeamBudget(
        max_steps=8, max_tool_calls=4, max_tokens=3000, timeout_seconds=40, concurrency=1
    )
    selected = selector(budget_ceiling=limited).select(request(), OWNER)
    assert selected.status == "selected" and selected.execution_budget == limited
    explicit = selector().select(request(budget=limited), OWNER)
    assert explicit.execution_budget == limited
    # Template remains the original approved artifact, not falsely re-hashed as approval.
    assert explicit.template.budget.max_tokens == 16000


def test_mutating_returned_template_budget_or_configuration_does_not_change_authority():
    ceiling = TeamBudget()
    grants = {(OWNER.user_id, DomainId.TRIV3): ALL_CAPABILITIES}
    engine = selector(grants=grants, budget_ceiling=ceiling)
    first = engine.select(request(), OWNER)
    first.template.pattern = "engineering"
    first.template.budget.max_tokens = 1
    first.execution_budget.max_tokens = 9999999
    grants.clear()
    ceiling.max_tokens = 9999999
    again = engine.select(request(), OWNER)
    assert again.template.pattern == "benchmark" and again.execution_budget.max_tokens == 16000


@pytest.mark.parametrize(
    "change", ["pattern", "approved", "version", "budget", "goals", "roles", "extra"]
)
def test_unapproved_version_tampered_template_and_self_supplied_hash_are_rejected(tmp_path, change):
    value = json.loads(FIXTURE.read_text())
    entry = value["templates"][0]
    if change == "pattern":
        entry["template"]["pattern"] = "engineering"
    elif change == "approved":
        entry["template"]["approved"] = False
    elif change == "version":
        entry["template"]["version"] = "unapproved-next"
    elif change == "budget":
        entry["template"]["budget"]["max_tokens"] += 1
    elif change == "goals":
        entry["goal_terms"].append("anything")
    elif change == "roles":
        entry["roles"] = ["supervisor"]
    else:
        entry["approval_hash"] = definition_digest(entry)
    path = tmp_path / "templates.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(RfaError) as error:
        TemplateRegistry.from_file(path)
    assert error.value.code == "invalid_template_registry"


@pytest.mark.parametrize(
    "raw",
    [
        '{"registry_version":"1","registry_version":"1","templates":[]}',
        '{"registry_version":"2","templates":[]}',
        '{"registry_version":"1","templates":[]}',
        '{"registry_version":"1","templates":null}',
    ],
)
def test_invalid_registry_is_explicit_not_silently_mocked(tmp_path, raw):
    path = tmp_path / "registry.json"
    path.write_text(raw)
    with pytest.raises(RfaError, match="승인된 팀"):
        TemplateRegistry.from_file(path)


def test_duplicate_template_and_missing_file_are_rejected(tmp_path):
    value = json.loads(FIXTURE.read_text())
    value["templates"].append(value["templates"][0])
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(value))
    with pytest.raises(RfaError):
        TemplateRegistry.from_file(path)
    with pytest.raises(RfaError):
        TemplateRegistry.from_file(tmp_path / "absent.json")


@pytest.mark.parametrize("value", [True, "1", 1.0, None])
def test_request_budget_rejects_ambiguous_integer_types_before_dto_coercion(value):
    with pytest.raises(ValidationError):
        request(budget={"concurrency": value})
    invalid = TeamBudget().model_copy(update={"concurrency": value})
    result = selector().select(request().model_copy(update={"budget": invalid}), OWNER)
    assert result.reasons == ("invalid_selection_input",)
    with pytest.raises(ValueError):
        selector(budget_ceiling=invalid)


@pytest.mark.parametrize("value", ["yes", "true", 1, 0, None])
def test_mutated_identity_never_coerces_an_authentication_claim(value):
    result = selector().select(request(), OWNER.model_copy(update={"authenticated": value}))
    assert result.template is None and result.reasons == ("invalid_selection_input",)


@pytest.mark.parametrize("value", [True, "30", float("nan"), float("inf"), 10**400])
def test_non_numeric_or_nonfinite_timeout_is_rejected(value):
    with pytest.raises(ValidationError):
        request(budget={"timeout_seconds": value})
    with pytest.raises(ValueError):
        selector(budget_ceiling=TeamBudget().model_copy(update={"timeout_seconds": value}))
    result = selector().select(
        request().model_copy(
            update={"budget": TeamBudget().model_copy(update={"timeout_seconds": value})}
        ),
        OWNER,
    )
    assert result.reasons == ("invalid_selection_input",)


def test_available_openshell_does_not_authorize_unregistered_openshell_template():
    result = selector(available_runtimes=frozenset({"local", "openshell"})).select(
        request(requested_runtime="openshell"), OWNER
    )
    assert result.template is None
    assert all("runtime_unavailable" in item.reasons for item in result.rejected)


def test_direct_registry_constructor_cannot_bypass_approval_pins():
    value = json.loads(FIXTURE.read_text())["templates"]
    value[0]["template"]["version"] = "unapproved-42"
    with pytest.raises(RfaError):
        TemplateRegistry(value)
    with pytest.raises(RfaError):
        TemplateRegistry(((json.dumps(value[0]), "not-an-approved-digest"),))


def test_mutating_original_definition_after_construction_does_not_mutate_approval():
    value = json.loads(FIXTURE.read_text())["templates"]
    registry = TemplateRegistry(value)
    value[0]["template"]["version"] = "unapproved-after-construction"
    assert {entry.template.version for entry, _ in registry.definitions()} == {"1"}
