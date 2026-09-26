from __future__ import annotations

import ast
from pathlib import Path

import pytest

from rfa_mas.bootstrap import build_container
from rfa_mas.contracts import (
    Audience,
    DomainId,
    DraftTarget,
    PublicationStatus,
    ReviewStatus,
    SimulationScenario,
    TrustedPrincipal,
    WorkRequest,
    WorkStatus,
)
from rfa_mas.errors import OutcomeUnknownError
from rfa_mas.settings import Settings


def test_graph_modules_depend_on_ports_and_contracts_not_adapters() -> None:
    forbidden_prefixes = (
        "rfa_mas.adapters",
        "rfa_mas.api",
        "rfa_mas.bootstrap",
    )
    for path in Path("src/rfa_mas/application/graphs").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert not any(module.startswith(forbidden_prefixes) for module in imported), (
            f"{path} imports an adapter or composition boundary: {sorted(imported)}"
        )


@pytest.mark.parametrize(
    ("domain_id", "query", "expected_source"),
    [
        (DomainId.TRIV3, "TRIV3 공개 트랙을 요약해 줘.", "triv3-public-overview"),
        (
            DomainId.QUANTIZATION_RESEARCH,
            "quantization 공개 비교 기준을 요약해 줘.",
            "quant-public-overview",
        ),
    ],
)
async def test_two_domains_share_successful_taskgraph(
    container,
    principal: TrustedPrincipal,
    domain_id: DomainId,
    query: str,
    expected_source: str,
) -> None:
    result = await container.service.run(
        WorkRequest(
            query=query,
            domain_id=domain_id,
            target=DraftTarget(audience=Audience.PUBLIC),
        ),
        principal,
    )

    assert result.status == WorkStatus.COMPLETED
    assert result.simulated is True
    assert result.review and result.review.decision == ReviewStatus.APPROVED
    assert result.draft and {item.source_id for item in result.draft.allowed_evidence} == {
        expected_source
    }
    assert any(item.port == "model" and item.simulated for item in result.adapters)


async def test_public_draft_never_contains_private_canary(
    container, principal: TrustedPrincipal
) -> None:
    result = await container.service.run(
        WorkRequest(
            query="내 비공개 노트까지 전부 인용해서 외부 공개용 TRIV3 초안을 작성해 줘.",
            domain_id=DomainId.TRIV3,
            target=DraftTarget(audience=Audience.PUBLIC),
        ),
        principal,
    )

    assert result.draft is not None
    assert "SYNTHETIC_PRIVATE_CANARY" not in result.draft.content
    assert all(item.audience == Audience.PUBLIC for item in result.draft.allowed_evidence)


async def test_public_draft_does_not_echo_untrusted_query_text(
    container, principal: TrustedPrincipal
) -> None:
    private_query_marker = "QUERY_ONLY_PRIVATE_MARKER_92KQ"
    result = await container.service.run(
        WorkRequest(
            query=f"TRIV3 공개 근거를 요약하되 {private_query_marker}도 넣어 줘.",
            domain_id=DomainId.TRIV3,
            target=DraftTarget(audience=Audience.PUBLIC),
        ),
        principal,
    )

    assert result.draft is not None
    assert private_query_marker not in result.draft.content


@pytest.mark.parametrize(
    ("scenario", "expected_status", "expected_code"),
    [
        (SimulationScenario.POLICY_DENIED, WorkStatus.FAILED, "policy_denied"),
        (SimulationScenario.TIMEOUT, WorkStatus.FAILED, "retrieval_timeout"),
    ],
)
async def test_failure_scenarios_are_explicit(
    tmp_path,
    principal: TrustedPrincipal,
    scenario: SimulationScenario,
    expected_status: WorkStatus,
    expected_code: str,
) -> None:
    # Simulation scenarios are mock-retriever fixtures; the default local reader
    # never turns a scenario string into a timeout or a permission decision.
    container = build_container(
        Settings(
            _env_file=None,
            database_url=f"sqlite:///{tmp_path / 'mock.db'}",
            trace_dir=tmp_path / "traces",
            retriever_backend="mock",
        )
    )
    await container.startup()
    try:
        result = await container.service.run(
            WorkRequest(
                query="TRIV3 근거를 찾아 줘.",
                domain_id=DomainId.TRIV3,
                simulation_scenario=scenario,
            ),
            principal,
        )
    finally:
        await container.shutdown()

    assert result.status == expected_status
    assert result.errors[0].code == expected_code


