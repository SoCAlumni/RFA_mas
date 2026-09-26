from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from rfa_mas.adapters.http import ReferenceHttpClient, RuntimeHttpAdapter
from rfa_mas.adapters.mock import MockResponse
from rfa_mas.application.graphs.supervisor import (
    SupervisorDependencies,
    build_supervisor_graph,
)
from rfa_mas.contracts import (
    AgentSpec,
    Audience,
    DomainId,
    DraftBundle,
    DraftTarget,
    EvidenceRef,
    ResultStatus,
    ReviewDecision,
    SourceLocation,
    TaskRequest,
    TaskResult,
    TrustedPrincipal,
    WorkRequest,
    WorkStatus,
    sha256_text,
)
from rfa_mas.reference import create_reference_contract_app


def make_work(*, audience: Audience = Audience.PUBLIC) -> WorkRequest:
    return WorkRequest(
        request_id="req-supervisor-boundary",
        trace_id="trace-supervisor-boundary",
        run_id="run-supervisor-boundary",
        idempotency_key="idem-supervisor-boundary",
        domain_id=DomainId.TRIV3,
        query="TRIV3 공개 근거를 요약해 줘.",
        target=DraftTarget(audience=audience),
    )


def make_principal(*, authenticated: bool = True) -> TrustedPrincipal:
    return TrustedPrincipal(
        user_id="fixture-owner-001",
        authenticated=authenticated,
        company_id="local-company" if authenticated else None,
        business_units=frozenset({"triv3-team"}) if authenticated else frozenset(),
    )


def make_draft(
    task: TaskRequest,
    work: WorkRequest,
    *,
    evidence_audience: Audience = Audience.PUBLIC,
) -> DraftBundle:
    content = "synthetic supervisor boundary draft"
    evidence_text = "synthetic evidence"
    return DraftBundle(
        request_id=task.request_id,
        trace_id=task.trace_id,
        run_id=task.run_id,
        agent_id=task.agent_id,
        domain_id=task.domain_id,
        draft_id="draft-supervisor-boundary",
        content_hash=sha256_text(content),
        target=work.target,
        audience=work.target.audience,
        policy_version="test-v1",
        allowed_evidence=(
            EvidenceRef(
                source_id="source-boundary",
                source_revision="1",
                location=SourceLocation(uri="fixture://boundary"),
                audience=evidence_audience,
                content_hash=sha256_text(evidence_text),
            ),
        ),
        content=content,
        simulated=True,
        adapter="test-runtime",
    )


class RuntimeStub:
    simulated = True
    adapter_name = "test-runtime"

    def __init__(
        self,
        mutate: Callable[[TaskResult, TaskRequest, WorkRequest], Any] | None = None,
    ) -> None:
        self.mutate = mutate
        self.spec: AgentSpec | None = None

    async def run(self, spec: AgentSpec, request: TaskRequest) -> Any:
        self.spec = spec
        work = WorkRequest.model_validate(request.payload["work_request"])
        result = TaskResult(
            request_id=request.request_id,
            trace_id=request.trace_id,
            run_id=request.run_id,
            agent_id=request.agent_id,
            domain_id=request.domain_id,
            status=ResultStatus.SUCCEEDED,
            output={
                "draft": make_draft(request, work).model_dump(mode="json"),
                "steps": 3,
            },
            simulated=True,
            adapter=self.adapter_name,
        )
        return self.mutate(result, request, work) if self.mutate else result

    async def status(self, run_id: str) -> TaskResult | None:
        return None

    async def cancel(self, run_id: str) -> TaskResult:
        raise AssertionError("cancel is not used by the supervisor test")


class ReviewStub:
    simulated = True
    adapter_name = "test-review"

    def __init__(self, mutate: Callable[[ReviewDecision], Any] | None = None) -> None:
        self._mock = MockResponse()
        self.mutate = mutate
        self.calls = 0

    async def submit_draft(self, draft: DraftBundle, **kwargs: Any) -> Any:
        self.calls += 1
        decision = await self._mock.submit_draft(draft, **kwargs)
        return self.mutate(decision) if self.mutate else decision

    async def get_decision(self, draft_id: str) -> ReviewDecision | None:
        return await self._mock.get_decision(draft_id)


async def run_graph(
    runtime: Any,
    response: Any,
    *,
    work: WorkRequest | None = None,
    principal: TrustedPrincipal | None = None,
    max_graph_steps: int = 12,
) -> dict[str, Any]:
    graph = build_supervisor_graph(
        SupervisorDependencies(
            runtime=runtime,
            response=response,
            max_graph_steps=max_graph_steps,
            max_tool_calls=6,
        )
    )
    return await graph.ainvoke(
        {
            "work": work or make_work(),
            "principal": principal or make_principal(),
            "steps": 0,
        }
    )


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (Audience.PUBLIC, (Audience.PUBLIC,)),
        (Audience.COMPANY, (Audience.PUBLIC, Audience.COMPANY)),
        (
            Audience.BUSINESS_UNIT,
            (Audience.PUBLIC, Audience.COMPANY, Audience.BUSINESS_UNIT),
        ),
        (
            Audience.OWNER,
            (Audience.PUBLIC, Audience.COMPANY, Audience.BUSINESS_UNIT, Audience.OWNER),
        ),
        (
            Audience.PRIVATE,
            (
                Audience.PUBLIC,
                Audience.COMPANY,
                Audience.BUSINESS_UNIT,
                Audience.OWNER,
                Audience.PRIVATE,
            ),
        ),
    ],
)
async def test_agent_spec_uses_minimum_trusted_audience_scope(
    target: Audience, expected: tuple[Audience, ...]
) -> None:
    runtime = RuntimeStub()
    state = await run_graph(runtime, ReviewStub(), work=make_work(audience=target))

    assert state["status"] == WorkStatus.COMPLETED
    assert runtime.spec is not None
    assert runtime.spec.allowed_audiences == expected


