from __future__ import annotations

import httpx
import pytest

from rfa_mas.adapters.http import (
    PolicyHttpAdapter,
    ReferenceHttpClient,
    ResponseHttpAdapter,
    RuntimeHttpAdapter,
    ToolHttpAdapter,
)
from rfa_mas.adapters.local import LocalPolicy
from rfa_mas.adapters.mock import MockResponse, MockTool
from rfa_mas.contracts import (
    AgentSpec,
    Audience,
    DomainId,
    DraftBundle,
    DraftTarget,
    EvidenceBundle,
    PolicyRequest,
    ResultStatus,
    TaskRequest,
    ToolEffect,
    ToolRequest,
    TrustedPrincipal,
    WorkRequest,
    sha256_text,
)
from rfa_mas.errors import BackendNotImplementedError, RfaError
from rfa_mas.reference import create_reference_contract_app


def make_draft() -> DraftBundle:
    content = "reference contract draft"
    return DraftBundle(
        request_id="req-http",
        trace_id="trace-http",
        run_id="run-http",
        agent_id="assistant-supervisor",
        domain_id=DomainId.TRIV3,
        draft_id="draft-http",
        version=1,
        content_hash=sha256_text(content),
        target=DraftTarget(audience=Audience.PUBLIC),
        audience=Audience.PUBLIC,
        policy_version="local-v1",
        allowed_evidence=(),
        content=content,
        simulated=True,
        adapter="fixture",
    )


def make_tool(effect: ToolEffect = ToolEffect.READ) -> ToolRequest:
    return ToolRequest(
        request_id="req-tool",
        trace_id="trace-tool",
        run_id="run-tool",
        agent_id="agent",
        domain_id=DomainId.TRIV3,
        idempotency_key=f"tool-{effect.value}",
        tool_name="lookup" if effect == ToolEffect.READ else "publish",
        effect=effect,
        arguments={"term": "synthetic"},
    )


def make_agent_spec() -> AgentSpec:
    return AgentSpec(
        agent_id="triv3-task-supervisor",
        domain_id=DomainId.TRIV3,
        memory_namespace="domain:triv3",
        capabilities=("draft",),
        allowed_audiences=(Audience.PUBLIC,),
        instructions_ref="fixture://triv3",
        max_steps=4,
        max_tool_calls=1,
    )


def make_task() -> TaskRequest:
    work = WorkRequest(
        request_id="req-runtime",
        trace_id="trace-runtime",
        run_id="run-runtime",
        domain_id=DomainId.TRIV3,
        query="synthetic public reference contract request",
        target=DraftTarget(audience=Audience.PUBLIC),
    )
    return TaskRequest(
        request_id="req-runtime",
        trace_id="trace-runtime",
        run_id="run-runtime",
        agent_id="triv3-task-supervisor",
        domain_id=DomainId.TRIV3,
        idempotency_key="runtime-one",
        task_type="domain_task",
        payload={"work_request": work.model_dump(mode="json")},
    )


def make_policy_request() -> PolicyRequest:
    return PolicyRequest(
        request_id="req-policy",
        trace_id="trace-policy",
        run_id="run-policy",
        agent_id="assistant-supervisor",
        domain_id=DomainId.TRIV3,
        action="retrieve",
        resource_audience=Audience.PUBLIC,
        target_audience=Audience.PUBLIC,
        principal=TrustedPrincipal(user_id="external", authenticated=False),
    )


async def test_mock_and_reference_http_response_use_same_dto() -> None:
    app = create_reference_contract_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        http_adapter = ResponseHttpAdapter(
            ReferenceHttpClient(client, token=None, max_read_retries=1), simulated=True
        )
        http_result = await http_adapter.submit_draft(make_draft(), idempotency_key="review-http")
    mock_result = await MockResponse().submit_draft(make_draft(), idempotency_key="review-mock")
    assert type(http_result) is type(mock_result)
    assert http_result.model_dump(exclude={"adapter"}) == mock_result.model_dump(
        exclude={"adapter"}
    )
    assert http_result.adapter == "reference-http-response"


async def test_mock_and_reference_http_tool_use_same_dto() -> None:
    request = make_tool()
    app = create_reference_contract_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        http_adapter = ToolHttpAdapter(
            ReferenceHttpClient(client, token=None, max_read_retries=1), simulated=True
        )
        http_result = await http_adapter.execute(request)
    mock_result = await MockTool().execute(request)
    assert type(http_result) is type(mock_result)
    assert http_result.model_dump(exclude={"adapter"}) == mock_result.model_dump(
        exclude={"adapter"}
    )
    assert http_result.adapter == "reference-http-tool"