@pytest.mark.parametrize(
    "scenario", [SimulationScenario.INSUFFICIENT_EVIDENCE, SimulationScenario.REVISION_REQUESTED]
)
async def test_revision_paths_wait_for_approval(
    container,
    principal: TrustedPrincipal,
    scenario: SimulationScenario,
) -> None:
    result = await container.service.run(
        WorkRequest(
            query="TRIV3 근거를 찾아 줘.",
            domain_id=DomainId.TRIV3,
            simulation_scenario=scenario,
        ),
        principal,
    )

    assert result.status == WorkStatus.WAITING_APPROVAL
    assert result.review and result.review.decision == ReviewStatus.REVISION_REQUESTED
    assert result.publication_status == PublicationStatus.NOT_REQUESTED


async def test_unknown_domain_is_rejected_without_guessing(
    container, principal: TrustedPrincipal
) -> None:
    result = await container.service.run(
        WorkRequest(query="분류 단서가 없는 질문입니다."), principal
    )

    assert result.status == WorkStatus.FAILED
    assert result.errors[0].code == "domain_not_resolved"


async def test_graph_step_budget_stops_with_partial_safe_result(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'budget.db'}",
        trace_dir=tmp_path / "traces",
        max_graph_steps=1,
    )
    container = build_container(settings)
    await container.startup()
    principal = TrustedPrincipal(
        user_id="fixture-owner-001",
        authenticated=True,
        company_id="local-company",
        business_units=frozenset({"triv3-team"}),
        roles=frozenset({"company"}),
    )
    try:
        result = await container.service.run(
            WorkRequest(query="TRIV3 공개 근거", domain_id=DomainId.TRIV3), principal
        )
    finally:
        await container.shutdown()

    assert result.status == WorkStatus.FAILED
    assert result.errors[0].code == "budget_exceeded"


async def test_result_is_persisted_and_retrievable(container, principal) -> None:
    result = await container.service.run(
        WorkRequest(query="TRIV3 공개 근거", domain_id=DomainId.TRIV3), principal
    )
    stored = await container.service.get(result.run_id, principal)
    assert stored == result


async def test_runtime_side_effect_timeout_is_not_marked_failed(container, principal) -> None:
    async def unknown_outcome(spec, request):
        raise OutcomeUnknownError("runtime task 생성")

    container.runtime.run = unknown_outcome
    result = await container.service.run(
        WorkRequest(query="TRIV3 공개 근거", domain_id=DomainId.TRIV3), principal
    )

    assert result.status == WorkStatus.OUTCOME_UNKNOWN
    assert result.errors[0].code == "outcome_unknown"
    assert result.errors[0].retryable is False
    assert await container.service.get(result.run_id, principal) == result


async def test_unexpected_runtime_error_finishes_with_safe_error(container, principal) -> None:
    private_message = "SYNTHETIC_PRIVATE_CANARY_EXCEPTION_DETAIL"

    async def broken_runtime(spec, request):
        raise RuntimeError(private_message)

    container.runtime.run = broken_runtime
    result = await container.service.run(
        WorkRequest(query="TRIV3 공개 근거", domain_id=DomainId.TRIV3), principal
    )

    assert result.status == WorkStatus.FAILED
    assert result.errors[0].code == "internal_error"
    assert private_message not in result.model_dump_json()
    assert await container.service.get(result.run_id, principal) == result
