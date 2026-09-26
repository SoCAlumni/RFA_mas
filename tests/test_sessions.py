from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys

import httpx
import pytest

from rfa_mas.adapters.local import SqliteWorkRepository
from rfa_mas.api.app import create_app, resolve_principal
from rfa_mas.contracts import (
    Audience,
    DirectWorkRequest,
    DomainId,
    DraftTarget,
    PersistentTask,
    RunResult,
    SessionDetail,
    TrustedPrincipal,
    WorkRequest,
    WorkStatus,
)
from rfa_mas.errors import RfaError


def identity(name: str) -> TrustedPrincipal:
    # Only test-owned, server-injected authenticated fixtures grant identities.
    return TrustedPrincipal(user_id=name, authenticated=True)


def work(**overrides) -> DirectWorkRequest:
    values = {
        "query": "TRIV3 public evidence",
        "domain_id": DomainId.TRIV3,
        "target": DraftTarget(audience=Audience.PUBLIC),
    }
    return DirectWorkRequest(**(values | overrides))


async def test_session_ids_are_server_generated_and_default_identity_has_no_fixture_rights(
    container,
):
    local = await container.service.sessions.local_principal()
    assert local.user_id.startswith("owner_")
    assert local.user_id not in {"fixture-owner-001", "api-client"}
    assert local.authenticated and not local.business_units and not local.roles
    assert local.company_id is None
    session = await container.service.sessions.create(local)
    assert session.owner_id == local.user_id
    assert session.session_id.startswith("session_")
    assert session.thread_id.startswith("thread_")
    reopened = SqliteWorkRepository(container.repository.path)
    await reopened.initialize()
    assert await reopened.local_principal() == local
    assert (await reopened.get_session(session.session_id, local)).thread_id == session.thread_id


async def test_same_session_multiple_tasks_and_same_task_multiple_sessions(container):
    owner = identity("owner-a")
    first = await container.service.sessions.create(owner)
    second = await container.service.sessions.create(owner)
    for task_id in ("task-a", "task-b"):
        await container.repository.register_task_owner(
            PersistentTask(
                task_id=task_id,
                owner_id=owner.user_id,
                domain_id=DomainId.TRIV3,
                goal="Synthetic task",
            )
        )
    requests = [
        work(session_id=first.session_id, task_id="task-a"),
        work(session_id=first.session_id, task_id="task-b"),
        work(session_id=second.session_id, task_id="task-a"),
    ]
    results = [await container.service.run(request, owner) for request in requests]
    assert all(result.status == WorkStatus.COMPLETED for result in results)
    detail = await container.service.sessions.get(first.session_id, owner)
    assert detail.task_ids == ("task-a", "task-b")
    assert len(detail.runs) == 2 and len(detail.messages) == 4
    assert [message.role for message in detail.messages] == ["user", "assistant"] * 2
    assert {run.thread_id for run in detail.runs} == {first.thread_id}
    second_detail = await container.service.sessions.get(second.session_id, owner)
    assert second_detail.task_ids == ("task-a",)
    assert second_detail.thread_id != first.thread_id


async def test_task_references_never_grant_ownership_or_reassign_tasks(container):
    alice, bob = identity("alice"), identity("bob")
    session = await container.service.sessions.create(bob)
    task = PersistentTask(
        task_id="alice-task", owner_id=alice.user_id, domain_id=DomainId.TRIV3, goal="private"
    )
    await container.repository.register_task_owner(task)
    for task_id in ("alice-task", "missing-task"):
        with pytest.raises(RfaError) as error:
            await container.service.run(work(session_id=session.session_id, task_id=task_id), bob)
        assert error.value.code == "not_found"
        assert "alice" not in error.value.safe_message
    with pytest.raises(RfaError, match="Task"):
        await container.repository.register_task_owner(task.model_copy(update={"owner_id": "bob"}))
    detail = await container.service.sessions.get(session.session_id, bob)
    assert not detail.runs and not detail.messages and not detail.task_ids


async def test_task_domain_must_match_registered_task(container):
    owner = identity("owner")
    await container.repository.register_task_owner(
        PersistentTask(
            task_id="task-other-domain",
            owner_id=owner.user_id,
            domain_id=DomainId.QUANTIZATION_RESEARCH,
            goal="research",
        )
    )
    with pytest.raises(RfaError) as error:
        await container.service.run(work(task_id="task-other-domain"), owner)
    assert error.value.code == "task_domain_mismatch"
    # The failed run and its implicit session are rolled back together.
    assert await container.service.sessions.list(owner) == []


