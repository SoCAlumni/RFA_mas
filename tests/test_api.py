from __future__ import annotations

import httpx

from rfa_mas.api.app import create_app
from rfa_mas.bootstrap import build_container
from rfa_mas.contracts import Audience, DomainId, DraftTarget, WorkRequest
from rfa_mas.settings import Settings


async def test_health_ready_openapi_and_work_endpoints(container) -> None:
    app = create_app(container=container)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        health = await client.get("/healthz")
        ready = await client.get("/readyz")
        openapi = await client.get("/openapi.json")
        request = WorkRequest(
            query="TRIV3 공개 트랙을 근거와 함께 요약해 줘.",
            domain_id=DomainId.TRIV3,
            target=DraftTarget(audience=Audience.PUBLIC),
        )
        created = await client.post("/v1/work", json=request.model_dump(mode="json"))
        fetched = await client.get(f"/v1/work/{created.json()['run_id']}")

    assert health.status_code == 200
    assert ready.status_code == 200
    assert openapi.status_code == 200
    assert "/v1/work" in openapi.json()["paths"]
    assert created.status_code == 201
    assert created.json()["run_id"] != request.run_id
    assert created.json()["simulated"] is True
    assert created.json() == fetched.json()


async def test_duplicate_work_request_is_not_executed_twice(container) -> None:
    app = create_app(container=container)
    request = WorkRequest(query="TRIV3 synthetic request", domain_id=DomainId.TRIV3)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.post("/v1/work", json=request.model_dump(mode="json"))
        duplicate = await client.post("/v1/work", json=request.model_dump(mode="json"))

    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "idempotency_conflict"


async def test_api_auth_and_untrusted_identity_claims(tmp_path) -> None:
    fake_secret = "known-fake-api-secret-for-tests"
    settings = Settings(
        _env_file=None,
        app_api_key=fake_secret,
        database_url=f"sqlite:///{tmp_path / 'auth.db'}",
        trace_dir=tmp_path / "traces",
    )
    container = build_container(settings)
    await container.startup()
    try:
        app = create_app(container=container)
        body = WorkRequest(
            query="public synthetic request",
            domain_id=DomainId.TRIV3,
            target=DraftTarget(audience=Audience.PUBLIC),
        ).model_dump(mode="json")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            unauthenticated = await client.post("/v1/work", json=body)
            body["user_id"] = "forged-owner"
            forged_identity = await client.post(
                "/v1/work",
                json=body,
                headers={"Authorization": f"Bearer {fake_secret}"},
            )
    finally:
        await container.shutdown()

    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["code"] == "authentication_required"
    assert fake_secret not in unauthenticated.text
    assert forged_identity.status_code == 422


async def test_api_key_resolves_persisted_owner_without_fixture_memberships(tmp_path) -> None:
    fake_secret = "synthetic-single-installation-secret"
    instance = build_container(
        Settings(
            _env_file=None,
            app_api_key=fake_secret,
            database_url=f"sqlite:///{tmp_path / 'owner.db'}",
            trace_dir=tmp_path / "trace",
        )
    )
    await instance.startup()
    try:
        app = create_app(container=instance)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("203.0.113.1", 1234)),
            base_url="http://test",
        ) as client:
            missing = await client.get("/v1/sessions")
            wrong = await client.get("/v1/sessions", headers={"Authorization": "Bearer invalid"})
            created = await client.post(
                "/v1/sessions",
                json={},
                headers={
                    "Authorization": f"Bearer {fake_secret}",
                    "X-User-Id": "fixture-owner-001",
                    "X-Forwarded-For": "127.0.0.1",
                },
            )
        owner = await instance.repository.local_principal()
    finally:
        await instance.shutdown()
    assert missing.status_code == wrong.status_code == 401
    assert created.status_code == 201 and created.json()["owner_id"] == owner.user_id
    assert owner.user_id.startswith("owner_")
    assert not owner.business_units and not owner.roles and owner.company_id is None
    assert fake_secret not in missing.text + wrong.text + created.text



