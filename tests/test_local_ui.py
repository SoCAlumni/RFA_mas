"""P0-025A: small same-origin local UI over the core API and the local review stand-in.

The core container is real (tmp_path SQLite, mock providers). Its ResponsePort is
the existing ResponseHttpAdapter pointed at the in-process review stand-in, using
the same dependency-injection pattern as tests/test_resume.py. No network/process.
"""

from __future__ import annotations

import os
import re
import secrets
import socket
import subprocess
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr

from rfa_mas.adapters.http import ReferenceHttpClient, ResponseHttpAdapter
from rfa_mas.api.app import create_app
from rfa_mas.bootstrap import Container, build_container
from rfa_mas.contracts import (
    Audience,
    DomainId,
    DraftBundleV11,
    DraftTarget,
    EvidenceRef,
    SourceLocation,
    SourceRevisionRef,
    sha256_text,
)
from rfa_mas.errors import BackendNotImplementedError
from rfa_mas.reference.local_response import create_local_response_app
from rfa_mas.reference.local_security import LocalServiceBoundary
from rfa_mas.settings import Settings
from rfa_mas.ui.app import STATIC_DIR, UpstreamTarget, create_local_ui_app

UI_HOST = "127.0.0.1:8780"
UI_ORIGIN = f"http://{UI_HOST}"
REVIEW_HOST = "127.0.0.1:8781"
CORE_URL = "http://127.0.0.1:8000"
MALICIOUS = '<img src=x onerror="alert(1)"><script>alert(2)</script>'
FIXED_UI_ROUTES = {
    "/",
    "/ui/",
    "/ui/static/app.js",
    "/ui/static/app.css",
    "/ui/api/csrf",
    "/ui/api/status",
    "/ui/api/sessions",
    "/ui/api/sessions/{session_id}",
    "/ui/api/sessions/{session_id}/work",
    "/ui/api/runs/{run_id}",
    "/ui/api/runs/{run_id}/refresh-review",
    "/ui/api/notes",
    "/ui/api/reviews",
    "/ui/api/reviews/{draft_id}",
    "/ui/api/reviews/{draft_id}/decision",
    "/ui/api/publications",
    "/ui/api/publications/{publication_id}",
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


@dataclass
class Stack:
    container: Container
    owner_id: str
    review_app: object
    review_token: SecretStr
    review_http: httpx.AsyncClient
    core_app: object
    ui: object
    tmp_path: Path

    def ui_with(self, review: UpstreamTarget | None):
        return create_local_ui_app(
            allowed_hosts=[UI_HOST],
            core=UpstreamTarget.in_process("core", self.core_app, base_url=CORE_URL),
            review=review,
        )


@pytest_asyncio.fixture
async def stack(tmp_path) -> AsyncIterator[Stack]:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'core' / 'rfa.db'}",
        trace_dir=tmp_path / "traces",
    )
    container = build_container(settings)
    await container.startup()
    owner = await container.repository.local_principal()
    review_token = SecretStr(secrets.token_urlsafe(32))
    review_app = create_local_response_app(
        db_path=tmp_path / "review" / "review.db",
        boundary=LocalServiceBoundary.create(
            owner_id=owner.user_id, service_token=review_token, allowed_hosts=[REVIEW_HOST]
        ),
    )
    review_http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=review_app), base_url=f"http://{REVIEW_HOST}"
    )
    adapter = ResponseHttpAdapter(
        ReferenceHttpClient(review_http, token=review_token, max_read_retries=1), simulated=True
    )
    container.service._dependencies = replace(container.service._dependencies, response=adapter)
    container.service.start(container.checkpoints.saver)
    core_app = create_app(container=container)
    ui = create_local_ui_app(
        allowed_hosts=[UI_HOST],
        core=UpstreamTarget.in_process("core", core_app, base_url=CORE_URL),
        review=UpstreamTarget.in_process(
            "review", review_app, base_url=f"http://{REVIEW_HOST}", token=review_token
        ),
    )
    try:
        yield Stack(
            container, owner.user_id, review_app, review_token, review_http, core_app, ui, tmp_path
        )
    finally:
        await review_http.aclose()
        await container.shutdown()


def browser(ui, *, origin: str | None = UI_ORIGIN, base=f"http://{UI_HOST}", **kwargs):
    headers = {"Sec-Fetch-Site": "same-origin"}
    if origin is not None:
        headers["Origin"] = origin
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=ui, client=("127.0.0.1", 50200)),
        base_url=base,
        headers=headers,
        **kwargs,
    )


