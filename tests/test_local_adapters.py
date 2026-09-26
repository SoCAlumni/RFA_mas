from __future__ import annotations

import asyncio

import pytest

from rfa_mas.adapters.local import LocalRuntime, SqliteWorkRepository
from rfa_mas.contracts import (
    AgentSpec,
    Audience,
    DomainId,
    DraftBundle,
    DraftTarget,
    ResultStatus,
    RunResult,
    TaskRequest,
    TaskResult,
    WorkRequest,
    WorkStatus,
    sha256_text,
)
from rfa_mas.errors import RfaError


def _work_request() -> WorkRequest:
    return WorkRequest(
        request_id="req-store",
        trace_id="trace-store",
        run_id="run-store",
        idempotency_key="work-store",
        query="synthetic repository test",
        domain_id=DomainId.TRIV3,
        target=DraftTarget(audience=Audience.PUBLIC),
    )


def _draft(content: str = "safe draft") -> DraftBundle:
    return DraftBundle(
        request_id="req-store",
        trace_id="trace-store",
        run_id="run-store",
        agent_id="assistant-supervisor",
        domain_id=DomainId.TRIV3,
        draft_id="draft-store",
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


def _run_result(draft: DraftBundle) -> RunResult:
    return RunResult(
        request_id=draft.request_id,
        trace_id=draft.trace_id,
        run_id=draft.run_id,
        agent_id=draft.agent_id,
        domain_id=draft.domain_id,
        status=WorkStatus.COMPLETED,
        draft=draft,
        stop_reason="test",
        simulated=True,
        adapters=(),
    )


async def test_draft_version_is_immutable_but_exact_resave_is_idempotent(tmp_path) -> None:
    repository = SqliteWorkRepository(tmp_path / "store.db")
    await repository.initialize()
    await repository.create_run(_work_request())

    original = _run_result(_draft())
    await repository.save_result(original)
    await repository.save_result(original)

    with pytest.raises(RfaError) as caught:
        await repository.save_result(_run_result(_draft("changed draft")))

    assert caught.value.code == "draft_version_conflict"
    persisted = await repository.get_result("run-store")
    assert persisted is not None
    assert persisted.draft is not None
    assert persisted.draft.content == "safe draft"


def _agent_spec() -> AgentSpec:
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


def _task_request() -> TaskRequest:
    return TaskRequest(
        request_id="req-runtime",
        trace_id="trace-runtime",
        run_id="run-runtime",
        agent_id="triv3-task-supervisor",
        domain_id=DomainId.TRIV3,
        idempotency_key="runtime-key",
        task_type="domain_task",
        payload={"query": "same"},
    )


async def test_runtime_concurrent_duplicate_executes_handler_once() -> None:
    runtime = LocalRuntime(timeout_seconds=1)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handler(spec: AgentSpec, request: TaskRequest) -> TaskResult:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return TaskResult(
            request_id=request.request_id,
            trace_id=request.trace_id,
            run_id=request.run_id,
            agent_id=spec.agent_id,
            domain_id=request.domain_id,
            status=ResultStatus.SUCCEEDED,
            output={"query": request.payload["query"]},
            simulated=False,
            adapter=runtime.adapter_name,
        )

    runtime.register("domain_task", handler)
    first = asyncio.create_task(runtime.run(_agent_spec(), _task_request()))
    await started.wait()
    second = asyncio.create_task(runtime.run(_agent_spec(), _task_request()))
    await asyncio.sleep(0)

    assert await runtime.status("run-runtime") is None
    with pytest.raises(RfaError) as conflict:
        await runtime.run(
            _agent_spec(),
            _task_request().model_copy(update={"payload": {"query": "different"}}),
        )
    assert conflict.value.code == "idempotency_conflict"
    release.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert calls == 1
    assert first_result is second_result
    assert await runtime.status("run-runtime") == first_result


async def test_runtime_idempotency_rejects_changed_request_and_cancel_is_explicit() -> None:
    runtime = LocalRuntime(timeout_seconds=1)

    async def handler(spec: AgentSpec, request: TaskRequest) -> TaskResult:
        return TaskResult(
            request_id=request.request_id,
            trace_id=request.trace_id,
            run_id=request.run_id,
            agent_id=spec.agent_id,
            domain_id=request.domain_id,
            status=ResultStatus.SUCCEEDED,
            output=request.payload,
            simulated=False,
            adapter=runtime.adapter_name,
        )

    runtime.register("domain_task", handler)
    request = _task_request()
    await runtime.run(_agent_spec(), request)

    with pytest.raises(RfaError) as conflict:
        await runtime.run(
            _agent_spec(),
            request.model_copy(update={"payload": {"query": "different"}}),
        )
    assert conflict.value.code == "idempotency_conflict"

    with pytest.raises(RfaError) as cancellation:
        await runtime.cancel(request.run_id)
    assert cancellation.value.code == "not_implemented"