# -- P0-020B: team result and cancel routes ------------------------------------------------
import asyncio  # noqa: E402

from test_team_execution import make, request  # noqa: E402

from rfa_mas.contracts import TeamRunResult, TrustedPrincipal  # noqa: E402


def _team_client(container, peer=("127.0.0.1", 1234)):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(container=container), client=peer),
        base_url="http://test",
    )


async def test_team_route_returns_owner_team_receipt_and_hides_others(tmp_path) -> None:
    container, owner = await make(tmp_path)
    try:
        team_run = await container.service.run(request(), owner)
        plain = await container.service.run(request(team=False), owner)
        foreign = WorkRequest(query="foreign", domain_id=DomainId.TRIV3)
        other = TrustedPrincipal(user_id="fixture-other-owner", authenticated=True)
        await container.repository.create_owned_run(foreign, other, session_id=None)
        async with _team_client(container) as client:
            found = await client.get(f"/v1/runs/{team_run.run_id}/team")
            assert found.status_code == 200
            team = TeamRunResult.model_validate(found.json())
            assert team.run_id == team_run.run_id and team.status == "completed"
            assert [r.role for r in team.roles] == [
                "paper_scout", "experiment_runner", "result_analyst", "supervisor"]
            assert team == await container.service.team_result(team_run.run_id, owner)
            # No team, another owner's run and an absent run are indistinguishable 404s.
            responses = [await client.get(f"/v1/runs/{run_id}/team")
                         for run_id in (plain.run_id, foreign.run_id, "run-absent")]
            assert {r.status_code for r in responses} == {404}
            assert len({r.text for r in responses}) == 1
            assert responses[0].json()["code"] == "not_found"
        async with _team_client(container, peer=("203.0.113.9", 1234)) as remote:
            unauthenticated = await remote.get(f"/v1/runs/{team_run.run_id}/team")
            assert unauthenticated.status_code == 401
    finally:
        await container.shutdown()


async def test_cancel_route_signals_running_team_and_rejects_terminal_or_foreign(tmp_path):
    container, owner = await make(tmp_path)
    runner = container.team_runner
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
        async with _team_client(container) as client:
            async with _team_client(container, peer=("203.0.113.9", 1234)) as remote:
                denied = await remote.post(f"/v1/runs/{work.run_id}/cancel")
                assert denied.status_code == 401  # No owner auth, no cancel signal.
            cancelled = await client.post(f"/v1/runs/{work.run_id}/cancel")
            assert cancelled.status_code == 200 and cancelled.json() == {"state": "cancelling"}
            release.set()
            result = await asyncio.wait_for(task, 10)
            assert result.status.value == "cancelled"
            terminal = await client.post(f"/v1/runs/{work.run_id}/cancel")
            assert terminal.status_code == 409
            assert terminal.json()["code"] == "invalid_state_transition"
            absent = await client.post("/v1/runs/run-absent/cancel")
            assert absent.status_code == 404 and absent.json()["code"] == "not_found"
            team = await client.get(f"/v1/runs/{work.run_id}/team")
            assert team.status_code == 200 and team.json()["status"] == "cancelled"
    finally:
        release.set()
        await container.shutdown()


async def test_cancel_route_ends_a_run_waiting_for_approval(tmp_path):
    from rfa_mas.contracts import SimulationScenario

    container, owner = await make(tmp_path)
    try:
        waiting = await container.service.run(
            request(team=False, simulation_scenario=SimulationScenario.REVISION_REQUESTED),
            owner)
        assert waiting.status.value == "waiting_approval"
        async with _team_client(container) as client:
            ended = await client.post(f"/v1/runs/{waiting.run_id}/cancel")
            assert ended.status_code == 200 and ended.json() == {"state": "cancelled"}
            again = await client.post(f"/v1/runs/{waiting.run_id}/cancel")
            assert again.status_code == 409
        record = await container.repository.get_owned_run(waiting.run_id, owner)
        assert record.status.value == "cancelled"
    finally:
        await container.shutdown()


