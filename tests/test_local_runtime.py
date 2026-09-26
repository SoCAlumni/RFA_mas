"""P1-008D: local runtime stand-in (TeamSpec lifecycle + synthetic handlers + journal).

Synthetic fixtures only, tmp_path DBs, in-process ASGI transport, and an autouse
guard that fails on any socket connect or subprocess spawn.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import socket
import stat
import subprocess
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from rfa_mas.adapters.http import ReferenceHttpClient, RuntimeHttpAdapter
from rfa_mas.contracts import (
    AgentSpec,
    Audience,
    DomainId,
    DraftBundle,
    DraftTarget,
    ResultStatus,
    TaskRequest,
    TeamBudget,
    TeamMember,
    TeamSpec,
    TeamTemplate,
    TrustedPrincipal,
    WorkRequest,
)
from rfa_mas.reference.local_runtime import (
    DEFAULT_HANDLERS,
    LocalRuntimePolicy,
    SyntheticHandler,
    create_local_runtime_app,
)
from rfa_mas.reference.local_runtime_store import (
    LocalMemberPrepareError,
    identity_of,
    task_result,
)
from rfa_mas.reference.local_security import LocalServiceBoundary

HOST = "127.0.0.1:8782"
BASE = f"http://{HOST}"
OWNER = "installation-owner-1"
CANARY = "PRIVATE-CANARY-51be0a"
ROLE_CAPS = {
    "supervisor": (),
    "paper_scout": ("evidence_search",),
    "source_scout": ("evidence_search",),
    "experiment_runner": ("experiment_run",),
    "result_analyst": ("result_analysis",),
    "evidence_reviewer": ("evidence_review",),
}


@pytest.fixture(autouse=True)
def no_external_effects(monkeypatch):
    attempts: list[str] = []

    def blocked(*args, **kwargs):
        attempts.append("external")
        raise AssertionError("external network/process effect attempted")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(subprocess, "Popen", blocked)
    monkeypatch.setattr(os, "system", blocked)
    yield attempts
    assert attempts == []


class Gate:
    """Server-registered blocking synthetic handler used to hold a run in flight."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.completed = 0

    async def run(self, spec: AgentSpec, request: TaskRequest):
        self.calls += 1
        self.started.set()
        await self.release.wait()
        self.completed += 1
        return task_result(
            identity_of(request), ResultStatus.SUCCEEDED, output={"steps": 1, "synthetic": True}
        )


class Counting:
    def __init__(self, fail_agents: dict[str, type[Exception]] | None = None) -> None:
        self.calls: list[str] = []
        self.fail_agents = dict(fail_agents or {})

    def __call__(self, member: TeamMember) -> None:
        self.calls.append(member.spec.agent_id)
        error = self.fail_agents.pop(member.spec.agent_id, None)
        if error is not None:
            raise error("synthetic member failure")


@pytest.fixture
def token() -> SecretStr:
    return SecretStr(secrets.token_urlsafe(32))


@pytest.fixture
def boundary(token) -> LocalServiceBoundary:
    return LocalServiceBoundary.create(owner_id=OWNER, service_token=token, allowed_hosts=[HOST])


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "local-runtime" / "runtime.db"


@pytest.fixture
def gate() -> Gate:
    return Gate()


@pytest.fixture
def make_app(db_path, boundary, gate):
    def factory(**kwargs):
        kwargs.setdefault(
            "handlers",
            {**DEFAULT_HANDLERS, "synthetic.gate": SyntheticHandler(gate.run, None)},
        )
        kwargs.setdefault("boundary", boundary)
        return create_local_runtime_app(
            db_path=db_path, clock=lambda: datetime(2026, 9, 26, tzinfo=UTC), **kwargs
        )

    return factory


def client(app, token: SecretStr | None, *, peer=("127.0.0.1", 50124), base=BASE, **kwargs):
    headers = {"Authorization": f"Bearer {token.get_secret_value()}"} if token else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=peer),
        base_url=base,
        headers=headers,
        **kwargs,
    )


