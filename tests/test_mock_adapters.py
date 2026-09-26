from __future__ import annotations

import asyncio

import pytest

from rfa_mas.adapters.mock import MockResponse, MockTool
from rfa_mas.contracts import (
    Audience,
    DomainId,
    DraftBundle,
    DraftTarget,
    PublicationStatus,
    ResultStatus,
    ReviewStatus,
    SimulationScenario,
    ToolEffect,
    ToolRequest,
    sha256_text,
)
from rfa_mas.errors import RfaError


def draft(content: str = "safe", *, draft_id: str = "draft-test") -> DraftBundle:
    return DraftBundle(
        request_id="req-test",
        trace_id="trace-test",
        run_id="run-test",
        agent_id="assistant-supervisor",
        domain_id=DomainId.TRIV3,
        draft_id=draft_id,
        version=1,
        content_hash=sha256_text(content),
        target=DraftTarget(audience=Audience.PUBLIC),
        audience=Audience.PUBLIC,
        policy_version="local-v1",
        allowed_evidence=(),
        content=content,
        simulated=True,
        adapter="test",
    )


async def test_review_idempotency_is_bound_to_exact_draft() -> None:
    adapter = MockResponse()
    first = await adapter.submit_draft(draft(), idempotency_key="same")
    second = await adapter.submit_draft(draft(), idempotency_key="same")
    assert first == second

    with pytest.raises(RfaError, match="idempotency"):
        await adapter.submit_draft(draft("changed"), idempotency_key="same")

    with pytest.raises(RfaError, match="idempotency"):
        await adapter.submit_draft(draft(draft_id="different-draft"), idempotency_key="same")


async def test_concurrent_review_duplicates_share_one_bound_result() -> None:
    adapter = MockResponse()
    first, second = await asyncio.gather(
        adapter.submit_draft(draft(), idempotency_key="concurrent"),
        adapter.submit_draft(draft(), idempotency_key="concurrent"),
    )
    assert first is second


async def test_review_timeout_is_outcome_unknown_and_not_retried() -> None:
    adapter = MockResponse()
    result = await adapter.submit_draft(
        draft(),
        idempotency_key="timeout-once",
        simulation_scenario=SimulationScenario.TIMEOUT,
    )
    assert result.decision == ReviewStatus.PENDING
    assert result.publication_status == PublicationStatus.NOT_REQUESTED
    assert "게시 요청은 실행되지 않았습니다" in result.safe_reason


async def test_tool_write_is_always_denied_in_p0() -> None:
    adapter = MockTool()
    request = ToolRequest(
        request_id="req",
        trace_id="trace",
        run_id="run",
        agent_id="agent",
        domain_id=DomainId.TRIV3,
        idempotency_key="write-1",
        tool_name="publish",
        effect=ToolEffect.WRITE,
        arguments={"value": "test"},
    )
    first = await adapter.execute(request)
    second = await adapter.execute(request)
    assert first == second
    assert first.status == ResultStatus.DENIED


async def test_tool_idempotency_rejects_changed_payload() -> None:
    adapter = MockTool()
    original = ToolRequest(
        request_id="req",
        trace_id="trace",
        run_id="run",
        agent_id="agent",
        domain_id=DomainId.TRIV3,
        idempotency_key="read-1",
        tool_name="lookup",
        effect=ToolEffect.READ,
        arguments={"term": "first"},
    )
    await adapter.execute(original)

    with pytest.raises(RfaError, match="idempotency"):
        await adapter.execute(original.model_copy(update={"arguments": {"term": "different"}}))


async def test_tool_write_timeout_reports_unknown_outcome() -> None:
    result = await MockTool().execute(
        ToolRequest(
            request_id="req",
            trace_id="trace",
            run_id="run",
            agent_id="agent",
            domain_id=DomainId.TRIV3,
            idempotency_key="write-timeout",
            tool_name="publish",
            effect=ToolEffect.WRITE,
            arguments={"simulate": "timeout"},
        )
    )
    assert result.status == ResultStatus.OUTCOME_UNKNOWN
    assert result.error and result.error.retryable is False