# -- P0-025: owner status views, safe cursor events, readiness and registry ---------------
import json  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from pydantic import SecretStr  # noqa: E402
from test_draft_lifecycle import Authority, install  # noqa: E402
from test_team_execution import request as team_request  # noqa: E402

from rfa_mas.api.app import resolve_principal  # noqa: E402
from rfa_mas.contracts import (  # noqa: E402
    DirectWorkRequest,
    KnowledgeDelete,
    KnowledgeWrite,
    PublishRequest,
    SimulationScenario,
)

TITLE_CANARY = "PRIVATE_TITLE_CANARY_P0025"
BODY_CANARY = "SYNTHETIC_PRIVATE_CANARY_P0025_BODY"
SECRET = "synthetic-secret-value-p0025-not-a-credential"
OTHER = TrustedPrincipal(user_id="fixture-other-owner-p0025", authenticated=True)


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _note(key, title, content, audience):
    return KnowledgeWrite.model_validate({
        "domain_id": "triv3",
        "provenance": {"provider": "note", "namespace": "p0025", "external_id": key},
        "provider_revision": "r1", "title": title, "content": content, "synthetic": True,
        "acl": {"audience": audience},
    })


def _public(query="TRIV3 공개 트랙", **extra):
    return DirectWorkRequest(query=query, domain_id=DomainId.TRIV3,
                             target=DraftTarget(audience=Audience.PUBLIC), **extra)


async def test_status_and_events_link_draft_review_receipt_to_the_same_run(container):
    install(container, Authority())
    owner = await container.repository.local_principal()
    result = await container.service.run(_public(), owner)
    content_hash = result.draft.content_hash
    async with _client(create_app(container=container)) as api:
        before = (await api.get(f"/v1/runs/{result.run_id}/status")).json()
        # Stages that did not happen are null, never invented.
        assert before["receipt"] is None and before["task"] is None and before["team"] is None
        assert before["workers"] == []
        await container.drafts.publish(result.run_id, PublishRequest(idempotency_key="p-1"), owner)
        status = (await api.get(f"/v1/runs/{result.run_id}/status")).json()
        page = (await api.get("/v1/events", params={"run_id": result.run_id})).json()
    assert status["status"] == "completed" and status["stop_reason"] == result.stop_reason
    assert (status["draft"]["version"], status["draft"]["content_hash"]) == (1, content_hash)
    assert status["review"]["decision"] == "approved"
    assert status["review"]["review_ref"] == f"review:{result.draft.draft_id}@v1:{content_hash}"
    receipt = status["receipt"]
    assert (receipt["status"], receipt["mode"], receipt["draft_version"]) == (
        "succeeded", "mock", 1)
    assert receipt["content_hash"] == content_hash and receipt["approval_id"]
    events = page["events"]
    kinds = {e["kind"] for e in events}
    assert {"run", "context", "policy", "draft", "review", "publication"} <= kinds
    assert not {"team", "role", "notification"} & kinds
    assert all(e["run_id"] == result.run_id and e["session_id"] == status["session_id"]
               for e in events)
    by_kind = {e["kind"]: e for e in events}
    assert by_kind["draft"]["draft"]["content_hash"] == content_hash
    assert by_kind["review"]["review"]["content_hash"] == content_hash
    assert by_kind["publication"]["receipt"]["content_hash"] == content_hash
    assert by_kind["policy"]["policy"]["outcome"] == "allowed"
    context = by_kind["context"]
    cited = {s["source_id"] for s in by_kind["draft"]["sources"]}
    staged = {s["source_id"]: s["stage"] for s in context["sources"]}
    assert cited and cited <= staged.keys() and all(staged[s] == "L2" for s in cited)
    assert {s["stage"] for s in context["stages"]} <= {"L0", "L1", "L2"}
    assert all(e["task_id"] is None and e["team_id"] is None for e in events)