async def test_repository_owner_checks_and_anonymous_denial(container):
    alice, bob = identity("alice"), identity("bob")
    request = work(query="SYNTHETIC_OWNER_HISTORY_ONLY")
    result = await container.service.run(request, alice)
    session = (await container.service.sessions.list(alice))[0]
    assert await container.service.sessions.list(bob) == []
    for operation in (
        container.service.sessions.get(session.session_id, bob),
        container.service.sessions.get_run(result.run_id, bob),
        container.service.get(result.run_id, bob),
        container.service.run(work(session_id=session.session_id), bob),
    ):
        with pytest.raises(RfaError) as caught:
            await operation
        assert caught.value.code == "not_found"
        assert "SYNTHETIC_OWNER_HISTORY_ONLY" not in caught.value.safe_message
        assert session.thread_id not in caught.value.safe_message
    anonymous = TrustedPrincipal(user_id="alice", authenticated=False)
    with pytest.raises(RfaError) as error:
        await container.service.sessions.get(session.session_id, anonymous)
    assert error.value.code == "authentication_required"
    denied = await container.service.run(work(), anonymous)
    assert denied.errors[0].code == "policy_denied"
    assert len((await container.service.sessions.get(session.session_id, alice)).runs) == 1


async def test_duplicate_run_atomicity_and_owner_scoped_idempotency(container):
    alice, bob = identity("alice"), identity("bob")
    handler = container.runtime._handlers["domain_task"]
    calls = []

    async def spy(spec, request):
        calls.append((request.payload["principal"]["user_id"], request.idempotency_key))
        return await handler(spec, request)

    container.runtime.register("domain_task", spy)
    request = work(idempotency_key="shared-key")
    outcomes = await asyncio.gather(
        container.service.run(request, alice),
        container.service.run(request, alice),
        return_exceptions=True,
    )
    assert sum(isinstance(item, RunResult) for item in outcomes) == 1
    assert next(item for item in outcomes if isinstance(item, RunResult)).status == "completed"
    failures = [item for item in outcomes if isinstance(item, RfaError)]
    assert len(failures) == 1 and failures[0].code == "idempotency_conflict"
    sessions = await container.service.sessions.list(alice)
    assert len(sessions) == 1
    detail = await container.service.sessions.get(sessions[0].session_id, alice)
    assert len(detail.messages) == 2 and len(detail.runs) == 1
    # Identity, not a globally chosen client key, scopes application replay keys.
    second_result = await container.service.run(work(idempotency_key="shared-key"), bob)
    assert second_result.status == "completed"
    assert len(await container.service.sessions.list(bob)) == 1
    assert [caller for caller, _ in calls] == ["alice", "bob"]
    assert len({key for _, key in calls}) == 2


async def test_graph_projection_preserves_principal_and_checks_session_before_worker(container):
    alice, bob = identity("alice"), identity("bob")
    session = await container.service.sessions.create(alice)
    task = PersistentTask(
        task_id="projection-task",
        owner_id=alice.user_id,
        domain_id=DomainId.TRIV3,
        goal="synthetic",
    )
    await container.repository.register_task_owner(task)
    handler = container.runtime._handlers["domain_task"]
    calls = []

    async def spy(spec, request):
        calls.append(request)
        assert request.payload["principal"] == alice.model_dump(mode="json")
        projected = WorkRequest.model_validate(request.payload["work_request"])
        assert projected.schema_version == "1.0"
        return await handler(spec, request)

    container.runtime.register("domain_task", spy)
    for unauthorized in (bob, alice.model_copy(update={"authenticated": False})):
        try:
            outcome = await container.service.run(
                work(session_id=session.session_id, task_id=task.task_id), unauthorized
            )
            assert outcome.errors[0].code == "policy_denied"
        except RfaError as error:
            assert error.code == "not_found"
        assert calls == []
    result = await container.service.run(
        work(session_id=session.session_id, task_id=task.task_id), alice
    )
    assert result.status == "completed" and len(calls) == 1
    record = await container.repository.get_owned_run(result.run_id, alice)
    assert record.session_id == session.session_id and record.task_id == task.task_id


