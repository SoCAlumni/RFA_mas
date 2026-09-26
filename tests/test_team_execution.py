"""P0-020 team role execution: local SQLite/LocalRuntime, synthetic notes, no network/keys."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from rfa_mas.application import workers
from rfa_mas.application.workers import SupervisorBus, TeamBudgetState, TeamStop
from rfa_mas.bootstrap import build_container
from rfa_mas.contracts import (
    Audience,
    DirectWorkRequest,
    DomainId,
    DraftTarget,
    KnowledgeWrite,
    TeamBudget,
    TeamExecutionRequest,
    WorkStatus,
)
from rfa_mas.errors import RfaError
from rfa_mas.settings import Settings

CANARY = "CANARY_1ON1_Q7_TEAMTEST"
GOAL = "TRIV3 benchmark 로그 검증과 지연 비교"


def note(external_id, title, content, audience="owner"):
    return KnowledgeWrite.model_validate({
        "domain_id": "triv3",
        "provenance": {"provider": "note", "namespace": "team-test", "external_id": external_id},
        "provider_revision": "r1",
        "title": title,
        "content": content,
        "synthetic": True,
        "acl": {"audience": audience},
    })


async def make(tmp_path, **settings):
    container = build_container(Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'team.db'}",
        trace_dir=(tmp_path / "traces").resolve(),
        **settings,
    ))
    await container.startup()
    owner = await container.repository.local_principal()
    for args in (
        ("a", "합성 benchmark A 로그", "합성 benchmark A 로그. 환경 fixture-env-1, 지연 10.0ms, 정확도 81.0%"),
        ("b", "합성 benchmark B 로그", "합성 benchmark B 로그. 환경 fixture-env-1, 지연 8.2ms, 정확도 80.8%"),
        ("memo", "연구 메모 benchmark", "다른 방법의 benchmark 지연 6.0ms는 검증 전 가설이다."),
        ("paper", "공개 논문 P-A benchmark", "가상 논문 P-A는 benchmark 지연 개선 연구를 설명한다.",
         "public"),
        ("private", "개인 1:1 benchmark", f"benchmark 개인 일정 {CANARY}", "private"),
    ):
        await container.knowledge.write(note(*args), owner)
    return container, owner


def request(team=True, **values):
    return DirectWorkRequest(
        query=values.pop("query", GOAL),
        domain_id=DomainId.TRIV3,
        target=DraftTarget(audience=values.pop("audience", Audience.OWNER)),
        team=TeamExecutionRequest(goal=values.pop("goal", GOAL),
                                  outputs=values.pop("outputs", ("benchmark_report",)),
                                  requested_pattern=values.pop("pattern", None))
        if team else None,
        **values,
    )


class Spy:
    def __init__(self, inner):
        self.inner, self.calls = inner, []
        self.adapter_name, self.simulated = inner.adapter_name, inner.simulated

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def execute(self, request):
        self.calls.append(request.tool_name)
        return await self.inner.execute(request)


def rows(container, sql, *args):
    with sqlite3.connect(container.repository.path) as db:
        return db.execute(sql, args).fetchall()


async def test_benchmark_roles_run_in_order_through_supervisor_with_simulated_numbers(tmp_path):
    container, owner = await make(tmp_path)
    runner = container.team_runner
    runner.tools = spy = Spy(runner.tools)
    runner.bus = bus = SupervisorBus()
    try:
        result = await container.service.run(request(), owner)
        assert result.status == WorkStatus.COMPLETED, result.errors
        team = await container.service.team_result(result.run_id, owner)
        assert [r.role for r in team.roles] == [
            "paper_scout", "experiment_runner", "result_analyst", "supervisor"]
        assert all(r.status == "succeeded" for r in team.roles) and team.status == "completed"
        comparison = team.findings["result_analyst"]["comparisons"][0]
        assert (comparison["baseline"], comparison["candidate"]) == ("A", "B")
        assert comparison["latency_change_pct"] == -18.0
        assert comparison["accuracy_delta_pp"] == -0.2
        assert comparison["same_environment"] is True
        assert "memo" not in str(team.findings["result_analyst"]["comparisons"])
        experiment = team.findings["experiment_runner"]
        assert experiment["simulated_experiment"] is True and experiment["mode"] == "fixture_log_parse"
        assert any(r["tentative"] for r in experiment["runs"])
        assert spy.calls.count("metric_compare") == 1 and "benchmark_log_parse" in spy.calls
        assert all("supervisor" in pair for pair in bus.delivered)
        assert result.draft.simulated and "실측 아님" in result.draft.content
        assert CANARY not in result.model_dump_json() and CANARY not in team.model_dump_json()
        assert team.usage.tokens is None and all(r.input_tokens is None for r in team.roles)
        assert team.usage.max_concurrency_observed == 1
        assert team.usage.tool_calls == sum(r.tool_calls for r in team.roles)
        # Durable binding: Run -> Task -> Team, one Task, session link, stored receipts.
        record = await container.repository.get_owned_run(result.run_id, owner)
        assert record.task_id == team.task_id
        assert rows(container, "SELECT count(*) FROM product_tasks") == [(1,)]
        assert rows(container, "SELECT role, status FROM role_executions ORDER BY started_at") \
            == [(r.role, "succeeded") for r in team.roles]
        # Trace: per-role runtime + tool receipts, aliased actors, no raw text/tokens.
        ledger = await container.service.observations.ledger(result.run_id, owner)
        coverage = {c.boundary: c for c in ledger.coverage}
        assert coverage["tool"].state == "collected" and coverage["tool"].calls == len(spy.calls)
        runtime_rows = [o for o in ledger.observations if o.event.event == "runtime"]
        assert len({o.event.execution.agent_id for o in runtime_rows}) >= 4
        assert any(o.event.execution.team_id for o in ledger.observations)
        assert ledger.observations[0].event.execution.team_id is None
        raw = "".join(p.read_text() for p in (tmp_path / "traces").rglob("*.jsonl"))
        assert raw and CANARY not in raw and "10.0ms" not in raw and team.team_id not in raw
    finally:
        await container.shutdown()


async def test_research_pattern_and_plain_query_create_no_team(tmp_path):
    container, owner = await make(tmp_path)
    try:
        plain = await container.service.run(
            request(team=False, query="TRIV3 benchmark", audience=Audience.PUBLIC), owner)
        assert plain.status == WorkStatus.COMPLETED
        # Owner-target domain path reads only audiences capped by the delegated agent:
        # private notes are excluded instead of failing the draft binding.
        mine = await container.service.run(request(team=False, query="TRIV3 benchmark 로그"), owner)
        assert mine.status == WorkStatus.COMPLETED and CANARY not in mine.model_dump_json()
        assert all(e.audience != Audience.PRIVATE for e in mine.draft.allowed_evidence)
        assert rows(container, "SELECT count(*) FROM product_tasks") == [(0,)]
        research = await container.service.run(
            request(goal="TRIV3 연구 자료 조사", outputs=("research_report",),
                    pattern="research", query="TRIV3 연구 자료 조사"), owner)
        team = await container.service.team_result(research.run_id, owner)
        assert team.pattern == "research"
        assert [r.role for r in team.roles] == ["source_scout", "evidence_reviewer", "supervisor"]
        states = {r["epistemic_state"] for r in team.findings["evidence_reviewer"]["reviewed"]}
        assert states <= {"cited", "tentative"}
    finally:
        await container.shutdown()


async def test_follow_up_run_reuses_team_and_replayed_ensure_is_idempotent(tmp_path):
    container, owner = await make(tmp_path)
    try:
        first = await container.service.run(request(), owner)
        team = await container.service.team_result(first.run_id, owner)
        second = await container.service.run(request(task_id=team.task_id), owner)
        again = await container.service.team_result(second.run_id, owner)
        assert (again.task_id, again.team_id) == (team.task_id, team.team_id)
        assert rows(container, "SELECT count(*) FROM product_tasks") == [(1,)]
        # Concurrent replay of the same Run's delegation binds exactly one team.
        work = request(run_id="run_replay_same_key")
        await container.repository.create_owned_run(work, owner, session_id=None)
        graph_work = work.model_copy()
        results = await asyncio.gather(*[
            container.team_runner.ensure_and_bind(graph_work, work.team, owner, task_id=None)
            for _ in range(3)
        ])
        assert len({r.task.task_id for r in results}) == 1
        assert rows(container, "SELECT count(*) FROM product_tasks") == [(2,)]
        with pytest.raises(RfaError) as conflict:
            await container.repository.bind_run_team(
                "run_replay_same_key", owner, task_id=team.task_id, team_id=team.team_id)
        assert conflict.value.code == "team_binding_conflict"
    finally:
        await container.shutdown()


async def test_role_tool_source_memory_and_messaging_boundaries(tmp_path):
    container, owner = await make(tmp_path)
    runner = container.team_runner
    try:
        result = await container.service.run(request(), owner)
        team = await container.service.team_result(result.run_id, owner)
        lifecycle = await container.repository.get_team_lifecycle(team.task_id, owner)
        members = {m.role: m for m in lifecycle.team.spec.members}
        # Unique per-role memory namespaces scoped under domain/task/team.
        spaces = {m.spec.memory_namespace for m in members.values()}
        assert len(spaces) == len(members) and all(team.task_id in s for s in spaces)
        budget = TeamBudgetState(limits=TeamBudget())
        context = workers.RoleContext(
            run_id=result.run_id, principal=owner, work=request(), goal=GOAL,
            lifecycle=lifecycle, member=members["paper_scout"], budget=budget,
            audiences=(Audience.PUBLIC, Audience.OWNER), inputs={},
        )
        with pytest.raises(RfaError) as denied:
            await runner.tool(context, "metric_compare", {})
        assert denied.value.code == "tool_not_allowed" and budget.tool_calls == 0
        context.member = members["result_analyst"]
        with pytest.raises(RfaError):
            await runner.search(context, "benchmark")  # analyst has no source access
        with pytest.raises(RfaError) as direct:
            SupervisorBus().send("paper_scout", "experiment_runner", {})
        assert direct.value.code == "direct_message_denied"
        # Forged/unregistered member spec is refused before any handler work.
        handler = runner.handler()
        forged = members["paper_scout"].spec.model_copy(update={"memory_namespace": "other/space"})
        reply = await handler(forged, workers.TaskRequest(
            request_id="r", trace_id="t", run_id="role:x:paper_scout",
            agent_id=forged.agent_id, domain_id=DomainId.TRIV3,
            idempotency_key="role:x:paper_scout", task_type=workers.TEAM_ROLE_TASK,
            payload={"role": "paper_scout"}))
        assert reply.status.value == "denied"
        # Private notes never enter owner-target team evidence; other owners cannot bind.
        assert all(e.audience != Audience.PRIVATE for r in team.roles for e in r.evidence)
        other = owner.model_copy(update={"user_id": "someone-else"})
        with pytest.raises(RfaError) as missing:
            await container.repository.bind_run_team(
                result.run_id, other, task_id=team.task_id, team_id=team.team_id)
        assert missing.value.code == "not_found"
        with pytest.raises(RfaError):
            await container.service.team_result(result.run_id, other)
    finally:
        await container.shutdown()


async def test_shared_budget_and_required_worker_failure_stop_without_review(tmp_path, monkeypatch):
    container, owner = await make(tmp_path, max_tool_calls=3)
    submitted = []
    original = container.service._dependencies.response.submit_draft

    async def counting(*args, **kwargs):
        submitted.append(1)
        return await original(*args, **kwargs)

    monkeypatch.setattr(container.service._dependencies.response, "submit_draft", counting)
    try:
        result = await container.service.run(request(), owner)
        team = await container.service.team_result(result.run_id, owner)
        assert result.status == WorkStatus.FAILED and result.draft is None
        assert team.stop_reason == "budget_exceeded" and team.status == "partial"
        assert team.usage.tool_calls <= 3 and team.usage.limits.max_tool_calls == 3
        assert team.roles[0].status == "succeeded" and "supervisor" not in {
            r.role for r in team.roles}
        assert submitted == []
    finally:
        await container.shutdown()


async def test_required_worker_failure_keeps_partial_and_skips_review(tmp_path, monkeypatch):
    container, owner = await make(tmp_path)
    submitted = []
    original = container.service._dependencies.response.submit_draft

    async def counting(*args, **kwargs):
        submitted.append(1)
        return await original(*args, **kwargs)

    monkeypatch.setattr(container.service._dependencies.response, "submit_draft", counting)
    try:
        async def broken(runner, context):
            raise RfaError("role_failed", "fixture")

        monkeypatch.setitem(workers.ROLE_HANDLERS, "evidence_reviewer", broken)
        failed = await container.service.run(
            request(goal="TRIV3 연구 자료 조사", outputs=("research_report",), pattern="research"),
            owner)
        broken_team = await container.service.team_result(failed.run_id, owner)
        assert failed.status == WorkStatus.FAILED and broken_team.status == "partial"
        assert [r.status for r in broken_team.roles] == ["succeeded", "failed"]
        assert submitted == []
    finally:
        await container.shutdown()


def test_token_ceiling_without_reported_usage_is_unavailable_and_budget_not_reset():
    budget = TeamBudgetState(limits=TeamBudget(max_steps=2, max_tool_calls=1))
    with pytest.raises(TeamStop) as unavailable:
        budget.charge_model(usage_reported=False)
    assert unavailable.value.code == "budget_unavailable"
    budget.charge_step(); budget.charge_step(); budget.charge_tool()
    for charge in (budget.charge_step, budget.charge_tool):
        with pytest.raises(TeamStop) as exceeded:
            charge()
        assert exceeded.value.code == "budget_exceeded"
    budget.cancelled = True
    with pytest.raises(TeamStop) as stopped:
        budget.check()
    assert stopped.value.code == "cancelled"


async def test_cancel_barrier_blocks_next_role_and_tool(tmp_path):
    container, owner = await make(tmp_path)
    runner = container.team_runner
    runner.tools = spy = Spy(runner.tools)
    entered, release = asyncio.Event(), asyncio.Event()

    async def hook(kind, context):
        if context.member.role == "experiment_runner" and kind == "tool":
            entered.set()
            await release.wait()

    runner.role_hook = hook
    try:
        work = request()
        task = asyncio.create_task(container.service.run(work, owner))
        await asyncio.wait_for(entered.wait(), 10)
        tools_before = len(spy.calls)
        assert await container.service.cancel(work.run_id, owner) == "cancelling"
        release.set()
        result = await asyncio.wait_for(task, 10)
        team = await container.service.team_result(work.run_id, owner)
        assert result.status == WorkStatus.CANCELLED and result.draft is None
        assert team.status == "cancelled" and team.stop_reason == "cancelled"
        assert [r.role for r in team.roles] == ["paper_scout", "experiment_runner"]
        assert team.roles[-1].status == "cancelled"
        assert len(spy.calls) == tools_before  # No tool call after the barrier.
        status = await container.runtime.status(team.roles[-1].execution_key)
        assert status.error.code == "cancelled"
        with pytest.raises(RfaError) as terminal:
            await container.service.cancel(work.run_id, owner)
        assert terminal.value.code == "invalid_state_transition"
    finally:
        await container.shutdown()


async def test_restart_unknown_role_receipt_is_not_success_or_replayed(tmp_path):
    container, owner = await make(tmp_path)
    try:
        work = request(run_id="run_restart_unknown")
        await container.repository.create_owned_run(work, owner, session_id=None)
        lifecycle = await container.team_runner.ensure_and_bind(
            work, work.team, owner, task_id=None)
        key = f"role:{work.run_id}:paper_scout"
        member = next(m for m in lifecycle.team.spec.members if m.role == "paper_scout")
        assert (await container.repository.begin_role_execution(
            work.run_id, owner, execution_key=key, role="paper_scout",
            agent_id=member.spec.agent_id))[0] == "started"
        await container.repository.initialize()  # Simulated fresh process start.
        calls = []
        original = container.team_runner.runtime.run

        async def spy(spec, task):
            calls.append(task.task_type)
            return await original(spec, task)

        container.team_runner.runtime.run = spy
        result = await container.team_runner.execute(
            work, work.team, lifecycle, owner, (Audience.PUBLIC, Audience.OWNER))
        assert result.status == "failed" and result.stop_reason == "outcome_unknown"
        assert calls == []
        with pytest.raises(RfaError) as late:
            await container.repository.finish_role_execution(
                work.run_id, owner,
                workers.RoleOutcome(role="paper_scout", agent_id=member.spec.agent_id,
                                    execution_key=key, status="succeeded", simulated=True))
        assert late.value.code == "invalid_state_transition"
    finally:
        await container.shutdown()