async def test_team_and_failed_runs_report_task_team_workers_and_stop_reason(tmp_path):
    team_container, owner = await make(tmp_path)
    try:
        team_run = await team_container.service.run(team_request(), owner)
        denied = await team_container.service.run(
            _public(simulation_scenario=SimulationScenario.POLICY_DENIED), owner)
        async with _client(create_app(container=team_container)) as api:
            status = (await api.get(f"/v1/runs/{team_run.run_id}/status")).json()
            task = (await api.get(f"/v1/tasks/{status['task']['task_id']}/status")).json()
            failed = (await api.get(f"/v1/runs/{denied.run_id}/status")).json()
            events = (await api.get("/v1/events", params={"limit": 200})).json()["events"]
    finally:
        await team_container.shutdown()
    assert status["task"]["status"] == "active" and task == status["task"]
    assert status["team"]["team_id"] == task["team"]["team_id"]
    assert status["team"]["status"] == "completed" and status["team"]["stop_reason"] is None
    assert [w["role"] for w in status["workers"]] == [
        "paper_scout", "experiment_runner", "result_analyst", "supervisor"]
    assert all(w["status"] == "succeeded" for w in status["workers"])
    assert failed["status"] == "failed" and failed["stop_reason"] == "policy_denied"
    assert failed["error_codes"] == ["policy_denied"]
    assert failed["draft"] is None and failed["review"] is None and failed["receipt"] is None
    team_events = [e for e in events if e["run_id"] == team_run.run_id]
    roles = [e for e in team_events if e["kind"] == "role"]
    assert len(roles) == 4 and all(e["team_id"] == status["team"]["team_id"] for e in roles)
    denied_events = [e for e in events if e["run_id"] == denied.run_id]
    assert any(e["kind"] == "policy" and e["policy"]["outcome"] == "denied"
               for e in denied_events)
    assert any(e["kind"] == "run" and e["status"] == "failed"
               and e["reason_code"] == "policy_denied" for e in denied_events)


async def test_events_are_owner_scoped_cursor_stable_reference_only_and_reauthorized(container):
    owner = await container.repository.local_principal()
    await container.knowledge.write(
        _note("private", f"{TITLE_CANARY} TRIV3 1:1", f"{BODY_CANARY} TRIV3 트랙 일정", "private"),
        owner)
    public = await container.knowledge.write(
        _note("public", "TRIV3 공개 트랙 공지", "TRIV3 공개 트랙 공지 내용", "public"), owner)
    mine = await container.service.run(DirectWorkRequest(
        query="TRIV3 트랙 일정", domain_id=DomainId.TRIV3,
        target=DraftTarget(audience=Audience.OWNER)), owner)
    shared = await container.service.run(_public("TRIV3 공개 트랙 공지"), owner)
    app = create_app(container=container)
    async with _client(app) as api:
        first = (await api.get("/v1/events", params={"limit": 3})).json()
        rest = (await api.get("/v1/events", params={"cursor": first["next_cursor"],
                                                     "limit": 200})).json()
        resent = (await api.get("/v1/events", params={"cursor": first["next_cursor"],
                                                       "limit": 200})).json()
        statuses = [(await api.get(f"/v1/runs/{r.run_id}/status")).text for r in (mine, shared)]
        bad = await api.get("/v1/events", params={"cursor": "ev1_-1;drop"})
        zero = await api.get("/v1/events", params={"limit": 0})
        by_trace = await api.get(f"/v1/runs/{mine.trace_id}/status")
        await container.knowledge.delete(public.document.source_id, KnowledgeDelete(
            expected_revision=public.document.source_revision, mutation_id="delete-p0025"), owner)
        after_delete = (await api.get("/v1/events", params={"run_id": shared.run_id})).json()
        withheld_status = (await api.get(f"/v1/runs/{shared.run_id}/status")).json()
        app.dependency_overrides[resolve_principal] = lambda: OTHER
        other_page = (await api.get("/v1/events", params={
            "trace_id": mine.trace_id, "request_id": mine.request_id})).json()
        other_run = await api.get("/v1/events", params={"run_id": mine.run_id})
        other_status = await api.get(f"/v1/runs/{mine.run_id}/status")
        other_task = await api.get("/v1/tasks/task-absent/status")
    assert first["has_more"] is True and len(first["events"]) == 3
    events = first["events"] + rest["events"]
    assert rest["events"] == resent["events"] and rest["next_cursor"] == resent["next_cursor"]
    sequence = [int(e["cursor"].removeprefix("ev1_")) for e in events]
    assert sequence == list(range(1, len(events) + 1))  # Per-owner, gap-free, append-only.
    assert len({e["event_id"] for e in events}) == len(events)
    # Reference-only: the owner's own private note is referenced by ID, never by content.
    blob = json.dumps(events, ensure_ascii=False) + "".join(statuses)
    assert TITLE_CANARY not in blob and BODY_CANARY not in blob
    keys = {key for e in events for key in e} | {
        key for e in events for s in e["sources"] for key in s}
    assert not {"title", "content", "excerpt", "summary", "parents"} & keys
    assert bad.status_code == 400 and bad.json()["code"] == "invalid_cursor"
    assert zero.status_code == 422
    assert by_trace.status_code == 404  # A trace ID is not a lookup key.
    # Current ACL on read: the deleted public note is withheld from older draft events.
    draft_event = next(e for e in after_delete["events"] if e["kind"] == "draft")
    assert public.document.source_id not in {s["source_id"] for s in draft_event["sources"]}
    assert draft_event["withheld_sources"] >= 1
    assert withheld_status["draft_withheld"] is True and withheld_status["draft"] is None
    assert withheld_status["review"] is None
    # Another principal: no events, no status, whatever trace/request ID it presents.
    assert other_page["events"] == [] and other_page["next_cursor"] == "ev1_0"
    assert other_run.status_code == other_status.status_code == other_task.status_code == 404
    assert other_run.json() == other_status.json()