def team_spec(
    team_id="team_research_1",
    *,
    pattern="research",
    domain=DomainId.QUANTIZATION_RESEARCH,
    owner=OWNER,
    runtime_kind="local",
    extra_capability: str | None = None,
    tools=("synthetic.glossary_lookup",),
    member_steps=10,
    digest: str | None = "d" * 64,
) -> TeamSpec:
    roles = (
        ("supervisor", "source_scout", "evidence_reviewer")
        if pattern == "research"
        else ("supervisor", "paper_scout", "experiment_runner", "result_analyst")
    )
    required = tuple(sorted({c for r in roles for c in ROLE_CAPS[r]}))
    return TeamSpec(
        task_id=f"task-{team_id}",
        team_id=team_id,
        domain_id=domain,
        owner_id=owner,
        template=TeamTemplate(
            template_id=f"tmpl-{pattern}",
            version="v1",
            pattern=pattern,
            approved=True,
            required_capabilities=required + ((extra_capability,) if extra_capability else ()),
            runtime_kind=runtime_kind,
            budget=TeamBudget(),
        ),
        members=tuple(
            TeamMember(
                role=role,
                spec=AgentSpec(
                    agent_id=f"{team_id}:{role}",
                    domain_id=domain,
                    memory_namespace=f"teams/{team_id}/{role}",
                    capabilities=ROLE_CAPS[role],
                    allowed_audiences=(Audience.OWNER, Audience.PUBLIC),
                    instructions_ref=f"approved:{role}",
                    max_steps=member_steps,
                    max_tool_calls=2,
                ),
                tool_names=tools if role.endswith("scout") else (),
            )
            for role in roles
        ),
        definition_digest=digest,
        execution_budget=TeamBudget(max_steps=20, max_tool_calls=5) if digest else None,
    )


def member(spec: TeamSpec, role: str) -> AgentSpec:
    return next(m.spec for m in spec.members if m.role == role)


def task(
    spec: AgentSpec, *, run_id="run-1", key=None, task_type="synthetic.role_note", payload=None
):
    return TaskRequest(
        request_id=f"req-{run_id}",
        trace_id=f"trace-{run_id}",
        run_id=run_id,
        agent_id=spec.agent_id,
        domain_id=spec.domain_id,
        idempotency_key=key or f"{run_id}:key",
        task_type=task_type,
        payload={} if payload is None else payload,
    )


def domain_supervisor(domain=DomainId.TRIV3, *, caps=("evidence_search", "draft_generation")):
    return AgentSpec(
        agent_id=f"domain-supervisor:{domain.value}",
        domain_id=domain,
        memory_namespace=f"domain/{domain.value}",
        capabilities=caps,
        allowed_audiences=(Audience.OWNER, Audience.PUBLIC),
        instructions_ref=f"domain://{domain.value}/v1",
        max_steps=6,
        max_tool_calls=2,
    )


def domain_request(spec: AgentSpec, *, run_id="run-domain-1", principal_user=OWNER):
    work = WorkRequest(
        request_id=f"req-{run_id}",
        trace_id=f"trace-{run_id}",
        run_id=run_id,
        domain_id=spec.domain_id,
        query="합성 질의",
        target=DraftTarget(audience=Audience.PUBLIC),
    )
    principal = TrustedPrincipal(user_id=principal_user, authenticated=True)
    return task(
        spec,
        run_id=run_id,
        task_type="domain_task",
        payload={
            "work_request": work.model_dump(mode="json"),
            "principal": principal.model_dump(mode="json"),
        },
    ), work


async def prepare(api, spec: TeamSpec, key="prepare-1"):
    return await api.post(
        "/v1/runtime/teams",
        json={"spec": spec.model_dump(mode="json")},
        headers={"Idempotency-Key": key},
    )


async def run(api, spec: AgentSpec, request: TaskRequest):
    return await api.post(
        "/v1/runtime/tasks",
        json={"spec": spec.model_dump(mode="json"), "request": request.model_dump(mode="json")},
        headers={"Idempotency-Key": request.idempotency_key},
    )