async def with_csrf(client: httpx.AsyncClient) -> httpx.AsyncClient:
    token = (await client.get("/ui/api/csrf")).json()["csrf_token"]
    client.headers["X-RFA-CSRF"] = token
    return client


async def pending_run(api) -> tuple[dict, dict]:
    session = (await api.post("/ui/api/sessions")).json()
    run = await api.post(
        f"/ui/api/sessions/{session['session_id']}/work",
        json={"query": "TRIV3 공개 트랙", "domain_id": "triv3", "target_audience": "public"},
    )
    assert run.status_code == 201, run.text
    return session, run.json()


def v11_draft(draft_id="draft-ui-v11", version=1, content="UI 1.1 합성 초안") -> DraftBundleV11:
    evidence = {
        "source_id": "src-public-ui",
        "source_revision": "rev-1",
        "location": SourceLocation(uri="fixture://ui/public", section="s1"),
        "audience": Audience.PUBLIC,
        "content_hash": sha256_text("synthetic public evidence"),
    }
    fields = dict(
        request_id="req-ui",
        trace_id="trace-ui",
        run_id="run-ui",
        agent_id="assistant-supervisor",
        domain_id=DomainId.TRIV3,
        draft_id=draft_id,
        version=version,
        content_hash=sha256_text(content),
        target=DraftTarget(audience=Audience.PUBLIC),
        audience=Audience.PUBLIC,
        policy_version="local-v1",
        allowed_evidence=(EvidenceRef(**evidence),),
        sources=(SourceRevisionRef(**evidence, acl_revision="acl-1", policy_version="local-v1"),),
        policy_decision_id="policy-decision-ui",
        content=content,
        simulated=True,
        adapter="fixture",
    )
    provisional = DraftBundleV11.model_construct(**fields, payload_hash="0" * 64)
    return DraftBundleV11(**fields, payload_hash=provisional.calculated_payload_hash())


def decision(view: dict, value="approved") -> dict:
    return {
        "draft_version": view["version"],
        "content_hash": view["content_hash"],
        "payload_hash": view["payload_hash"],
        "target": view["target"],
        "decision": value,
    }


# ---------------------------------------------------------------- AC1
async def test_ac1_sessions_notes_query_pending_review_and_manual_approval(stack):
    async with browser(stack.ui) as api:
        page = await api.get("/ui/")
        assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
        assert "default-src 'none'" in page.headers["content-security-policy"]
        assert "local" in page.text and "not_run" in page.text

        status = (await api.get("/ui/api/status")).json()
        assert status["review"]["mode"] == "mock"
        assert status["review"]["authority"] == "reference_mock"
        assert status["features"]["manual_review"] == "enabled"
        assert status["features"]["mock_publication"] == "mock"
        for disabled in ("real_publication", "schedules", "team_role_execution", "openshell"):
            assert status["features"][disabled] == "disabled"
        assert status["features"]["experiments"] == "not_run"
        assert set(status["gates"].values()) == {"not_run"}

        await with_csrf(api)
        session, run = await pending_run(api)
        listed = (await api.get("/ui/api/sessions")).json()
        assert session["session_id"] in {item["session_id"] for item in listed}

        note = await api.post(
            "/ui/api/notes",
            json={"domain_id": "triv3", "title": "합성 노트", "content": "개인 메모 내용"},
            headers={"Idempotency-Key": "ui-note-1"},
        )
        assert note.status_code == 201
        assert note.json()["document"]["audience"] == "private"
        titles = {item["document"]["title"] for item in (await api.get("/ui/api/notes")).json()}
        assert "합성 노트" in titles

        assert run["status"] == "waiting_approval"
        assert run["review"]["decision"] == "pending" and run["simulated"] is True
        draft_id = run["draft"]["draft_id"]
        pending = (await api.get("/ui/api/reviews", params={"decision": "pending"})).json()
        view = next(item for item in pending if item["draft_id"] == draft_id)
        assert view["contract"] == "1.0" and view["mode"] == "mock"
        assert view["content"] == run["draft"]["content"]

        # Still pending until a human decides; refresh never fabricates approval.
        still = (await api.post(f"/ui/api/runs/{run['run_id']}/refresh-review")).json()
        assert still["status"] == "waiting_approval"

        approved = await api.post(
            f"/ui/api/reviews/{draft_id}/decision",
            json=decision(view),
            headers={"Idempotency-Key": "ui-approve-1"},
        )
        assert approved.status_code == 200
        assert approved.json()["decided_by"] == stack.owner_id
        done = (await api.post(f"/ui/api/runs/{run['run_id']}/refresh-review")).json()
        assert done["status"] == "completed" and done["review"]["decision"] == "approved"
        assert done["publication_status"] == "not_requested"
        detail = (await api.get(f"/ui/api/sessions/{session['session_id']}")).json()
        assert {r["run_id"]: r["status"] for r in detail["runs"]}[run["run_id"]] == "completed"
        assert (await api.get(f"/ui/api/runs/{run['run_id']}")).json()["status"] == "completed"

    # The UI keeps no state/DB of its own: only core and review stores exist.
    databases = {p.relative_to(stack.tmp_path).parts[0] for p in stack.tmp_path.rglob("*.db")}
    assert databases == {"core", "review"}
    assert not any("store" in name for name in vars(stack.ui.state).get("_state", {}))


