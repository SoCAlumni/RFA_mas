from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from rfa_mas.adapters.http import (
    PolicyHttpAdapter,
    ReferenceHttpClient,
    ResponseHttpAdapter,
    RuntimeHttpAdapter,
    ToolHttpAdapter,
)
from rfa_mas.adapters.local import LocalPolicy, LocalRuntime
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
    ReviewStatus,
    SimulationScenario,
    TaskRequest,
    TeamBudget,
    TeamMember,
    TeamSpec,
    TeamTemplate,
    ToolEffect,
    ToolRequest,
    TrustedPrincipal,
    WorkRequest,
    sha256_text,
)
from rfa_mas.errors import BackendNotImplementedError, OutcomeUnknownError, RfaError
from rfa_mas.reference import create_reference_contract_app
from rfa_mas.reference.local_runtime import create_local_runtime_app
from rfa_mas.reference.local_security import LocalServiceBoundary


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



# -- P1-008: one suite for mock and reference HTTP, TeamSpec lifecycle, versions ----------


SYNTHETIC_TOKEN = SecretStr("synthetic-reference-token-p1008-0001")


def review_draft(label: str) -> DraftBundle:
    content = f"reference contract draft {label}"
    return make_draft().model_copy(
        update={
            "draft_id": f"draft-{label}",
            "run_id": f"run-{label}",
            "content": content,
            "content_hash": sha256_text(content),
        }
    )