# ---------------------------------------------------------------- AC1
async def test_ac1_prepare_run_status_cancel_cleanup_lifecycle(make_app, token):
    app = make_app()
    spec = team_spec()
    scout = member(spec, "source_scout")
    async with client(app, token) as api:
        ready = (await prepare(api, spec)).json()
        assert ready["state"] == "ready" and ready["mode"] == "local"
        assert ready["sandbox_id"] is None
        assert ready["runtime_ref"] == f"local-reference:{spec.team_id}"
        assert [m["prepare"] for m in ready["member_states"]] == ["prepared"] * 3
        assert (await api.get(f"/v1/runtime/teams/{spec.team_id}")).json() == ready

        request = task(scout, run_id="run-scout-1")
        result = (await run(api, scout, request)).json()
        assert result["status"] == "succeeded" and result["simulated"] is True
        assert result["adapter"] == "local-reference-runtime"
        assert result["output"]["synthetic"] is True
        assert (await api.get("/v1/runtime/tasks/run-scout-1")).json() == result
        cancel_done = await api.post("/v1/runtime/tasks/run-scout-1/cancel")
        assert cancel_done.json() == result

        cleaned = (
            await api.post(
                f"/v1/runtime/teams/{spec.team_id}/cleanup", headers={"Idempotency-Key": "c-1"}
            )
        ).json()
        assert cleaned["state"] == "cleaned" and cleaned["sandbox_id"] is None
        assert [m["cleanup"] for m in cleaned["member_states"]] == ["cleaned"] * 3
        after = (await run(api, scout, task(scout, run_id="run-scout-2"))).json()
        assert after["status"] == "denied" and after["error"]["code"] == "team_not_ready"


@pytest.mark.parametrize(
    ("mutation", "status", "code"),
    [
        ({"runtime_kind": "openshell"}, 501, "openshell_not_supported"),
        ({"extra_capability": "shell_exec"}, 403, "capability_not_supported"),
        ({"tools": ("shell.exec",)}, 403, "tool_not_allowed"),
        ({"tools": ("http.get",)}, 403, "tool_not_allowed"),
        ({"owner": "someone-else"}, 403, "owner_mismatch"),
        ({"digest": None}, 422, "team_spec_incomplete"),
        ({"member_steps": 50}, 403, "budget_exceeded"),
    ],
)
async def test_ac1_prepare_rejects_unsupported_runtime_capability_tool_owner(
    make_app, token, mutation, status, code
):
    app = make_app()
    async with client(app, token) as api:
        response = await prepare(api, team_spec(**mutation))
    assert response.status_code == status and response.json()["code"] == code
    assert app.state.local_runtime_store.counts()["teams"] == 0


async def test_ac1_prepare_enforces_server_role_and_domain_allowlists(make_app, token):
    app = make_app(
        policy=LocalRuntimePolicy(
            allowed_domains=frozenset({DomainId.TRIV3}),
            allowed_roles=frozenset({"supervisor", "source_scout", "evidence_reviewer"}),
        )
    )
    async with client(app, token) as api:
        domain = await prepare(api, team_spec(), key="p-domain")
        assert domain.status_code == 403 and domain.json()["code"] == "domain_not_allowed"
        role = await prepare(
            api, team_spec("team_bench", pattern="benchmark", domain=DomainId.TRIV3), key="p-role"
        )
        assert role.status_code == 403 and role.json()["code"] == "role_not_allowed"
    assert app.state.local_runtime_store.counts()["teams"] == 0