async def test_readiness_separates_liveness_from_selected_backends(tmp_path, container):
    async with _client(create_app(container=container)) as api:
        local = await api.get("/readyz")
    assert local.status_code == 200 and local.json()["ready"] is True
    assert {c["component"]: c["code"] for c in local.json()["checks"]} == {
        "service": "ok", "configuration": "ok"}

    def settings(name, **values):
        return Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / name}.db",
                        trace_dir=(tmp_path / f"{name}-traces").resolve(), **values)

    http = dict(response_backend="http", response_base_url="http://127.0.0.1:9",
                response_api_token=SecretStr(SECRET))

    def refused(request):
        raise httpx.ConnectError("synthetic refusal")

    def healthy(request):
        return httpx.Response(200 if request.url.path == "/healthz" else 404, json={})

    results = {}
    for name, transport in (("down", refused), ("up", healthy)):
        selected = build_container(settings(name, **http),
                                   http_transport=httpx.MockTransport(transport))
        await selected.startup()
        try:
            async with _client(create_app(container=selected)) as api:
                results[name] = (await api.get("/healthz"), await api.get("/readyz"))
        finally:
            await selected.shutdown()
    health, down = results["down"]
    assert health.status_code == 200 and down.status_code == 503
    checks = {c["component"]: c for c in down.json()["checks"]}
    assert down.json()["ready"] is False and checks["response"]["code"] == "unavailable"
    assert checks["configuration"]["code"] == "ok" and SECRET not in down.text
    assert results["up"][1].status_code == 200 and results["up"][1].json()["ready"] is True
    # A selected real backend that cannot be built: alive, not ready, exact names only.
    missing = create_app(settings("nvidia-missing", model_provider="nvidia"))
    invalid = create_app(settings("nvidia-invalid", model_provider="nvidia",
                                  nvidia_model="synthetic-model",
                                  nvidia_base_url="http://example.invalid/v1",
                                  nvidia_api_key=SecretStr(SECRET)))
    async with _client(missing) as api:
        alive, not_ready, work = (await api.get("/healthz"), await api.get("/readyz"),
                                  await api.post("/v1/work", json={"query": "x"}))
    async with _client(invalid) as api:
        invalid_ready = await api.get("/readyz")
    configuration = {c["component"]: c for c in not_ready.json()["checks"]}["configuration"]
    assert alive.status_code == 200 and not_ready.status_code == 503
    assert configuration["code"] == "configuration_error"
    assert configuration["missing"] == ["NVIDIA_API_KEY", "NVIDIA_MODEL"]
    assert work.status_code == 503 and work.json()["code"] == "configuration_error"
    invalid_check = {c["component"]: c for c in invalid_ready.json()["checks"]}["configuration"]
    assert invalid_ready.status_code == 503 and invalid_check["code"] == "configuration_error"
    assert invalid_check["invalid"] == ["NVIDIA_BASE_URL"] and SECRET not in invalid_ready.text
    # P1-002: a complete NVIDIA selection is implemented (no longer reserved) and ready.
    # Hosted reachability is not probed by /readyz (no credential is ever sent there).
    configured = build_container(settings("nvidia-configured", model_provider="nvidia",
                                          nvidia_model="synthetic-model",
                                          nvidia_api_key=SecretStr(SECRET)))
    await configured.startup()
    try:
        async with _client(create_app(container=configured)) as api:
            configured_ready = await api.get("/readyz")
    finally:
        await configured.shutdown()
    checks = {c["component"]: c for c in configured_ready.json()["checks"]}
    assert configured_ready.status_code == 200 and configured_ready.json()["ready"] is True
    assert checks["configuration"]["code"] == "ok" and checks["configuration"]["reserved"] == []
    assert SECRET not in configured_ready.text