# ---------------------------------------------------------------- AC2
async def test_ac2_rejects_foreign_origin_missing_csrf_host_and_forwarding(stack):
    async with browser(stack.ui) as api:
        await with_csrf(api)
        good_token = api.headers["X-RFA-CSRF"]
        attempts = {
            "no-csrf": await api.post("/ui/api/sessions", headers={"X-RFA-CSRF": ""}),
            "bad-csrf": await api.post("/ui/api/sessions", headers={"X-RFA-CSRF": "0" * 64}),
            "foreign-origin": await api.post(
                "/ui/api/sessions", headers={"Origin": "http://evil.example"}
            ),
            "null-origin": await api.post("/ui/api/sessions", headers={"Origin": "null"}),
            "cross-site": await api.post(
                "/ui/api/sessions", headers={"Sec-Fetch-Site": "cross-site"}
            ),
            "evil-host": await api.post("/ui/api/sessions", headers={"Host": "evil.example:8780"}),
            "forwarded": await api.post(
                "/ui/api/sessions", headers={"X-Forwarded-Host": "evil.example"}
            ),
            "text-plain": await api.post(
                "/ui/api/notes",
                content=b"{}",
                headers={"Content-Type": "text/plain", "Idempotency-Key": "n1"},
            ),
        }
    async with browser(stack.ui, origin=None) as no_origin:
        no_origin.cookies = api.cookies
        attempts["cookie-no-origin"] = await no_origin.post(
            "/ui/api/sessions", headers={"X-RFA-CSRF": good_token}
        )
    async with browser(stack.ui, origin=None) as scripted:
        attempts["script-no-origin"] = await scripted.post(
            "/ui/api/sessions", headers={"X-RFA-CSRF": good_token}
        )
    async with browser(stack.ui) as other_browser:
        # A token minted for a different cookie is useless without that cookie.
        attempts["token-without-cookie"] = await other_browser.post(
            "/ui/api/sessions", headers={"X-RFA-CSRF": good_token}
        )
    async with browser(
        stack.ui, base="http://localhost:8780", origin="http://localhost:8780"
    ) as alias:
        attempts["unlisted-alias"] = await alias.get("/ui/api/sessions")
    expected = {
        "no-csrf": 403,
        "bad-csrf": 403,
        "foreign-origin": 403,
        "null-origin": 403,
        "cross-site": 403,
        "evil-host": 400,
        "forwarded": 400,
        "text-plain": 415,
        "cookie-no-origin": 403,
        "script-no-origin": 403,
        "token-without-cookie": 403,
        "unlisted-alias": 400,
    }
    assert {name: r.status_code for name, r in attempts.items()} == expected
    assert (
        await stack.container.service.sessions.list(
            await stack.container.repository.local_principal()
        )
        == []
    )


async def test_ac2_no_user_selectable_upstream_or_generic_proxy(stack):
    api_routes = {route.path for route in stack.ui.routes if hasattr(route, "path")}
    assert api_routes == FIXED_UI_ROUTES
    assert not any("url" in path.lower() or ":path" in path for path in api_routes)
    async with browser(stack.ui) as api:
        await with_csrf(api)
        for probe in (
            "/ui/api/proxy?url=http://example.com",
            "/ui/api/fetch?target=http://127.0.0.1:9",
            "/v1/sessions",
            "/ui/api/../v1/sessions",
        ):
            assert (await api.get(probe)).status_code == 404
        session = (await api.post("/ui/api/sessions")).json()
        smuggled = await api.post(
            f"/ui/api/sessions/{session['session_id']}/work",
            json={"query": "q", "base_url": "http://example.com", "upstream": "http://x"},
        )
        assert smuggled.status_code == 422
        redirected = await api.get("/ui/api/sessions", headers={"X-Upstream-Url": "http://x"})
        assert redirected.status_code == 200
    for bad in ("http://example.com", "http://10.0.0.5:8000", "http://0.0.0.0:8000"):
        with pytest.raises(BackendNotImplementedError):
            UpstreamTarget.in_process("core", stack.core_app, base_url=bad)
    with pytest.raises(ValueError):
        create_local_ui_app(
            allowed_hosts=["example.com:8780"],
            core=UpstreamTarget.in_process("core", stack.core_app, base_url=CORE_URL),
        )