async def test_ac1_run_rejects_unregistered_handler_capability_identity_and_escalation(
    make_app, token, gate
):
    calls: list[str] = []

    async def counted(spec, request):
        calls.append(request.task_type)
        return await DEFAULT_HANDLERS["domain_task"].run(spec, request)

    app = make_app(handlers={"domain_task": SyntheticHandler(counted, "draft_generation")})
    team = team_spec()
    scout = member(team, "source_scout")
    supervisor = domain_supervisor()
    async with client(app, token) as api:
        await prepare(api, team)
        cases = []
        cases.append(
            await run(
                api,
                supervisor,
                task(
                    supervisor, run_id="r-python", task_type="python.exec", payload={"code": CANARY}
                ),
            )
        )
        cases.append(
            await run(
                api,
                supervisor,
                task(supervisor, run_id="r-shell", task_type="shell", payload={"cmd": CANARY}),
            )
        )
        shell_caps = domain_supervisor(caps=("draft_generation", "shell_exec"))
        cases.append(await run(api, shell_caps, domain_request(shell_caps, run_id="r-caps")[0]))
        no_draft = domain_supervisor(caps=("evidence_search",))
        cases.append(await run(api, no_draft, domain_request(no_draft, run_id="r-nodraft")[0]))
        forged, _ = domain_request(supervisor, run_id="r-forged", principal_user="attacker")
        cases.append(await run(api, supervisor, forged))
        escalated = scout.model_copy(
            update={"capabilities": ("evidence_search", "draft_generation")}
        )
        cases.append(
            await run(api, escalated, task(escalated, run_id="r-escalate", task_type="domain_task"))
        )
        mismatch = task(supervisor, run_id="r-mismatch").model_copy(
            update={"agent_id": "someone-else"}
        )
        cases.append(await run(api, supervisor, mismatch))
    codes = [(r.json()["status"], r.json()["error"]["code"]) for r in cases]
    assert codes == [
        ("denied", "handler_not_allowed"),
        ("denied", "handler_not_allowed"),
        ("denied", "capability_not_supported"),
        ("denied", "capability_denied"),
        ("denied", "identity_mismatch"),
        ("denied", "agent_spec_mismatch"),
        ("failed", "agent_spec_binding_mismatch"),
    ]
    assert all(CANARY not in r.text for r in cases)
    assert calls == [] and gate.calls == 0


# ---------------------------------------------------------------- AC2
async def test_ac2_concurrent_same_key_prepare_and_run_have_no_duplicates(make_app, token, gate):
    hook = Counting()
    app = make_app(prepare_member=hook)
    store = app.state.local_runtime_store
    spec = team_spec()
    scout = member(spec, "source_scout")
    async with client(app, token) as api:
        same_key = await asyncio.gather(*(prepare(api, spec, key="prep-same") for _ in range(10)))
        assert {r.status_code for r in same_key} == {200} and len({r.text for r in same_key}) == 1
        other_keys = await asyncio.gather(*(prepare(api, spec, key=f"prep-{n}") for n in range(10)))
        assert {r.json()["state"] for r in other_keys} == {"ready"}
        assert hook.calls == [m.spec.agent_id for m in spec.members]
        changed = team_spec(tools=("synthetic.text_stats",))
        conflict = await prepare(api, changed, key="prep-same")
        assert conflict.status_code == 409 and conflict.json()["code"] == "idempotency_conflict"
        respec = await prepare(api, changed, key="prep-changed")
        assert respec.status_code == 409 and respec.json()["code"] == "team_spec_conflict"
        assert store.counts()["teams"] == 1

        request = task(scout, run_id="run-gate", task_type="synthetic.gate")
        pending = [asyncio.create_task(run(api, scout, request)) for _ in range(10)]
        await gate.started.wait()
        await asyncio.sleep(0.05)
        gate.release.set()
        results = await asyncio.gather(*pending)
        assert {r.status_code for r in results} == {200} and len({r.text for r in results}) == 1
        assert gate.calls == 1 and gate.completed == 1
        changed_payload = request.model_copy(update={"payload": {"x": 1}})
        conflict = await run(api, scout, changed_payload)
        assert conflict.status_code == 409 and conflict.json()["code"] == "idempotency_conflict"
    assert store.counts()["runs"] == 1