def test_registry_lists_only_capabilities_the_default_service_provides(container):
    registry = json.loads(Path("registry/service.json").read_text(encoding="utf-8"))["service"]
    api = create_app(container=container).openapi()
    operations = {f"{method.upper()} {path}" for path, ops in api["paths"].items()
                  for method in ops}
    by_endpoint, by_adapter = registry["capability_endpoints"], registry["capability_adapters"]
    assert set(registry["capabilities"]) == set(by_endpoint) | set(by_adapter)
    assert not set(by_endpoint) & set(by_adapter)
    for capability, endpoints in by_endpoint.items():
        assert endpoints and set(endpoints) <= operations, capability
    adapters = {a.port: a.adapter for a in container.adapters} | {
        "tool": container.tool.adapter_name, "judge": container.judge.adapter_name}
    assert registry["default_adapters"] == adapters
    for capability, reference in by_adapter.items():
        port, name = reference.split(":", 1)
        assert adapters[port] == name, capability
    for endpoint in registry["endpoints"]:
        operation = api["paths"][endpoint["path"]][endpoint["method"].lower()]
        response = operation["responses"].get("200") or operation["responses"]["201"]
        schema = response["content"]["application/json"]["schema"]
        assert schema == {"$ref": f"#/components/schemas/{endpoint['response_model']}"}
    # Polling only: no SSE/stream/notification capability is claimed or served.
    assert "text/event-stream" not in json.dumps(api)
    assert registry["polling"] == {"transport": "http_get_polling", "server_sent_events": False,
                                   "events_cursor_param": "cursor",
                                   "notifications": "not_provided"}
    assert not any(word in capability for capability in registry["capabilities"]
                   for word in ("notification", "sse", "stream", "schedule"))


@pytest.mark.parametrize("cursor", ["ev1_", "ev2_1", "7", "ev1_1 x", "ev1_" + "9" * 19,
                                    "ev1_CANARY_P0025_CURSOR"])
async def test_malformed_cursors_are_rejected_without_echo(container, cursor):
    async with _client(create_app(container=container)) as api:
        response = await api.get("/v1/events", params={"cursor": cursor})
    assert response.status_code == 400 and response.json()["code"] == "invalid_cursor"
    assert "CANARY_P0025" not in response.text