@asynccontextmanager
async def review_port(transport: str):
    """The same consumer-facing ResponsePort, backed by mock or reference HTTP."""
    if transport == "mock":
        port = MockResponse()

        async def decide(draft, decision, version=None):
            return await port.decide(
                draft.draft_id,
                draft_version=version or draft.version,
                content_hash=draft.content_hash,
                decision=decision,
            )

        yield port, decide
        return
    app = create_reference_contract_app(service_token=SYNTHETIC_TOKEN, manual_decisions=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        reference = ReferenceHttpClient(client, token=SYNTHETIC_TOKEN, max_read_retries=1)
        port = ResponseHttpAdapter(reference, simulated=True)

        async def decide(draft, decision, version=None):
            response = await reference.request(
                "POST",
                f"/v1/reviews/{draft.draft_id}/decision",
                json_body={
                    "draft_version": version or draft.version,
                    "content_hash": draft.content_hash,
                    "decision": decision.value,
                },
            )
            return response.json()

        yield port, decide


@pytest.mark.parametrize("transport", ["mock", "http"])
async def test_review_outcomes_mean_the_same_over_mock_and_reference_http(transport) -> None:
    async with review_port(transport) as (port, decide):
        outcomes = {}
        for label, scenario in (
            ("approved", SimulationScenario.SUCCESS),
            ("revision", SimulationScenario.REVISION_REQUESTED),
            ("timeout", SimulationScenario.TIMEOUT),
            ("rejected", SimulationScenario.TIMEOUT),
        ):
            outcomes[label] = await port.submit_draft(
                review_draft(label), idempotency_key=f"k-{label}", simulation_scenario=scenario
            )
        replay = await port.submit_draft(
            review_draft("approved"), idempotency_key="k-approved"
        )
        await decide(review_draft("rejected"), ReviewStatus.REJECTED)
        rejected = await port.get_decision("draft-rejected")
        with pytest.raises(RfaError):  # A stale version can never be decided.
            await decide(review_draft("timeout"), ReviewStatus.APPROVED, version=2)
        missing = await port.get_decision("draft-never-submitted")
    assert outcomes["approved"].decision == ReviewStatus.APPROVED
    assert outcomes["revision"].decision == ReviewStatus.REVISION_REQUESTED
    assert outcomes["timeout"].decision == ReviewStatus.PENDING  # authority timeout: pending
    assert outcomes["timeout"].publication_status.value == "not_requested"
    assert rejected.decision == ReviewStatus.REJECTED
    assert rejected.draft_version == 1
    assert rejected.content_hash == review_draft("rejected").content_hash
    assert replay.model_dump(exclude={"adapter"}) == outcomes["approved"].model_dump(
        exclude={"adapter"}
    )
    assert missing is None
    assert all(item.simulated for item in outcomes.values())


async def test_reference_identity_proof_is_required_before_any_handler() -> None:
    app = create_reference_contract_app(service_token=SYNTHETIC_TOKEN, manual_decisions=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        for token in (None, SecretStr("forged-reference-token-p1008-0000")):
            port = ResponseHttpAdapter(
                ReferenceHttpClient(client, token=token, max_read_retries=0), simulated=True
            )
            with pytest.raises(RfaError) as denied:
                await port.submit_draft(make_draft(), idempotency_key="unauthenticated")
            assert "401" in denied.value.safe_message
        honest = ResponseHttpAdapter(
            ReferenceHttpClient(client, token=SYNTHETIC_TOKEN, max_read_retries=0), simulated=True
        )
        # Nothing was accepted from the unauthenticated attempts.
        assert await honest.get_decision(make_draft().draft_id) is None


async def test_unsupported_contract_version_is_an_explicit_error() -> None:
    decision = await MockResponse().submit_draft(make_draft(), idempotency_key="future")
    future = decision.model_dump(mode="json") | {"schema_version": "2.0"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=future)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://127.0.0.1"
    ) as client:
        adapter = ResponseHttpAdapter(
            ReferenceHttpClient(client, token=None, max_read_retries=0), simulated=True
        )
        for call in (
            adapter.submit_draft(make_draft(), idempotency_key="future"),
            adapter.get_decision("draft-http"),
        ):
            with pytest.raises(RfaError) as unsupported:
                await call
            assert unsupported.value.code == "unsupported_contract_version"


RUNTIME_HOST = "127.0.0.1:8782"
RUNTIME_OWNER = "installation-owner-p1008"


def team_spec(digest: str = "d" * 64) -> TeamSpec:
    team_id = "team_p1008_research"
    domain = DomainId.QUANTIZATION_RESEARCH
    caps = {"supervisor": (), "source_scout": ("evidence_search",),
            "evidence_reviewer": ("evidence_review",)}
    return TeamSpec(
        task_id=f"task-{team_id}",
        team_id=team_id,
        domain_id=domain,
        owner_id=RUNTIME_OWNER,
        template=TeamTemplate(
            template_id="tmpl-research",
            version="v1",
            pattern="research",
            approved=True,
            required_capabilities=("evidence_review", "evidence_search"),
            runtime_kind="local",
            budget=TeamBudget(),
        ),
        members=tuple(
            TeamMember(
                role=role,
                spec=AgentSpec(
                    agent_id=f"{team_id}:{role}",
                    domain_id=domain,
                    memory_namespace=f"teams/{team_id}/{role}",
                    capabilities=caps[role],
                    allowed_audiences=(Audience.OWNER, Audience.PUBLIC),
                    instructions_ref=f"approved:{role}",
                    max_steps=10,
                    max_tool_calls=2,
                ),
                tool_names=("synthetic.glossary_lookup",) if role == "source_scout" else (),
            )
            for role in caps
        ),
        definition_digest=digest,
        execution_budget=TeamBudget(max_steps=20, max_tool_calls=5),
    )


async def test_team_spec_round_trip_matches_local_runtime(tmp_path) -> None:
    spec = team_spec()
    local = LocalRuntime(timeout_seconds=5)
    local_ready = await local.prepare(spec, idempotency_key="prepare-1")
    local_clean = await local.cleanup(spec.team_id, idempotency_key="cleanup-1")
    app = create_local_runtime_app(
        db_path=tmp_path / "runtime" / "runtime.db",
        boundary=LocalServiceBoundary.create(
            owner_id=RUNTIME_OWNER, service_token=SYNTHETIC_TOKEN, allowed_hosts=[RUNTIME_HOST]
        ),
        clock=lambda: datetime(2026, 9, 27, tzinfo=UTC),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=f"http://{RUNTIME_HOST}"
    ) as client:
        adapter = RuntimeHttpAdapter(
            ReferenceHttpClient(client, token=SYNTHETIC_TOKEN, max_read_retries=0), simulated=True
        )
        ready = await adapter.prepare(spec, idempotency_key="prepare-1")
        replay = await adapter.prepare(spec, idempotency_key="prepare-1")
        with pytest.raises(RfaError) as changed:
            await adapter.prepare(team_spec(digest="e" * 64), idempotency_key="prepare-1")
        cleaned = await adapter.cleanup(spec.team_id, idempotency_key="cleanup-1")
    assert "409" in changed.value.safe_message
    for prepared, done in ((local_ready, local_clean), (ready, cleaned)):
        assert prepared.spec == spec and prepared.state == "ready"
        assert [m.agent_id for m in prepared.member_states] == [
            m.spec.agent_id for m in spec.members
        ]
        assert {m.prepare for m in prepared.member_states} == {"prepared"}
        assert done.state == "cleaned"
        assert {m.cleanup for m in done.member_states} == {"cleaned"}
    assert replay == ready
    assert ready.sandbox_id is None  # The stand-in is not a sandbox and never claims one.


@pytest.mark.parametrize("operation", ["prepare", "cleanup"])
async def test_team_side_effect_timeout_is_outcome_unknown_and_not_retried(operation) -> None:
    calls = 0

    def timeout(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(timeout), base_url="http://127.0.0.1"
    ) as client:
        adapter = RuntimeHttpAdapter(
            ReferenceHttpClient(client, token=None, max_read_retries=3), simulated=True
        )
        with pytest.raises(OutcomeUnknownError):
            if operation == "prepare":
                await adapter.prepare(team_spec(), idempotency_key="prepare-timeout")
            else:
                await adapter.cleanup("team_p1008_research", idempotency_key="cleanup-timeout")
    assert calls == 1