async def test_ac2_partial_prepare_failure_and_unknown_are_recoverable(make_app, token):
    bench = team_spec("team_bench_1", pattern="benchmark", domain=DomainId.TRIV3)
    runner = f"{bench.team_id}:experiment_runner"
    unsure = team_spec("team_unknown_1")
    unsure_scout = f"{unsure.team_id}:source_scout"
    hook = Counting({runner: LocalMemberPrepareError, unsure_scout: RuntimeError})
    app = make_app(prepare_member=hook)
    async with client(app, token) as api:
        failed = (await prepare(api, bench, key="bench-1")).json()
        assert failed["state"] == "failed" and failed["failed_agent_ids"] == [runner]
        assert [m["prepare"] for m in failed["member_states"]] == [
            "prepared",
            "prepared",
            "failed",
            "not_started",
        ]
        scout = member(bench, "paper_scout")
        blocked = (await run(api, scout, task(scout, run_id="run-bench-early"))).json()
        assert blocked["error"]["code"] == "team_not_ready"
        retried = (await prepare(api, bench, key="bench-2")).json()
        assert retried["state"] == "ready" and retried["failed_agent_ids"] == []
        assert hook.calls.count(f"{bench.team_id}:supervisor") == 1
        assert hook.calls.count(runner) == 2

        unknown = (await prepare(api, unsure, key="unsure-1")).json()
        assert unknown["state"] == "unknown"
        refused = await prepare(api, unsure, key="unsure-2")
        assert refused.status_code == 409 and refused.json()["code"] == "team_recovery_required"
        cleaned = (
            await api.post(
                f"/v1/runtime/teams/{unsure.team_id}/cleanup",
                headers={"Idempotency-Key": "unsure-clean"},
            )
        ).json()
        assert cleaned["state"] == "cleaned"
        assert (await prepare(api, unsure, key="unsure-3")).json()["state"] == "ready"


async def test_ac2_cancel_is_durable_and_cleanup_waits_for_running_tasks(make_app, token, gate):
    app = make_app()
    spec = team_spec()
    scout = member(spec, "source_scout")
    async with client(app, token) as api:
        await prepare(api, spec)
        request = task(scout, run_id="run-cancel", task_type="synthetic.gate")
        pending = asyncio.create_task(run(api, scout, request))
        await gate.started.wait()
        assert (await api.get("/v1/runtime/tasks/run-cancel")).status_code == 404
        journal = (await api.get("/v1/runtime/tasks/run-cancel/journal")).json()
        assert journal["state"] == "running" and journal["auto_replay"] is False
        busy = await api.post(
            f"/v1/runtime/teams/{spec.team_id}/cleanup", headers={"Idempotency-Key": "busy"}
        )
        assert busy.status_code == 409 and busy.json()["code"] == "team_busy"

        cancelled = (await api.post("/v1/runtime/tasks/run-cancel/cancel")).json()
        assert cancelled["status"] == "failed" and cancelled["error"]["code"] == "cancelled"
        assert (await pending).json() == cancelled
        gate.release.set()
        await asyncio.sleep(0.01)
        assert gate.completed == 0
        assert (await api.get("/v1/runtime/tasks/run-cancel")).json() == cancelled
        assert (await run(api, scout, request)).json() == cancelled
        assert (await api.post("/v1/runtime/tasks/run-cancel/cancel")).json() == cancelled
        cleaned = await api.post(
            f"/v1/runtime/teams/{spec.team_id}/cleanup", headers={"Idempotency-Key": "clean"}
        )
        assert cleaned.json()["state"] == "cleaned"


