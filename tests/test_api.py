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
        fetched = await client.get(f"/v1/work/{request.run_id}")

    assert health.status_code == 200
    assert ready.status_code == 200
    assert openapi.status_code == 200
    assert "/v1/work" in openapi.json()["paths"]
    assert created.status_code == 201
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
