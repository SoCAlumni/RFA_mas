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


def _client(container, peer=("127.0.0.1", 1234)):
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
        async with _client(container) as client:
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
        async with _client(container, peer=("203.0.113.9", 1234)) as remote:
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
        async with _client(container) as client:
            async with _client(container, peer=("203.0.113.9", 1234)) as remote:
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