# ---------------------------------------------------------------- AC3
async def test_ac3_restart_marks_inflight_unknown_and_never_replays(make_app, token, gate, db_path):
    first = make_app()
    spec = team_spec()
    scout = member(spec, "source_scout")
    async with client(first, token) as api:
        await prepare(api, spec)
        done = (await run(api, scout, task(scout, run_id="run-done"))).json()
        request = task(scout, run_id="run-inflight", task_type="synthetic.gate")
        pending = asyncio.create_task(run(api, scout, request))
        await gate.started.wait()

        restarted = make_app()
        assert restarted.state.recovered_runs == 1
        async with client(restarted, token) as api2:
            unknown = (await api2.get("/v1/runtime/tasks/run-inflight")).json()
            assert unknown["status"] == "outcome_unknown"
            assert unknown["error"]["code"] == "outcome_unknown"
            assert unknown["output"] == {"recovery": "manual_query_required", "auto_replay": False}
            journal = (await api2.get("/v1/runtime/tasks/run-inflight/journal")).json()
            assert journal["state"] == "unknown" and journal["recovered_after_restart"] is True
            assert (await run(api2, scout, request)).json() == unknown
            assert (await api2.post("/v1/runtime/tasks/run-inflight/cancel")).json() == unknown
            assert (await api2.get("/v1/runtime/tasks/run-done")).json() == done
            team = (await api2.get(f"/v1/runtime/teams/{spec.team_id}")).json()
            assert team["state"] == "ready" and team["sandbox_id"] is None
        assert gate.calls == 1

        # The old process finishing late cannot overwrite the recovery state.
        gate.release.set()
        assert (await pending).json() == unknown
    assert gate.calls == 1
    third = make_app()
    assert third.state.recovered_runs == 0
    assert third.state.local_runtime_store.counts()["runs"] == 2
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(db_path.parent.stat().st_mode) == 0o700


async def test_ac3_runtime_http_adapter_run_status_cancel_are_simulated_local(make_app, token):
    app = make_app()
    supervisor = domain_supervisor()
    request, work = domain_request(supervisor)
    async with client(app, None) as http:
        adapter = RuntimeHttpAdapter(ReferenceHttpClient(http, token=token, max_read_retries=1))
        result = await adapter.run(supervisor, request)
        assert result.status == ResultStatus.SUCCEEDED and result.simulated is True
        assert result.output["steps"] == 3
        draft = DraftBundle.model_validate(result.output["draft"])
        assert (
            draft.request_id,
            draft.trace_id,
            draft.run_id,
            draft.agent_id,
            draft.domain_id,
        ) == (
            request.request_id,
            request.trace_id,
            request.run_id,
            request.agent_id,
            request.domain_id,
        )
        assert draft.target == work.target and draft.simulated is True
        assert all(item.audience == Audience.PUBLIC for item in draft.allowed_evidence)
        assert (await adapter.status(request.run_id)).output == result.output
        assert await adapter.status("run-missing") is None
        assert (await adapter.cancel(request.run_id)).status == ResultStatus.SUCCEEDED
        replay = await adapter.run(supervisor, request)
        assert replay.output == result.output


# ---------------------------------------------------------------- AC4
async def test_ac4_boundary_rejects_forged_token_host_origin_forwarded(make_app, token):
    app = make_app()
    good = f"Bearer {token.get_secret_value()}"
    cases = [
        ({}, {}, 401),
        ({"Authorization": "Bearer " + "w" * 40}, {}, 401),
        ({"Authorization": good, "Host": "evil.example:8782"}, {}, 400),
        ({"Authorization": good}, {"base": "http://127.0.0.1:9999"}, 400),
        ({"Authorization": good, "Origin": "http://evil.example"}, {}, 403),
        ({"Authorization": good, "Forwarded": "for=127.0.0.1"}, {}, 400),
        ({"Authorization": good, "X-Forwarded-For": "127.0.0.1"}, {}, 400),
        ({"Authorization": good}, {"peer": ("198.51.100.7", 1234)}, 403),
        ({"Authorization": good, "Sec-Fetch-Mode": "cors"}, {}, 403),
    ]
    for headers, options, expected in cases:
        async with client(
            app,
            None,
            peer=options.get("peer", ("127.0.0.1", 50124)),
            base=options.get("base", BASE),
        ) as api:
            response = await api.post(
                "/v1/runtime/teams",
                json={"spec": team_spec().model_dump(mode="json")},
                headers={"Idempotency-Key": "forged", **headers},
            )
        assert response.status_code == expected, headers
    assert app.state.local_runtime_store.counts() == {
        "teams": 0,
        "team_members": 0,
        "team_operations": 0,
        "runs": 0,
    }


