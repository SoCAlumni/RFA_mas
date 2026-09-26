"""Local state is real SQLite; runtime failures are explicit synthetic fixtures."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import SecretStr

from rfa_mas.adapters.local import LocalRuntime, SqliteWorkRepository
from rfa_mas.application.observations import ObservedPort
from rfa_mas.application.team_selector import (
    APPROVED_PINS,
    SelectionRequest,
    TeamSelector,
    TemplateRegistry,
)
from rfa_mas.application.teams import RuntimeLifecycleSupport, TeamFactory
from rfa_mas.bootstrap import build_container
from rfa_mas.contracts import (
    DomainId,
    ExecutionMode,
    MemberLifecycle,
    PersistentTask,
    TeamBudget,
    TrustedPrincipal,
    WorkRequest,
)
from rfa_mas.errors import OutcomeUnknownError, RfaError
from scripts.contract_baseline import offline_settings

OWNER = TrustedPrincipal(user_id="owner-test", authenticated=True)
OTHER = TrustedPrincipal(user_id="owner-other", authenticated=True)
CAPS = frozenset({"evidence_search", "experiment_run", "result_analysis", "evidence_review"})


def intent(**changes):
    return SelectionRequest(
        **{
            "goal": "Benchmark latency comparison",
            "domain_id": DomainId.TRIV3,
            "outputs": frozenset({"benchmark_report"}),
            **changes,
        }
    )


class Authority:
    def __init__(self):
        self.grants = {(OWNER.user_id, DomainId.TRIV3): CAPS}
        self.pins = dict(APPROVED_PINS)
        self.budget = TeamBudget()
        self.runtimes = frozenset({"local"})
        self.support = RuntimeLifecycleSupport(True, "local", ExecutionMode.LOCAL)
        self.calls = 0

    async def resolve(self, principal, domain):
        self.calls += 1
        return TeamSelector(
            TemplateRegistry.from_file(
                Path("fixtures/teams/templates.json"), approved_pins=self.pins
            ),
            grants=self.grants,
            available_capabilities=CAPS,
            available_runtimes=self.runtimes,
            budget_ceiling=self.budget,
        )


class RuntimeSpy(LocalRuntime):
    def __init__(self):
        super().__init__(timeout_seconds=1)
        self.prepare_calls = []
        self.cleanup_calls = []
        self.members = []
        self.fail_second = False
        self.cleanup_error = None
        self.prepare_error = None
        self.mutate = None
        self.cleanup_mutate = None
        self.entered = None
        self.release = None

    async def prepare(self, spec, *, idempotency_key):
        self.prepare_calls.append((spec.model_copy(deep=True), idempotency_key))
        if self.entered:
            self.entered.set()
            await self.release.wait()
        if self.prepare_error:
            raise self.prepare_error
        result = await super().prepare(spec, idempotency_key=idempotency_key)
        return self.mutate(result) if self.mutate else result

    async def _prepare_member(self, member):
        self.members.append(member.spec.agent_id)
        if self.fail_second and len(self.members) == 2:
            raise RfaError("member_prepare_failed", "synthetic no-allocation failure")
        await super()._prepare_member(member)

    async def cleanup(self, team_id, *, idempotency_key):
        self.cleanup_calls.append((team_id, idempotency_key))
        result = await super().cleanup(team_id, idempotency_key=idempotency_key)
        return self.cleanup_mutate(result) if self.cleanup_mutate else result

    async def _cleanup_member(self, member):
        if self.cleanup_error:
            raise self.cleanup_error
        await super()._cleanup_member(member)


async def setup(tmp_path, runtime=None):
    repo = SqliteWorkRepository(tmp_path / "tasks.sqlite")
    await repo.initialize()
    authority, runtime = Authority(), runtime or RuntimeSpy()
    factory = TeamFactory(repo, runtime, authority.resolve, lambda: authority.support)
    return repo, authority, runtime, factory


def rows(repo, table):
    assert table in {
        "team_slots",
        "team_members",
        "team_lifecycle_events",
        "product_tasks",
        "product_task_owners",
        "task_creation_keys",
        "rfa_schema_migrations",
    }
    with sqlite3.connect(repo.path) as db:
        return db.execute(f"SELECT * FROM {table}").fetchall()


async def test_independent_repository_race_one_slot_and_runtime_io_outside_transaction(tmp_path):
    repo, authority, runtime, factory = await setup(tmp_path)
    peer = SqliteWorkRepository(repo.path)
    await peer.initialize()
    other = TeamFactory(peer, runtime, authority.resolve, lambda: authority.support)
    runtime.entered, runtime.release = asyncio.Event(), asyncio.Event()
    first = asyncio.create_task(factory.ensure(intent(), OWNER, idempotency_key="same"))
    await asyncio.wait_for(runtime.entered.wait(), 2)
    # Second DB connection can acquire write transaction while runtime IO waits.
    waiting = await other.ensure(intent(), OWNER, idempotency_key="same")
    assert waiting.phase == "pending" and waiting.team.state == "provisioning"
    assert len(runtime.prepare_calls) == 1
    runtime.release.set()
    done = await first
    assert done.task.task_id == waiting.task.task_id and done.team.state == "ready"
    assert len(rows(repo, "team_slots")) == len(rows(repo, "product_tasks")) == 1
    assert len(rows(repo, "team_members")) == 4
    assert len(rows(repo, "team_lifecycle_events")) == 2
    assert done.trace_collection == "uncollected"


async def test_replay_content_conflicts_owner_scoped_keys_and_existing_task_reuse(tmp_path):
    repo, authority, runtime, factory = await setup(tmp_path)
    first = await factory.ensure(intent(), OWNER, idempotency_key="same")
    same = await factory.ensure(intent(), OWNER, idempotency_key="same")
    assert same == first and len(runtime.prepare_calls) == 1
    with pytest.raises(RfaError, match="동일 key") as failure:
        await factory.ensure(intent(goal="Benchmark changed"), OWNER, idempotency_key="same")
    assert failure.value.code == "idempotency_conflict"
    again = await factory.ensure(
        intent(), OWNER, task_id=first.task.task_id, idempotency_key="followup"
    )
    assert again == first and len(runtime.prepare_calls) == 1
    authority.grants[(OTHER.user_id, DomainId.TRIV3)] = CAPS
    other = await factory.ensure(intent(), OTHER, idempotency_key="same")
    assert other.task.task_id != first.task.task_id
    assert other.task.owner_id == OTHER.user_id and len(rows(repo, "team_slots")) == 2


async def test_ownership_missing_and_cross_domain_have_same_safe_error_before_runtime(tmp_path):
    repo, authority, runtime, factory = await setup(tmp_path)
    first = await factory.ensure(intent(), OWNER, idempotency_key="new")
    errors = []
    for task_id in (first.task.task_id, "missing-task"):
        with pytest.raises(RfaError) as failure:
            await factory.ensure(intent(), OTHER, task_id=task_id, idempotency_key="attacker")
        errors.append((failure.value.code, failure.value.safe_message))
    assert errors[0] == errors[1]
    with pytest.raises(RfaError) as failure:
        await factory.ensure(
            intent(domain_id=DomainId.QUANTIZATION_RESEARCH),
            OWNER,
            task_id=first.task.task_id,
            idempotency_key="domain",
        )
    assert (failure.value.code, failure.value.safe_message) == errors[0]
    assert len(runtime.prepare_calls) == 1


@pytest.mark.parametrize("revocation", ["grant", "pin", "budget", "runtime", "descriptor"])
async def test_current_authority_on_replay_same_factory_before_any_io(tmp_path, revocation):
    repo, authority, runtime, factory = await setup(tmp_path)
    first = await factory.ensure(intent(), OWNER, idempotency_key="new")
    if revocation == "grant":
        authority.grants.clear()
    elif revocation == "pin":
        authority.pins.clear()
    elif revocation == "budget":
        authority.budget = TeamBudget(max_tokens=7000)
    elif revocation == "runtime":
        authority.runtimes = frozenset()
    else:
        authority.support = RuntimeLifecycleSupport(False, "local", ExecutionMode.LOCAL)
    with pytest.raises(RfaError):
        await factory.ensure(intent(), OWNER, idempotency_key="new")
    with pytest.raises(RfaError):
        await factory.ensure(intent(), OWNER, task_id=first.task.task_id, idempotency_key="next")
    assert len(runtime.prepare_calls) == 1 and not runtime.cleanup_calls
    assert (await repo.get_team_lifecycle(first.task.task_id, OWNER)).team.state == "ready"


async def test_restart_sessions_migration_preserve_owners_and_approved_template(tmp_path):
    repo, authority, runtime, factory = await setup(tmp_path)
    authority.budget = TeamBudget(max_tokens=6000, max_tool_calls=5)
    first = await factory.ensure(intent(), OWNER, idempotency_key="create")
    template = TemplateRegistry.builtin().definitions()[0][0].template
    assert first.team.spec.template == template
    assert first.team.spec.template.budget.max_tokens == 16000
    assert first.team.spec.execution_budget.max_tokens == 6000
    assert runtime.prepare_calls[0][0].execution_budget == first.team.spec.execution_budget
    for _ in range(2):
        session = await repo.create_session(OWNER)
        request = WorkRequest(query="followup", domain_id=DomainId.TRIV3)
        await repo.create_owned_run(
            request, OWNER, session_id=session.session_id, task_id=first.task.task_id
        )
        assert (await repo.get_owned_run(request.run_id, OWNER)).task_id == first.task.task_id
    old_identity = await repo.local_principal()
    await asyncio.gather(repo.initialize(), SqliteWorkRepository(repo.path).initialize())
    assert await repo.local_principal() == old_identity
    assert [r[0] for r in rows(repo, "rfa_schema_migrations")] == [1, 2, 3, 4, 5]
    reopened = SqliteWorkRepository(repo.path)
    new_runtime = RuntimeSpy()
    new_factory = TeamFactory(reopened, new_runtime, authority.resolve, lambda: authority.support)
    resumed = await new_factory.ensure(
        intent(), OWNER, task_id=first.task.task_id, idempotency_key="new-run"
    )
    assert resumed == first and not new_runtime.prepare_calls
    assert len(await reopened.list_sessions(OWNER)) == 2


async def test_registry_only_task_goal_not_invented_or_reassigned(tmp_path):
    repo, _, runtime, factory = await setup(tmp_path)
    old = PersistentTask(
        task_id="old-task", owner_id=OWNER.user_id, domain_id="triv3", goal="unknown"
    )
    await repo.register_task_owner(old)
    with pytest.raises(RfaError) as failure:
        await factory.ensure(intent(), OWNER, task_id=old.task_id, idempotency_key="legacy")
    assert failure.value.code == "task_definition_missing"
    assert rows(repo, "product_task_owners") == [("old-task", OWNER.user_id, "triv3")]
    assert not rows(repo, "product_tasks") and not runtime.prepare_calls


async def test_two_repositories_reuse_existing_task_with_distinct_keys(tmp_path):
    repo, authority, runtime, factory = await setup(tmp_path)
    first = await factory.ensure(intent(), OWNER, idempotency_key="first")
    other = TeamFactory(
        SqliteWorkRepository(repo.path), runtime, authority.resolve, lambda: authority.support
    )
    results = await asyncio.gather(
        factory.ensure(intent(), OWNER, task_id=first.task.task_id, idempotency_key="run-a"),
        other.ensure(intent(), OWNER, task_id=first.task.task_id, idempotency_key="run-b"),
    )
    assert results == [first, first]
    assert len(runtime.prepare_calls) == len(rows(repo, "team_slots")) == 1


async def test_local_runtime_operation_keys_and_successful_cleanup(tmp_path):
    repo, _, runtime, factory = await setup(tmp_path)
    first = await factory.ensure(intent(), OWNER, idempotency_key="first")
    assert len(runtime._prepared_agents) == 4
    replay = await runtime.prepare(first.team.spec, idempotency_key=first.operation_key)
    assert replay == first.team and len(runtime.members) == 4
    with pytest.raises(RfaError) as failure:
        await runtime.prepare(
            first.team.spec.model_copy(update={"owner_id": "foreign"}),
            idempotency_key=first.operation_key,
        )
    assert failure.value.code == "idempotency_conflict"
    cleaned = await factory.cleanup(first.task.task_id, OWNER)
    assert cleaned.team.state == "cleaned" and not runtime._prepared_agents
    assert all(m.cleanup == "cleaned" for m in cleaned.team.member_states)
    assert cleaned.team.spec == first.team.spec and not cleaned.team.failed_agent_ids
    with pytest.raises(RfaError):
        await runtime.cleanup("foreign-team", idempotency_key=cleaned.operation_key)
    assert len(rows(repo, "team_lifecycle_events")) == 4


async def test_research_pattern_is_a_distinct_three_member_team(tmp_path):
    _, _, runtime, factory = await setup(tmp_path)
    research = intent(goal="Research literature review", outputs={"research_report"})
    result = await factory.ensure(research, OWNER, idempotency_key="research")
    assert result.team.state == "ready"
    assert [m.role for m in result.team.spec.members] == [
        "supervisor",
        "source_scout",
        "evidence_reviewer",
    ]
    assert len(runtime.members) == 3
    assert result.team.spec.members[0].spec.capabilities == ()
    assert all(not m.tool_names and not m.source_ids for m in result.team.spec.members)


@pytest.mark.parametrize(
    "bad",
    [
        "owner",
        "team",
        "task",
        "domain",
        "mode",
        "digest",
        "budget",
        "member",
        "incomplete",
        "failed_ids",
        "bool_budget",
        "role_capability",
    ],
)
async def test_runtime_return_exact_binding_and_never_foreign_cleanup(tmp_path, bad):
    repo, _, runtime, factory = await setup(tmp_path)

    def change(result):
        spec = result.spec
        if bad in {"owner", "team", "task", "domain"}:
            fields = {
                "owner": "owner_id",
                "team": "team_id",
                "task": "task_id",
                "domain": "domain_id",
            }
            return result.model_copy(
                update={"spec": spec.model_copy(update={fields[bad]: "foreign"})}
            )
        if bad == "mode":
            return result.model_copy(update={"mode": ExecutionMode.MOCK})
        if bad == "digest":
            return result.model_copy(
                update={"spec": spec.model_copy(update={"definition_digest": "0" * 64})}
            )
        if bad in {"budget", "bool_budget"}:
            budget = spec.execution_budget.model_copy(
                update={"max_tokens": True if bad == "bool_budget" else 7000}
            )
            return result.model_copy(
                update={"spec": spec.model_copy(update={"execution_budget": budget})}
            )
        if bad == "role_capability":
            member = spec.members[1].model_copy(
                update={
                    "spec": spec.members[1].spec.model_copy(update={"capabilities": ("publish",)})
                }
            )
            return result.model_copy(
                update={
                    "spec": spec.model_copy(
                        update={"members": (spec.members[0], member, *spec.members[2:])}
                    )
                }
            )
        if bad == "failed_ids":
            return result.model_copy(update={"failed_agent_ids": (spec.members[0].spec.agent_id,)})
        if bad == "member":
            return result.model_copy(
                update={
                    "member_states": (
                        MemberLifecycle(agent_id="foreign", prepare="prepared"),
                        *result.member_states[1:],
                    )
                }
            )
        return result.model_copy(update={"member_states": ()})

    runtime.mutate = change
    result = await factory.ensure(intent(), OWNER, idempotency_key="malformed")
    assert result.reason == "invalid_contract" and result.team.state == "unknown"
    assert result.team.spec.owner_id == OWNER.user_id
    assert all(m.prepare == "unknown" for m in result.team.member_states)
    assert not runtime.cleanup_calls and len(rows(repo, "team_slots")) == 1
    replay = await factory.ensure(intent(), OWNER, idempotency_key="malformed")
    assert replay == result and len(runtime.prepare_calls) == 1


@pytest.mark.parametrize(
    "cleanup_error,expected",
    [
        (None, "cleaned"),
        (RfaError("member_cleanup_failed", "synthetic"), "failed"),
        (TimeoutError("synthetic"), "unknown"),
    ],
)
async def test_partial_prepare_cleanup_targets_and_failed_unknown_slots_stay_occupied(
    tmp_path, cleanup_error, expected
):
    repo, _, runtime, factory = await setup(tmp_path)
    runtime.fail_second, runtime.cleanup_error = True, cleanup_error
    result = await factory.ensure(intent(), OWNER, idempotency_key="partial")
    assert result.team.state == expected and result.operation == "cleanup"
    assert [m.prepare for m in result.team.member_states] == [
        "prepared",
        "failed",
        "not_started",
        "not_started",
    ]
    assert all(m.cleanup == "not_requested" for m in result.team.member_states[1:])
    assert runtime.cleanup_calls == [(result.team.spec.team_id, result.operation_key)]
    replay = await factory.ensure(
        intent(), OWNER, task_id=result.task.task_id, idempotency_key="another"
    )
    assert replay == result and len(runtime.prepare_calls) == 1
    assert await factory.cleanup(result.task.task_id, OWNER) == result
    assert len(runtime.cleanup_calls) == 1 and len(rows(repo, "team_slots")) == 1
    events = [json.loads(r[3]) for r in rows(repo, "team_lifecycle_events")]
    assert [r["reason"] for r in events] == [
        "reserved",
        "partial_failure",
        "cleanup_requested",
        result.reason,
    ]


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("PRIVATE_CANARY"),
        OutcomeUnknownError("PRIVATE_CANARY"),
        RuntimeError("PRIVATE_CANARY"),
    ],
)
async def test_timeout_unknown_and_raw_failure_not_retried_or_reflected(tmp_path, error):
    repo, _, runtime, factory = await setup(tmp_path)
    runtime.prepare_error = error
    result = await factory.ensure(intent(), OWNER, idempotency_key="unknown")
    assert result.team.state == "unknown" and result.reason == "outcome_unknown"
    assert "PRIVATE_CANARY" not in result.model_dump_json()
    assert not runtime.cleanup_calls
    assert await factory.ensure(intent(), OWNER, idempotency_key="unknown") == result
    assert len(runtime.prepare_calls) == 1 and len(rows(repo, "team_slots")) == 1


async def test_pending_concurrent_cleanup_blocked_cancel_and_late_response_fenced(tmp_path):
    repo, _, runtime, factory = await setup(tmp_path)
    runtime.entered, runtime.release = asyncio.Event(), asyncio.Event()
    pending = asyncio.create_task(factory.ensure(intent(), OWNER, idempotency_key="cancel"))
    await asyncio.wait_for(runtime.entered.wait(), 2)
    record = await factory.ensure(intent(), OWNER, idempotency_key="cancel")
    with pytest.raises(RfaError) as error:
        await factory.cleanup(record.task.task_id, OWNER)
    assert error.value.code == "team_busy" and not runtime.cleanup_calls
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    unknown = await repo.get_team_lifecycle(record.task.task_id, OWNER)
    assert unknown.team.state == "unknown"
    cleaned = await factory.cleanup(record.task.task_id, OWNER)
    assert cleaned.generation == 2 and cleaned.team.state == "unknown"
    with pytest.raises(RfaError) as error:
        await repo.finish_team_operation(
            record.task.task_id, OWNER, generation=1, instance=unknown.team, reason="ready"
        )
    assert error.value.code == "stale_team_operation"
    assert (await repo.get_team_lifecycle(record.task.task_id, OWNER)) == cleaned


@pytest.mark.parametrize("change", ["owner", "runtime_ref", "missing_ref"])
async def test_invalid_cleanup_echo_cannot_change_original_spec(tmp_path, change):
    repo, _, runtime, factory = await setup(tmp_path)
    first = await factory.ensure(intent(), OWNER, idempotency_key="initial")
    runtime.cleanup_mutate = lambda r: r.model_copy(
        update=(
            {"spec": r.spec.model_copy(update={"owner_id": "foreign"})}
            if change == "owner"
            else {"runtime_ref": "foreign-runtime" if change == "runtime_ref" else None}
        )
    )
    result = await factory.cleanup(first.task.task_id, OWNER)
    assert result.reason == "invalid_contract" and result.team.spec == first.team.spec
    assert result.team.runtime_ref == first.team.runtime_ref
    assert result.team.state == "unknown"
    assert runtime.cleanup_calls[0][0] == first.team.spec.team_id


async def test_mutated_runtime_data_not_exposed_by_serializer_warnings(tmp_path, capsys, caplog):
    repo, _, runtime, factory = await setup(tmp_path)
    runtime.mutate = lambda r: r.model_copy(update={"mode": {"PRIVATE_CANARY": "invalid"}})
    result = await factory.ensure(intent(), OWNER, idempotency_key="malformed-mode")
    assert result.reason == "invalid_contract"
    captured = capsys.readouterr()
    assert "PRIVATE_CANARY" not in captured.out + captured.err + caplog.text
    assert "PRIVATE_CANARY" not in str(rows(repo, "team_lifecycle_events"))


async def test_caller_modified_budget_and_decision_are_not_authority(tmp_path):
    repo, _, runtime, factory = await setup(tmp_path)
    bad = intent().model_copy(
        update={"budget": TeamBudget().model_copy(update={"max_steps": True})}
    )
    with pytest.raises(RfaError):
        await factory.ensure(bad, OWNER, idempotency_key="bad")
    with pytest.raises(TypeError):
        await factory.ensure(intent(), OWNER, idempotency_key="bad", decision="selected")
    with pytest.raises(RfaError):
        await factory.ensure(
            intent(), OWNER.model_copy(update={"authenticated": "yes"}), idempotency_key="bad"
        )
    assert not runtime.prepare_calls and not rows(repo, "team_slots")


async def test_bootstrap_actual_runtime_support_not_decorator_and_graph_observation_preserved(
    tmp_path,
):
    local = build_container(offline_settings(tmp_path / "local"))
    await local.startup()
    try:
        owner = await local.repository.local_principal()
        assert local.team_factory.runtime is local.runtime
        assert isinstance(local.service._dependencies.runtime, ObservedPort)
        result = await local.team_factory.ensure(intent(), owner, idempotency_key="local")
        assert result.team.state == "ready" and result.trace_collection == "uncollected"
        assert len(rows(local.repository, "team_lifecycle_events")) == 2
        assert not list(local.settings.trace_dir.glob("**/events-*.jsonl"))
    finally:
        await local.shutdown()
    config = offline_settings(tmp_path / "http").model_copy(
        update={
            "runtime_backend": "http",
            "runtime_base_url": "http://127.0.0.1:8013",
            "runtime_api_token": SecretStr("synthetic-test-only"),
        }
    )
    http = build_container(config)
    await http.startup()
    try:
        assert hasattr(http.service._dependencies.runtime, "prepare")
        owner = await http.repository.local_principal()
        with pytest.raises(RfaError) as failure:
            await http.team_factory.ensure(intent(), owner, idempotency_key="unsupported")
        assert failure.value.code == "not_implemented"
        assert not rows(http.repository, "team_slots")
    finally:
        await http.shutdown()