async def test_ac2_untrusted_text_stays_data_and_tokens_never_reach_the_browser(stack):
    secret = stack.review_token.get_secret_value()
    seen_urls: list[str] = []

    async def record(request: httpx.Request) -> None:
        seen_urls.append(str(request.url))

    responses: list[httpx.Response] = []
    async with browser(stack.ui, event_hooks={"request": [record]}) as api:
        for path in ("/ui/", "/ui/static/app.js", "/ui/static/app.css", "/ui/api/status"):
            responses.append(await api.get(path))
        responses.append(await api.get("/ui/api/csrf"))
        await with_csrf(api)
        session, run = await pending_run(api)
        note = await api.post(
            "/ui/api/notes",
            json={"domain_id": "triv3", "title": MALICIOUS, "content": MALICIOUS},
            headers={"Idempotency-Key": "ui-note-xss"},
        )
        responses += [note, await api.get("/ui/api/notes"), await api.get("/ui/api/reviews")]
        responses.append(await api.get(f"/ui/api/sessions/{session['session_id']}"))
    assert note.status_code == 201
    assert note.json()["document"]["title"] == MALICIOUS
    for response in responses:
        assert secret not in response.text
        assert all(secret not in value for value in response.headers.values())
        assert response.headers["x-content-type-options"] == "nosniff"
        if response.url.path.startswith("/ui/api/"):
            assert response.headers["content-type"].startswith("application/json")
    assert all(secret not in url for url in seen_urls)
    assert all(secret not in cookie for cookie in api.cookies.values())

    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for forbidden in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
        "localStorage",
        "sessionStorage",
        "indexedDB",
        "Authorization",
        "Bearer",
        "http://",
        "https://",
    ):
        assert forbidden not in script, forbidden
    assert "textContent" in script
    assert re.findall(r"<script[^>]*>", html) == ['<script src="/ui/static/app.js" defer>']
    assert "<script src" in html and "</script>" in html
    assert not re.search(r"\son[a-z]+\s*=", html) and "style=" not in html


# ---------------------------------------------------------------- AC3
def failing_review(handler) -> UpstreamTarget:
    return UpstreamTarget(
        name="review",
        transport=httpx.MockTransport(handler),
        base_url=f"http://{REVIEW_HOST}",
        token=SecretStr("r" * 40),
    )


async def test_ac3_timeouts_and_failures_require_query_and_are_not_retried(stack):
    calls: list[str] = []

    async def timeout(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    ui = stack.ui_with(failing_review(timeout))
    body = {
        "draft_version": 1,
        "content_hash": "a" * 64,
        "target": {"audience": "public"},
        "decision": "approved",
    }
    async with browser(ui) as api:
        await with_csrf(api)
        decided = await api.post(
            "/ui/api/reviews/draft-x/decision", json=body, headers={"Idempotency-Key": "d-1"}
        )
        assert decided.status_code == 504
        assert decided.json()["code"] == "outcome_unknown"
        assert decided.json()["details"] == {"query_required": True, "upstream": "review"}
        assert calls == ["POST"]
        listed = await api.get("/ui/api/reviews")
        assert listed.status_code == 504 and listed.json()["code"] == "upstream_timeout"
        assert listed.json()["details"]["query_required"] is True
        status = (await api.get("/ui/api/status")).json()
        assert status["review"]["reachable"] is False
        assert status["features"]["manual_review"] == "disabled"
        assert status["features"]["mock_publication"] == "disabled"
    assert calls == ["POST", "GET", "GET"]

    async def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"code": "internal", "message": "UPSTREAM-CANARY-77"})

    async with browser(stack.ui_with(failing_review(broken))) as api:
        await with_csrf(api)
        publish = await api.post(
            "/ui/api/publications",
            json={
                "approval_id": "approval_1",
                "draft_id": "draft-x",
                "version": 1,
                "payload_hash": "b" * 64,
            },
            headers={"Idempotency-Key": "p-1"},
        )
        assert publish.status_code == 502 and publish.json()["code"] == "outcome_unknown"
        assert "UPSTREAM-CANARY-77" not in publish.text and "internal" not in publish.text

    async def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic refused", request=request)

    async with browser(stack.ui_with(failing_review(unreachable))) as api:
        gone = await api.get("/ui/api/reviews")
        assert gone.status_code == 502 and gone.json()["code"] == "upstream_unavailable"