async def test_unauthenticated_principal_only_delegates_public_scope() -> None:
    runtime = RuntimeStub()
    await run_graph(
        runtime,
        ReviewStub(),
        work=make_work(audience=Audience.OWNER),
        principal=make_principal(authenticated=False),
    )

    assert runtime.spec is not None
    assert runtime.spec.allowed_audiences == (Audience.PUBLIC,)


async def test_runtime_result_common_identifiers_are_rebound() -> None:
    runtime = RuntimeStub(
        lambda result, _task, _work: result.model_copy(update={"trace_id": "wrong-trace"})
    )
    review = ReviewStub()
    state = await run_graph(runtime, review)

    assert state["status"] == WorkStatus.FAILED
    assert state["error"].code == "runtime_result_binding_mismatch"
    assert state["error"].trace_id == make_work().trace_id
    assert review.calls == 0


@pytest.mark.parametrize(
    "invalid_result",
    [
        {"unexpected": "shape"},
        TaskResult(
            request_id="req-supervisor-boundary",
            trace_id="trace-supervisor-boundary",
            run_id="run-supervisor-boundary",
            agent_id="domain-supervisor:triv3",
            domain_id=DomainId.TRIV3,
            status=ResultStatus.SUCCEEDED,
            output={"steps": 3},
            simulated=True,
            adapter="test-runtime",
        ),
    ],
)
async def test_invalid_or_missing_runtime_draft_returns_safe_error(
    invalid_result: Any,
) -> None:
    runtime = RuntimeStub(lambda _result, _task, _work: invalid_result)
    state = await run_graph(runtime, ReviewStub())

    assert state["status"] == WorkStatus.FAILED
    assert state["error"].code in {"invalid_runtime_result", "invalid_runtime_output"}
    assert "unexpected" not in state["error"].message


async def test_public_draft_rejects_non_public_evidence_from_runtime() -> None:
    def with_company_evidence(
        result: TaskResult, task: TaskRequest, work: WorkRequest
    ) -> TaskResult:
        draft = make_draft(task, work, evidence_audience=Audience.COMPANY)
        return result.model_copy(
            update={"output": {"draft": draft.model_dump(mode="json"), "steps": 3}}
        )

    review = ReviewStub()
    state = await run_graph(RuntimeStub(with_company_evidence), review)

    assert state["status"] == WorkStatus.FAILED
    assert state["error"].code == "draft_binding_mismatch"
    assert review.calls == 0


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("request_id", "wrong-request"),
        ("trace_id", "wrong-trace"),
        ("run_id", "wrong-run"),
        ("agent_id", "wrong-agent"),
        ("domain_id", DomainId.QUANTIZATION_RESEARCH),
        ("draft_id", "wrong-draft"),
        ("draft_version", 2),
        ("content_hash", "0" * 64),
        ("target", DraftTarget(audience=Audience.OWNER)),
    ],
)
async def test_review_decision_requires_full_request_and_draft_binding(
    field: str, wrong_value: Any
) -> None:
    response = ReviewStub(lambda decision: decision.model_copy(update={field: wrong_value}))
    state = await run_graph(RuntimeStub(), response)

    assert state["status"] == WorkStatus.FAILED
    assert state["error"].code == "approval_binding_mismatch"


async def test_step_budget_keeps_partial_draft_and_skips_review() -> None:
    review = ReviewStub()
    state = await run_graph(RuntimeStub(), review, max_graph_steps=5)

    assert state["status"] == WorkStatus.FAILED
    assert state["error"].code == "budget_exceeded"
    assert state["steps"] == 5
    assert state["draft"].draft_id == "draft-supervisor-boundary"
    assert review.calls == 0


async def test_reference_http_runtime_completes_supervisor_flow_locally() -> None:
    app = create_reference_contract_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        runtime = RuntimeHttpAdapter(
            ReferenceHttpClient(client, token=None, max_read_retries=1),
            simulated=True,
        )
        state = await run_graph(runtime, MockResponse())

    assert state["status"] == WorkStatus.COMPLETED
    assert state["steps"] == 6
    assert state["draft"].adapter == "reference-contract-runtime-fixture"
    assert state["review"].simulated is True
    assert all(item.audience == Audience.PUBLIC for item in state["draft"].allowed_evidence)