async def test_legacy_migration_is_versioned_idempotent_and_never_adopts_unknown_owner(tmp_path):
    path = tmp_path / "legacy.db"
    legacy = WorkRequest(query="SYNTHETIC_PRIVATE_LEGACY")
    timestamp = "2026-09-26T00:00:00+00:00"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE runs (
            run_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, trace_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
            request_json TEXT NOT NULL, result_json TEXT, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL)""")
        db.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)",
            (
                legacy.run_id,
                legacy.request_id,
                legacy.trace_id,
                legacy.idempotency_key,
                "waiting_approval",
                legacy.model_dump_json(),
                timestamp,
                timestamp,
            ),
        )
    repository = SqliteWorkRepository(path)
    await repository.initialize()
    await repository.initialize()
    owner = await repository.local_principal()
    assert await repository.list_sessions(owner) == []
    with pytest.raises(RfaError) as error:
        await repository.get_owned_run(legacy.run_id, owner)
    assert error.value.code == "not_found"
    with sqlite3.connect(path) as db:
        row = db.execute("SELECT request_json, owner_id, session_id, status FROM runs").fetchone()
        assert row == (legacy.model_dump_json(), None, None, "waiting_approval")
        assert db.execute("SELECT version FROM rfa_schema_migrations").fetchall() == [(1,)]


@pytest.mark.parametrize("final_status", ["running", "waiting_approval", "failed", "cancelled"])
async def test_fresh_process_reads_history_and_incomplete_run_state(container, final_status):
    owner = await container.repository.local_principal()
    initial = await container.service.run(work(query="SYNTHETIC_HISTORY_KEEP"), owner)
    session = (await container.service.sessions.list(owner))[0]
    pending = work(session_id=session.session_id)
    await container.repository.create_owned_run(pending, owner, session_id=session.session_id)
    await container.repository.transition_run(pending.run_id, WorkStatus.RUNNING)
    if final_status != "running":
        await container.repository.transition_run(pending.run_id, WorkStatus(final_status))
    script = """
import asyncio, sys
from pathlib import Path
from rfa_mas.adapters.local import SqliteWorkRepository
async def inspect():
    repository = SqliteWorkRepository(Path(sys.argv[1]))
    await repository.initialize()
    owner = await repository.local_principal()
    detail = await repository.get_session(sys.argv[2], owner)
    print(detail.model_dump_json())