async def test_ac3_revoked_review_permission_is_a_safe_error(stack):
    revoked = UpstreamTarget.in_process(
        "review",
        stack.review_app,
        base_url=f"http://{REVIEW_HOST}",
        token=SecretStr(secrets.token_urlsafe(32)),
    )
    ui = stack.ui_with(revoked)
    async with browser(ui) as api:
        listed = await api.get("/ui/api/reviews")
        assert listed.status_code == 502 and listed.json()["code"] == "upstream_auth_failed"
        status = (await api.get("/ui/api/status")).json()
        assert status["review"] == {
            "configured": True,
            "reachable": False,
            "error": "upstream_auth_failed",
        }
        assert status["features"]["manual_review"] == "disabled"
    no_review = stack.ui_with(None)
    async with browser(no_review) as api:
        missing = await api.get("/ui/api/reviews")
        assert missing.status_code == 503 and missing.json()["code"] == "review_not_configured"
        assert (await api.get("/ui/api/status")).json()["review"] == {"configured": False}


async def test_ac3_changed_approval_rejection_and_unknown_publication_never_assume_success(stack):
    token = stack.review_token.get_secret_value()
    direct = {"Authorization": f"Bearer {token}"}
    v1 = v11_draft()
    async with browser(stack.ui) as api:
        await with_csrf(api)
        _, rejected_run = await pending_run(api)
        rejected_view = (
            await api.get(f"/ui/api/reviews/{rejected_run['draft']['draft_id']}")
        ).json()
        await api.post(
            f"/ui/api/reviews/{rejected_view['draft_id']}/decision",
            json=decision(rejected_view, "rejected"),
            headers={"Idempotency-Key": "ui-reject"},
        )
        failed = (await api.post(f"/ui/api/runs/{rejected_run['run_id']}/refresh-review")).json()
        assert failed["status"] == "failed" and failed["review"]["decision"] == "rejected"

        await stack.review_http.post(
            "/v1/local/reviews",
            json={"draft": v1.model_dump(mode="json")},
            headers={**direct, "Idempotency-Key": "direct-v1"},
        )
        view = (await api.get(f"/ui/api/reviews/{v1.draft_id}")).json()
        approval = (
            await api.post(
                f"/ui/api/reviews/{v1.draft_id}/decision",
                json=decision(view),
                headers={"Idempotency-Key": "ui-approve-v1"},
            )
        ).json()["approval"]
        publication = {
            "approval_id": approval["approval_id"],
            "draft_id": v1.draft_id,
            "version": 1,
            "payload_hash": v1.payload_hash,
            "simulate_outcome": "outcome_unknown",
        }
        unknown = await api.post(
            "/ui/api/publications", json=publication, headers={"Idempotency-Key": "ui-pub-1"}
        )
        receipt = unknown.json()["receipt"]
        assert unknown.json()["simulated"] is True and receipt["mode"] == "mock"
        assert receipt["status"] == "outcome_unknown" and receipt["next_action"] == "query"
        retry = await api.post(
            "/ui/api/publications",
            json={**publication, "simulate_outcome": "succeeded"},
            headers={"Idempotency-Key": "ui-pub-2"},
        )
        assert retry.status_code == 409
        assert retry.json()["details"]["upstream_code"] == "publication_outcome_unknown"
        queried = (await api.get(f"/ui/api/publications/{receipt['publication_id']}")).json()
        assert queried["receipt"]["status"] == "outcome_unknown"

        v2 = v11_draft(version=2, content="UI 1.1 합성 초안 v2")
        await stack.review_http.post(
            "/v1/local/reviews",
            json={"draft": v2.model_dump(mode="json")},
            headers={**direct, "Idempotency-Key": "direct-v2"},
        )
        stale = await api.post(
            f"/ui/api/reviews/{v1.draft_id}/decision",
            json=decision(view),
            headers={"Idempotency-Key": "ui-approve-stale"},
        )
        assert stale.status_code == 409
        assert stale.json()["details"]["upstream_code"] == "stale_draft_version"
        current = (await api.get(f"/ui/api/reviews/{v1.draft_id}")).json()
        assert current["version"] == 2 and current["decision"] == "pending"
        assert current["approval"] is None
    counts = stack.review_app.state.local_response_store.counts()
    assert counts["publications"] == 1