async def test_reference_http_runtime_uses_shared_task_contract() -> None:
    app = create_reference_contract_app()
    task = make_task()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        adapter = RuntimeHttpAdapter(
            ReferenceHttpClient(client, token=None, max_read_retries=1), simulated=True
        )
        created = await adapter.run(make_agent_spec(), task)
        fetched = await adapter.status(task.run_id)
        cancelled = await adapter.cancel(task.run_id)

    assert created.status == ResultStatus.SUCCEEDED
    draft = DraftBundle.model_validate(created.output["draft"])
    evidence = EvidenceBundle.model_validate(created.output["evidence"])
    assert created.output["steps"] == 3
    assert draft.target.audience == Audience.PUBLIC
    assert all(item.audience == Audience.PUBLIC for item in draft.allowed_evidence)
    assert all(item.audience == Audience.PUBLIC for item in evidence.items)
    assert created.adapter == "reference-http-runtime"
    assert fetched == created
    assert cancelled.error and cancelled.error.code == "cancelled"
    assert cancelled.adapter == "reference-http-runtime"


async def test_reference_runtime_rejects_changed_body_for_same_idempotency_key() -> None:
    app = create_reference_contract_app()
    task = make_task()
    original_spec = make_agent_spec()
    changed_spec = original_spec.model_copy(update={"capabilities": ("changed-capability",)})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        adapter = RuntimeHttpAdapter(
            ReferenceHttpClient(client, token=None, max_read_retries=1), simulated=True
        )
        original = await adapter.run(original_spec, task)
        conflict = await adapter.run(changed_spec, task)
        fetched = await adapter.status(task.run_id)

    assert original.status == ResultStatus.SUCCEEDED
    assert conflict.status == ResultStatus.FAILED
    assert conflict.error and conflict.error.code == "idempotency_conflict"
    assert fetched == original


async def test_local_and_reference_http_policy_use_same_dto() -> None:
    request = make_policy_request()
    local_result = await LocalPolicy().evaluate(request)
    app = create_reference_contract_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        adapter = PolicyHttpAdapter(
            ReferenceHttpClient(client, token=None, max_read_retries=1), simulated=True
        )
        http_result = await adapter.evaluate(request)

    assert type(http_result) is type(local_result)
    assert http_result.model_dump(exclude={"adapter", "simulated"}) == local_result.model_dump(
        exclude={"adapter", "simulated"}
    )
    assert http_result.adapter == "reference-http-policy"
    assert http_result.simulated is True


async def test_review_submission_timeout_is_not_retried() -> None:
    calls = 0

    def timeout_transport(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(timeout_transport), base_url="http://127.0.0.1"
    ) as client:
        adapter = ResponseHttpAdapter(
            ReferenceHttpClient(client, token=None, max_read_retries=3), simulated=True
        )
        result = await adapter.submit_draft(make_draft(), idempotency_key="review-timeout")
    assert calls == 1
    assert result.decision.value == "pending"
    assert result.publication_status.value == "not_requested"


async def test_http_tool_write_is_blocked_before_transport() -> None:
    calls = 0

    def counting_transport(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(counting_transport), base_url="http://127.0.0.1"
    ) as client:
        adapter = ToolHttpAdapter(
            ReferenceHttpClient(client, token=None, max_read_retries=3), simulated=True
        )
        result = await adapter.execute(make_tool(ToolEffect.WRITE))

    assert calls == 0
    assert result.status == ResultStatus.DENIED
    assert result.error and result.error.code == "external_writes_disabled"


async def test_reference_http_client_rejects_non_loopback_endpoints() -> None:
    async with httpx.AsyncClient(base_url="https://live.example") as client:
        with pytest.raises(BackendNotImplementedError, match="non-loopback"):
            ReferenceHttpClient(client, token=None, max_read_retries=1)


async def test_read_only_tool_transport_is_retried_within_budget() -> None:
    calls = 0
    expected = await MockTool().execute(make_tool())

    def flaky_transport(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("synthetic connect failure", request=request)
        return httpx.Response(200, json=expected.model_dump(mode="json"), request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(flaky_transport), base_url="http://127.0.0.1"
    ) as client:
        adapter = ToolHttpAdapter(
            ReferenceHttpClient(client, token=None, max_read_retries=1), simulated=True
        )
        result = await adapter.execute(make_tool())

    assert calls == 2
    assert result.status == ResultStatus.SUCCEEDED


async def test_malformed_upstream_response_becomes_safe_contract_error() -> None:
    fake_secret = "upstream-secret-that-must-not-leak"

    def malformed_transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"token": fake_secret}, request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(malformed_transport), base_url="http://127.0.0.1"
    ) as client:
        adapter = ToolHttpAdapter(
            ReferenceHttpClient(client, token=None, max_read_retries=0), simulated=True
        )
        with pytest.raises(RfaError) as captured:
            await adapter.execute(make_tool())

    assert captured.value.code == "upstream_contract_error"
    assert fake_secret not in captured.value.safe_message