asyncio.run(inspect())
"""
    outcome = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", script, str(container.repository.path), session.session_id],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert outcome.returncode == 0, outcome.stderr
    detail = SessionDetail.model_validate_json(outcome.stdout)
    assert detail.owner_id == owner.user_id and detail.thread_id == session.thread_id
    assert detail.messages[0].content == "SYNTHETIC_HISTORY_KEEP"
    assert {run.run_id: run.status.value for run in detail.runs} == {
        initial.run_id: "completed",
        pending.run_id: final_status,
    }
    assert detail.runs[1].result is None


async def test_api_authorization_sessions_history_run_and_continue(container):
    app = create_app(container=container)
    current = identity("api-alice")

    async def fixture_principal():
        return current

    app.dependency_overrides[resolve_principal] = fixture_principal
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        created = await api.post("/v1/sessions", json={})
        assert created.status_code == 201
        session = created.json()
        body = work(query="SYNTHETIC_HISTORY_API").model_dump(mode="json")
        response = await api.post(f"/v1/sessions/{session['session_id']}/work", json=body)
        assert response.status_code == 201
        run_id = response.json()["run_id"]
        assert (await api.get(f"/v1/work/{run_id}")).json() == response.json()
        detail = (await api.get(f"/v1/sessions/{session['session_id']}")).json()
        assert detail["messages"][0]["content"] == "SYNTHETIC_HISTORY_API"
        status = await api.get(f"/v1/runs/{run_id}")
        assert status.json()["thread_id"] == session["thread_id"]
        current = identity("api-bob")
        assert (await api.get("/v1/sessions")).json() == []
        for existing, missing in (
            (f"/v1/work/{run_id}", "/v1/work/absent"),
            (f"/v1/runs/{run_id}", "/v1/runs/absent"),
            (f"/v1/sessions/{session['session_id']}", "/v1/sessions/absent"),
        ):
            denied = await api.get(existing)
            absent = await api.get(missing)
            assert denied.status_code == absent.status_code == 404
            assert denied.json() == absent.json()
            assert "SYNTHETIC_HISTORY_API" not in denied.text
            assert session["thread_id"] not in denied.text
        denied = await api.post(f"/v1/sessions/{session['session_id']}/work", json=body)
        assert denied.status_code == 404
        assert (await api.get(f"/v1/threads/{session['thread_id']}")).status_code == 404


@pytest.mark.parametrize("claim", ["owner_id", "thread_id", "business_units", "principal"])
async def test_api_rejects_body_identity_and_thread_claims(container, claim):
    app = create_app(container=container)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        bad = await api.post("/v1/sessions", json={claim: "forged"})
        assert bad.status_code == 422
        session = (await api.post("/v1/sessions", json={})).json()
        body = work().model_dump(mode="json") | {claim: "forged"}
        bad = await api.post(f"/v1/sessions/{session['session_id']}/work", json=body)
        assert bad.status_code == 422
        detail = (await api.get(f"/v1/sessions/{session['session_id']}")).json()
        assert detail["messages"] == [] and detail["runs"] == []


async def test_session_body_binding_and_progress_endpoint(container):
    owner = await container.repository.local_principal()
    first, second = [await container.service.sessions.create(owner) for _ in range(2)]
    pending = work(session_id=first.session_id)
    await container.repository.create_owned_run(pending, owner, session_id=first.session_id)
    app = create_app(container=container)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        mismatch = await api.post(
            f"/v1/sessions/{first.session_id}/work",
            json=work(session_id=second.session_id).model_dump(mode="json"),
        )
        assert mismatch.status_code == 400 and mismatch.json()["code"] == "session_mismatch"
        status = await api.get(f"/v1/runs/{pending.run_id}")
        assert status.status_code == 200 and status.json()["status"] == "created"
        assert status.json()["result"] is None
        assert (await api.get(f"/v1/work/{pending.run_id}")).status_code == 404


async def test_keyless_identity_rejects_remote_peer_and_forwarded_claims(container):
    app = create_app(container=container)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("203.0.113.42", 1234)),
        base_url="http://127.0.0.1",
    ) as api:
        response = await api.get(
            "/v1/sessions",
            headers={
                "X-Forwarded-For": "127.0.0.1",
                "X-User-Id": "fixture-owner-001",
            },
        )
    assert response.status_code == 401


@pytest.mark.parametrize(
    "header", ["Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Real-IP"]
)
async def test_keyless_mode_rejects_proxy_headers_even_from_loopback(container, header):
    app = create_app(container=container)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
        base_url="http://test",
    ) as api:
        blocked = await api.post("/v1/sessions", json={}, headers={header: "127.0.0.1"})
        assert blocked.status_code == 401
        assert (await api.get("/v1/sessions")).json() == []


@pytest.mark.parametrize("session_route", [False, True])
async def test_http_issues_run_ids_without_foreign_existence_oracle(container, session_route):
    app = create_app(container=container)
    current = identity("alice")

    async def fixture_principal():
        return current

    app.dependency_overrides[resolve_principal] = fixture_principal
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        alice_request = WorkRequest(query="TRIV3 public", domain_id=DomainId.TRIV3)
        alice_result = await api.post("/v1/work", json=alice_request.model_dump(mode="json"))
        assert alice_result.status_code == 201
        alice_id = alice_result.json()["run_id"]
        assert alice_id != alice_request.run_id
        current = identity("bob")
        session = (await api.post("/v1/sessions", json={})).json()
        endpoint = f"/v1/sessions/{session['session_id']}/work" if session_route else "/v1/work"
        issued_ids = []
        for suggested_id in (alice_id, "unused-caller-run-id"):
            request = (
                work(run_id=suggested_id)
                if session_route
                else WorkRequest(
                    query="TRIV3 public", domain_id=DomainId.TRIV3, run_id=suggested_id
                )
            )
            response = await api.post(endpoint, json=request.model_dump(mode="json"))
            assert response.status_code == 201 and response.json()["status"] == "completed"
            assert response.json()["run_id"] != suggested_id
            assert response.json()["request_id"] == request.request_id
            assert response.json()["trace_id"] == request.trace_id
            issued_ids.append(response.json()["run_id"])
            replay = await api.post(endpoint, json=request.model_dump(mode="json"))
            assert replay.status_code == 409 and replay.json()["code"] == "idempotency_conflict"
        assert len(set(issued_ids)) == 2 and alice_id not in issued_ids
        denied = await api.get(f"/v1/work/{alice_id}")
        absent = await api.get("/v1/work/absent-run")
        assert denied.status_code == absent.status_code == 404
        assert denied.json() == absent.json()


def test_additive_routes_preserve_original_wire_contract(container):
    from scripts.contract_baseline import BASELINE, build_baseline, build_extended

    assert build_baseline() == json.loads(BASELINE.read_text())
    extended = build_extended()
    api = create_app(container=container).openapi()
    assert extended["implemented_http_routes"]["core"] == api
    assert "/v1/sessions/{session_id}/work" in api["paths"]
    assert "SessionDetail" in api["components"]["schemas"]
    assert api["paths"]["/v1/work"]["post"]["requestBody"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/WorkRequest"}