async def test_ac4_other_owner_cannot_read_or_mutate_runs_or_teams(make_app, token):
    first = make_app()
    spec = team_spec()
    scout = member(spec, "source_scout")
    async with client(first, token) as api:
        await prepare(api, spec)
        await run(api, scout, task(scout, run_id="run-owner-a"))
    other_token = SecretStr(secrets.token_urlsafe(32))
    other = make_app(
        boundary=LocalServiceBoundary.create(
            owner_id="installation-owner-2", service_token=other_token, allowed_hosts=[HOST]
        )
    )
    async with client(other, other_token) as api:
        assert (await api.get(f"/v1/runtime/teams/{spec.team_id}")).status_code == 404
        clean = await api.post(
            f"/v1/runtime/teams/{spec.team_id}/cleanup", headers={"Idempotency-Key": "b-clean"}
        )
        assert clean.status_code == 404
        assert (await api.get("/v1/runtime/tasks/run-owner-a")).status_code == 404
        assert (await api.get("/v1/runtime/tasks/run-owner-a/journal")).status_code == 404
        assert (await api.post("/v1/runtime/tasks/run-owner-a/cancel")).status_code == 404
        takeover = await prepare(api, spec, key="b-prepare")
        assert takeover.status_code == 403 and takeover.json()["code"] == "owner_mismatch"
        as_member = (await run(api, scout, task(scout, run_id="run-owner-b"))).json()
        assert as_member["status"] == "denied" and as_member["error"]["code"] == "agent_not_owned"
    async with client(first, token) as api:
        assert (await api.get(f"/v1/runtime/teams/{spec.team_id}")).json()["state"] == "ready"
        assert (await api.get("/v1/runtime/tasks/run-owner-b")).status_code == 404


async def test_ac4_errors_results_and_logs_never_echo_secret_or_payload(make_app, token, caplog):
    caplog.set_level(logging.DEBUG)
    app = make_app()
    secret = token.get_secret_value()
    supervisor = domain_supervisor()
    async with client(app, token) as api:
        responses = [
            await api.post(
                "/v1/runtime/tasks",
                json={"spec": {"agent_id": CANARY}, "request": {"payload": {"k": secret}}},
            ),
            await api.post(
                "/v1/runtime/teams",
                content=("{" + CANARY).encode(),
                headers={"Idempotency-Key": "echo", "Content-Type": "application/json"},
            ),
            await run(
                api,
                supervisor,
                task(supervisor, run_id="r-echo", task_type=f"x-{CANARY}", payload={"s": CANARY}),
            ),
            await api.get(f"/v1/runtime/tasks/{CANARY}!"),
        ]
    async with client(app, None) as api:
        responses.append(await api.get("/v1/capabilities", headers={"Authorization": CANARY}))
    assert [r.status_code for r in responses] == [422, 422, 200, 404, 401]
    for response in responses:
        assert CANARY not in response.text and secret not in response.text
    service_logs = "\n".join(
        record.getMessage() for record in caplog.records if not record.name.startswith("httpx")
    )
    assert CANARY not in service_logs and secret not in service_logs
    journal = app.state.local_runtime_store.get_journal("r-echo", owner_id=OWNER)
    assert journal.state == "denied" and CANARY not in journal.model_dump_json()


async def test_ac4_health_and_capabilities_never_claim_real_runtime(make_app, token):
    app = make_app()
    async with client(app, None) as anonymous:
        health = (await anonymous.get("/healthz")).json()
        assert health["mode"] == "local" and health["simulated"] is True
        assert health["sandbox"] is False and health["handlers"] == "synthetic"
        assert (await anonymous.get("/v1/capabilities")).status_code == 401
    async with client(app, token) as api:
        capabilities = (await api.get("/v1/capabilities")).json()
    assert capabilities["supported_runtime_kinds"] == ["local"]
    assert capabilities["handlers"] == ["domain_task", "synthetic.gate", "synthetic.role_note"]
    assert capabilities["restart_policy"] == "inflight_marked_unknown_no_auto_replay"
    assert {"openshell", "core_runtime_http_capability", "os_sandbox"} <= set(
        capabilities["real_integrations"]
    )
    assert not any(capabilities["real_integrations"].values())
