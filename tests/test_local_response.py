"""P1-008C: local manual review / mock publication / READ tool stand-in.

All fixtures are synthetic, DBs live in tmp_path, transport is in-process ASGI,
and an autouse guard fails the test on any socket connect or subprocess spawn.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import socket
import sqlite3
import stat
import subprocess
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

from rfa_mas.adapters.http import ReferenceHttpClient, ResponseHttpAdapter, ToolHttpAdapter
from rfa_mas.contracts import (
    Audience,
    DomainId,
    DraftBundle,
    DraftBundleV11,
    DraftTarget,
    EvidenceRef,
    ExecutionMode,
    PublicationStatus,
    ResultStatus,
    ReviewDecision,
    ReviewStatus,
    SimulationScenario,
    SourceLocation,
    SourceRevisionRef,
    ToolEffect,
    ToolRequest,
    sha256_text,
)
from rfa_mas.errors import RfaError
from rfa_mas.reference import local_response
from rfa_mas.reference.local_response import create_local_response_app
from rfa_mas.reference.local_security import LocalServiceBoundary, LocalServiceError

HOST = "127.0.0.1:8781"
BASE = f"http://{HOST}"
OWNER = "installation-owner-1"
CANARY = "PRIVATE-CANARY-9d41c7"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


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


@pytest.fixture
def token() -> SecretStr:
    return SecretStr(secrets.token_urlsafe(32))


@pytest.fixture
def boundary(token) -> LocalServiceBoundary:
    return LocalServiceBoundary.create(owner_id=OWNER, service_token=token, allowed_hosts=[HOST])


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "local-response" / "response.db"


@pytest.fixture
def make_app(db_path, boundary, clock):
    def factory(**kwargs):
        return create_local_response_app(db_path=db_path, boundary=boundary, clock=clock, **kwargs)

    return factory


def client(app, token: SecretStr | None, *, peer=("127.0.0.1", 50123), base=BASE, **kwargs):
    headers = {"Authorization": f"Bearer {token.get_secret_value()}"} if token else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=peer),
        base_url=base,
        headers=headers,
        **kwargs,
    )


def legacy_draft(draft_id="draft-legacy-1", version=1, content="합성 공개 초안 v1") -> DraftBundle:
    return DraftBundle(
        request_id="req-legacy",
        trace_id="trace-legacy",
        run_id="run-legacy",
        agent_id="assistant-supervisor",
        domain_id=DomainId.TRIV3,
        draft_id=draft_id,
        version=version,
        content_hash=sha256_text(content),
        target=DraftTarget(audience=Audience.PUBLIC),
        audience=Audience.PUBLIC,
        policy_version="local-v1",
        allowed_evidence=(),
        content=content,
        simulated=True,
        adapter="fixture",
    )


def v11_draft(draft_id="draft-v11-1", version=1, content="합성 공개 1.1 초안 v1") -> DraftBundleV11:
    location = SourceLocation(uri="fixture://local-response/public", section="s1")
    evidence = {
        "source_id": "src-public-1",
        "source_revision": "rev-1",
        "location": location,
        "audience": Audience.PUBLIC,
        "content_hash": sha256_text("synthetic public evidence"),
    }
    fields = dict(
        request_id="req-v11",
        trace_id="trace-v11",
        run_id="run-v11",
        agent_id="assistant-supervisor",
        domain_id=DomainId.TRIV3,
        draft_id=draft_id,
        version=version,
        content_hash=sha256_text(content),
        target=DraftTarget(
            audience=Audience.PUBLIC, channel="preview", destination="local-preview"
        ),
        audience=Audience.PUBLIC,
        policy_version="local-v1",
        allowed_evidence=(EvidenceRef(**evidence),),
        sources=(SourceRevisionRef(**evidence, acl_revision="acl-1", policy_version="local-v1"),),
        policy_decision_id="policy-decision-1",
        content=content,
        simulated=True,
        adapter="fixture",
    )
    provisional = DraftBundleV11.model_construct(**fields, payload_hash="0" * 64)
    return DraftBundleV11(**fields, payload_hash=provisional.calculated_payload_hash())


def decision_body(draft: DraftBundle | DraftBundleV11, decision="approved") -> dict:
    return {
        "draft_version": draft.version,
        "content_hash": draft.content_hash,
        "payload_hash": getattr(draft, "payload_hash", None),
        "target": draft.target.model_dump(mode="json"),
        "decision": decision,
    }


def publish_body(approval: dict, draft: DraftBundleV11, outcome="succeeded") -> dict:
    return {
        "approval_id": approval["approval_id"],
        "draft_id": draft.draft_id,
        "version": draft.version,
        "payload_hash": draft.payload_hash,
        "simulate_outcome": outcome,
    }


def tool_request(name="synthetic.glossary_lookup", effect=ToolEffect.READ, key="tool-1", **args):
    return ToolRequest(
        request_id="req-tool",
        trace_id="trace-tool",
        run_id="run-tool",
        agent_id="assistant-supervisor",
        domain_id=DomainId.TRIV3,
        idempotency_key=key,
        tool_name=name,
        effect=effect,
        arguments=args or {"term": "draft"},
    )


async def submit_v11(api, draft, key="submit-v11-1"):
    response = await api.post(
        "/v1/local/reviews",
        json={"draft": draft.model_dump(mode="json")},
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def decide(api, draft, decision="approved", key="decide-1"):
    return await api.post(
        f"/v1/local/reviews/{draft.draft_id}/decision",
        json=decision_body(draft, decision),
        headers={"Idempotency-Key": key},
    )


# ---------------------------------------------------------------- AC1
@pytest.mark.parametrize("scenario", list(SimulationScenario))
async def test_ac1_legacy_submit_stays_pending_for_every_scenario(make_app, token, scenario):
    app = make_app()
    async with client(app, token) as api:
        response = await api.post(
            "/v1/reviews",
            json={
                "draft": legacy_draft().model_dump(mode="json"),
                "simulation_scenario": scenario.value,
            },
            headers={"Idempotency-Key": f"legacy-{scenario.value}"},
        )
        assert response.status_code == 200
        decision = ReviewDecision.model_validate(response.json())
        assert decision.decision == ReviewStatus.PENDING
        assert decision.publication_status == PublicationStatus.NOT_REQUESTED
        assert decision.simulated is True
        fetched = await api.get("/v1/reviews/draft-legacy-1")
        assert fetched.json()["decision"] == "pending"


async def test_ac1_manual_decisions_use_server_owner_not_body_or_header_claims(make_app, token):
    app = make_app()
    legacy = legacy_draft()
    approve, reject, revise = (v11_draft(f"draft-v11-{n}") for n in ("a", "r", "v"))
    async with client(app, token) as api:
        await api.post(
            "/v1/reviews",
            json={"draft": legacy.model_dump(mode="json")},
            headers={"Idempotency-Key": "legacy-1"},
        )
        forged = await api.post(
            f"/v1/local/reviews/{legacy.draft_id}/decision",
            json={**decision_body(legacy), "approver_id": "attacker", "authority": "real"},
            headers={"Idempotency-Key": "forged-1"},
        )
        assert forged.status_code == 422
        assert (await api.get(f"/v1/reviews/{legacy.draft_id}")).json()["decision"] == "pending"

        response = await api.post(
            f"/v1/local/reviews/{legacy.draft_id}/decision",
            json=decision_body(legacy),
            headers={"Idempotency-Key": "legacy-decide", "X-User-Id": "attacker"},
        )
        assert response.status_code == 200
        assert response.json()["decided_by"] == OWNER
        assert (await api.get(f"/v1/reviews/{legacy.draft_id}")).json()["decision"] == "approved"

        for draft, decision in (
            (approve, "approved"),
            (reject, "rejected"),
            (revise, "revision_requested"),
        ):
            await submit_v11(api, draft, key=f"submit-{draft.draft_id}")
            view = (await decide(api, draft, decision, key=f"decide-{draft.draft_id}")).json()
            assert view["decision"] == decision and view["decided_by"] == OWNER
            approval = view["approval"]
            assert approval["approver_id"] == OWNER
            assert approval["decision"] == decision
            assert approval["mode"] == "mock" and approval["authority"] == "reference_mock"
            assert approval["binding"] == draft.binding().model_dump(mode="json")
            assert (await api.get(f"/v1/local/reviews/{draft.draft_id}")).json() == view
        listed = (await api.get("/v1/local/reviews", params={"decision": "approved"})).json()
        assert {item["draft_id"] for item in listed} == {legacy.draft_id, approve.draft_id}


def _forged_cases(token: SecretStr):
    good = f"Bearer {token.get_secret_value()}"
    return [
        ("missing-token", {}, {}, 401),
        ("wrong-token", {"Authorization": "Bearer " + "w" * 40}, {}, 401),
        ("basic-token", {"Authorization": "Basic " + token.get_secret_value()}, {}, 401),
        ("foreign-host", {"Authorization": good, "Host": "evil.example:8781"}, {}, 400),
        ("wrong-port", {"Authorization": good}, {"base": "http://127.0.0.1:9999"}, 400),
        ("unlisted-alias", {"Authorization": good}, {"base": "http://localhost:8781"}, 400),
        ("foreign-origin", {"Authorization": good, "Origin": "http://evil.example"}, {}, 403),
        ("null-origin", {"Authorization": good, "Origin": "null"}, {}, 403),
        ("port-origin", {"Authorization": good, "Origin": "http://127.0.0.1:9999"}, {}, 403),
        ("forwarded", {"Authorization": good, "Forwarded": "for=127.0.0.1"}, {}, 400),
        ("x-forwarded-for", {"Authorization": good, "X-Forwarded-For": "127.0.0.1"}, {}, 400),
        ("x-forwarded-host", {"Authorization": good, "X-Forwarded-Host": HOST}, {}, 400),
        ("x-real-ip", {"Authorization": good, "X-Real-IP": "127.0.0.1"}, {}, 400),
        ("remote-peer", {"Authorization": good}, {"peer": ("203.0.113.9", 4444)}, 403),
        ("cross-site", {"Authorization": good, "Sec-Fetch-Site": "cross-site"}, {}, 403),
        ("browser-no-origin", {"Authorization": good, "Sec-Fetch-Mode": "cors"}, {}, 403),
        ("cookie-no-origin", {"Authorization": good, "Cookie": "session=1"}, {}, 403),
    ]


async def test_ac1_boundary_rejects_forged_identity_host_origin_forwarded(make_app, token, db_path):
    app = make_app()
    store = app.state.local_response_store
    for name, headers, options, expected in _forged_cases(token):
        async with client(
            app,
            None,
            peer=options.get("peer", ("127.0.0.1", 50123)),
            base=options.get("base", BASE),
        ) as api:
            response = await api.post(
                "/v1/reviews",
                json={"draft": legacy_draft().model_dump(mode="json")},
                headers={"Idempotency-Key": f"forged-{name}", **headers},
            )
        assert response.status_code == expected, name
        assert response.headers["cache-control"] == "no-store"
        assert store.counts()["drafts"] == 0, name
    async with client(app, token) as api:
        plain = await api.post(
            "/v1/reviews",
            content=b"{}",
            headers={"Idempotency-Key": "plain", "Content-Type": "text/plain"},
        )
        assert plain.status_code == 415
        same_origin = await api.post(
            "/v1/reviews",
            json={"draft": legacy_draft().model_dump(mode="json")},
            headers={
                "Idempotency-Key": "same-origin",
                "Origin": BASE,
                "Sec-Fetch-Site": "same-origin",
            },
        )
        assert same_origin.status_code == 200
    assert store.counts()["drafts"] == 1


async def test_ac1_errors_and_logs_never_echo_body_token_or_canary(make_app, token, caplog):
    caplog.set_level(logging.DEBUG)
    app = make_app()
    secret = token.get_secret_value()
    draft = legacy_draft(content=f"본문 {CANARY}")
    responses = []
    async with client(app, token) as api:
        responses.append(
            await api.post(
                "/v1/reviews",
                json={"draft": {**draft.model_dump(mode="json"), CANARY: CANARY}},
                headers={"Idempotency-Key": "echo-1"},
            )
        )
        responses.append(
            await api.post(
                "/v1/reviews",
                content=("{" + CANARY).encode(),
                headers={"Idempotency-Key": "echo-2", "Content-Type": "application/json"},
            )
        )
        responses.append(
            await api.post(
                "/v1/reviews",
                json={"draft": draft.model_dump(mode="json")},
                headers={"Idempotency-Key": f"bad key {CANARY}"},
            )
        )
        responses.append(await api.get(f"/v1/reviews/{CANARY}!"))
        responses.append(await api.get(f"/v1/local/publications/{CANARY}"))
        responses.append(
            await api.post(
                "/v1/local/reviews",
                json={"draft": {"content": CANARY, "token": secret}},
                headers={"Idempotency-Key": "echo-3"},
            )
        )
    async with client(app, None) as api:
        responses.append(
            await api.get("/v1/capabilities", headers={"Authorization": f"Bearer {CANARY}{secret}"})
        )
        responses.append(await api.get("/healthz", headers={"Host": f"{CANARY}.example"}))
        responses.append(
            await api.get(
                "/v1/capabilities",
                headers={"Authorization": f"Bearer {secret}", "Origin": f"http://{CANARY}"},
            )
        )
    assert [r.status_code for r in responses] == [422, 422, 400, 404, 404, 422, 401, 400, 403]
    for response in responses:
        assert CANARY not in response.text and secret not in response.text
        assert set(response.json()) >= {"code", "message"}
    # The test's own httpx client logs the URL it chose; only service-side logs count.
    service_logs = "\n".join(
        record.getMessage() for record in caplog.records if not record.name.startswith("httpx")
    )
    assert CANARY not in service_logs and secret not in service_logs
    assert secret not in repr(app.state.local_response_store.path)
    assert secret not in repr(
        LocalServiceBoundary.create(owner_id=OWNER, service_token=token, allowed_hosts=[HOST])
    )


async def test_ac1_tools_execute_only_documented_synthetic_reads(make_app, token, monkeypatch):
    calls: list[str] = []
    for name, (model, handler) in list(local_response.READ_TOOLS.items()):

        def counted(args, handler=handler, name=name):
            calls.append(name)
            return handler(args)

        monkeypatch.setitem(local_response.READ_TOOLS, name, (model, counted))
    app = make_app()
    async with client(app, token) as api:

        async def run(request: ToolRequest):
            response = await api.post(
                "/v1/tools/execute",
                json=request.model_dump(mode="json"),
                headers={"Idempotency-Key": request.idempotency_key},
            )
            assert response.status_code == 200
            return response.json()

        ok = await run(tool_request(key="read-ok"))
        assert ok["status"] == "succeeded" and ok["simulated"] is True
        assert ok["output"] == {
            "found": True,
            "definition": ok["output"]["definition"],
            "synthetic": True,
        }
        stats = await run(tool_request("synthetic.text_stats", key="stats", text=f"a b\n{CANARY}"))
        assert stats["output"] == {
            "characters": len(f"a b\n{CANARY}"),
            "lines": 2,
            "words": 3,
            "synthetic": True,
        }
        assert CANARY not in str(stats)
        replay = await run(tool_request(key="read-ok"))
        assert replay == ok
        dangerous = [
            tool_request("shell.exec", key="shell", cmd=f"rm -rf / {CANARY}"),
            tool_request("http.get", key="url", url=f"http://example.com/{CANARY}"),
            tool_request("file.read", key="file", path=f"/etc/{CANARY}"),
            tool_request("mcp.call", key="mcp", server=CANARY),
        ]
        for request in dangerous:
            denied = await run(request)
            assert denied["status"] == "denied" and denied["error"]["code"] == "tool_not_allowed"
            assert denied["output"] == {} and CANARY not in str(denied)
        write = await run(tool_request(effect=ToolEffect.WRITE, key="write"))
        assert write["status"] == "denied" and write["error"]["code"] == "external_writes_disabled"
        smuggled = await run(tool_request(key="smuggle", term="draft", url=f"http://{CANARY}"))
        assert smuggled["error"]["code"] == "invalid_tool_arguments" and CANARY not in str(smuggled)
    assert calls == ["synthetic.glossary_lookup", "synthetic.text_stats"]


# ---------------------------------------------------------------- AC2
async def test_ac2_decision_binds_latest_exact_version_and_new_version_invalidates(make_app, token):
    app = make_app()
    v1 = v11_draft(version=1, content="v1 본문")
    v2 = v11_draft(version=2, content="v2 본문")
    async with client(app, token) as api:
        await submit_v11(api, v1, key="submit-v1")
        for field, value in (
            ("payload_hash", "a" * 64),
            ("content_hash", "b" * 64),
            ("target", {"audience": "public", "channel": "preview", "destination": "elsewhere"}),
        ):
            mismatch = await api.post(
                f"/v1/local/reviews/{v1.draft_id}/decision",
                json={**decision_body(v1), field: value},
                headers={"Idempotency-Key": f"mismatch-{field}"},
            )
            assert mismatch.status_code == 409 and mismatch.json()["code"] == "binding_mismatch"
        approved = (await decide(api, v1, key="approve-v1")).json()
        first_approval = approved["approval"]
        assert first_approval["binding"]["version"] == 1

        await submit_v11(api, v2, key="submit-v2")
        stale = await publish(api, first_approval, v1, key="publish-old")
        assert stale.status_code == 409 and stale.json()["code"] == "approval_superseded"
        old_decision = await decide(api, v1, key="approve-v1-again")
        assert (
            old_decision.status_code == 409 and old_decision.json()["code"] == "stale_draft_version"
        )
        view = (await api.get(f"/v1/local/reviews/{v1.draft_id}")).json()
        assert view["version"] == 2 and view["decision"] == "pending" and view["approval"] is None

        downgrade = await api.post(
            "/v1/local/reviews",
            json={"draft": v11_draft(version=1, content="다른 v1").model_dump(mode="json")},
            headers={"Idempotency-Key": "downgrade"},
        )
        assert downgrade.status_code == 409 and downgrade.json()["code"] == "stale_draft_version"
        same_version = await api.post(
            "/v1/local/reviews",
            json={"draft": v11_draft(version=2, content="다른 v2").model_dump(mode="json")},
            headers={"Idempotency-Key": "same-version"},
        )
        assert same_version.status_code == 409
        assert same_version.json()["code"] == "draft_version_conflict"

        second = (await decide(api, v2, key="approve-v2")).json()["approval"]
        receipt = (await publish(api, second, v2, key="publish-v2")).json()
        assert receipt["receipt"]["binding"] == v2.binding().model_dump(mode="json")


async def publish(api, approval, draft, *, key, outcome="succeeded"):
    return await api.post(
        "/v1/local/publications",
        json=publish_body(approval, draft, outcome),
        headers={"Idempotency-Key": key},
    )


async def test_ac2_mock_publication_requires_stored_current_unexpired_approval(
    make_app, token, clock
):
    app = make_app(approval_ttl=timedelta(minutes=10))
    approved, rejected, revised, expiring = (v11_draft(f"draft-pub-{n}") for n in "arve")
    async with client(app, token) as api:
        missing = await publish(api, {"approval_id": "approval_does_not_exist"}, approved, key="p0")
        assert missing.status_code == 404 and missing.json()["code"] == "approval_not_found"

        approvals = {}
        for draft, decision in (
            (approved, "approved"),
            (rejected, "rejected"),
            (revised, "revision_requested"),
            (expiring, "approved"),
        ):
            await submit_v11(api, draft, key=f"s-{draft.draft_id}")
            approvals[draft.draft_id] = (
                await decide(api, draft, decision, key=f"d-{draft.draft_id}")
            ).json()["approval"]
        for draft in (rejected, revised):
            refused = await publish(
                api, approvals[draft.draft_id], draft, key=f"p-{draft.draft_id}"
            )
            assert refused.status_code == 409 and refused.json()["code"] == "approval_not_granted"

        wrong_draft = await publish(api, approvals[approved.draft_id], expiring, key="p-cross")
        assert wrong_draft.json()["code"] == "approval_binding_mismatch"
        wrong_payload = await api.post(
            "/v1/local/publications",
            json={**publish_body(approvals[approved.draft_id], approved), "payload_hash": "c" * 64},
            headers={"Idempotency-Key": "p-payload"},
        )
        assert wrong_payload.status_code == 409
        assert wrong_payload.json()["code"] == "approval_binding_mismatch"

        ok = await publish(api, approvals[approved.draft_id], approved, key="p-ok")
        assert ok.status_code == 200
        view = ok.json()
        receipt = view["receipt"]
        assert receipt["mode"] == ExecutionMode.MOCK.value and receipt["status"] == "succeeded"
        assert receipt["external_result_ref"] == f"local-artifact:{receipt['publication_id']}"
        assert receipt["approval_id"] == approvals[approved.draft_id]["approval_id"]
        assert receipt["binding"] == approved.binding().model_dump(mode="json")
        assert view["artifact_kind"] == "local-artifact" and view["authority"] == "reference_mock"
        assert view["external_write_performed"] is False and view["simulated"] is True
        again = await publish(api, approvals[approved.draft_id], approved, key="p-again")
        assert again.status_code == 409 and again.json()["code"] == "already_published"

        clock.now += timedelta(minutes=11)
        expired = await publish(api, approvals[expiring.draft_id], expiring, key="p-expired")
        assert expired.status_code == 409 and expired.json()["code"] == "approval_expired"
    assert app.state.local_response_store.counts()["publications"] == 1


# ---------------------------------------------------------------- AC3
async def test_ac3_restart_preserves_state_and_outcome_unknown_is_never_republished(
    make_app, token, db_path
):
    legacy = legacy_draft()
    draft = v11_draft()
    first = make_app()
    async with client(first, token) as api:
        await api.post(
            "/v1/reviews",
            json={"draft": legacy.model_dump(mode="json")},
            headers={"Idempotency-Key": "legacy-restart"},
        )
        await submit_v11(api, draft, key="v11-restart")
        approval = (await decide(api, draft, key="approve-restart")).json()["approval"]
        unknown = (
            await publish(api, approval, draft, key="pub-unknown", outcome="outcome_unknown")
        ).json()
    assert unknown["receipt"]["status"] == "outcome_unknown"
    assert unknown["receipt"]["next_action"] == "query"
    assert unknown["receipt"]["external_result_ref"] is None
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(db_path.parent.stat().st_mode) == 0o700

    restarted = make_app()
    async with client(restarted, token) as api:
        assert (await api.get(f"/v1/reviews/{legacy.draft_id}")).json()["decision"] == "pending"
        view = (await api.get(f"/v1/local/reviews/{draft.draft_id}")).json()
        assert view["decision"] == "approved" and view["approval"] == approval
        publication_id = unknown["receipt"]["publication_id"]
        assert (await api.get(f"/v1/local/publications/{publication_id}")).json() == unknown
        republish = await publish(api, approval, draft, key="pub-retry", outcome="succeeded")
        assert republish.status_code == 409
        assert republish.json()["code"] == "publication_outcome_unknown"
        replay = await publish(api, approval, draft, key="pub-unknown", outcome="outcome_unknown")
        assert replay.status_code == 200 and replay.json() == unknown
        assert (await api.get(f"/v1/local/publications/{publication_id}")).json() == unknown
    assert restarted.state.local_response_store.counts() == {
        "drafts": 2,
        "submissions": 2,
        "decisions": 1,
        "publications": 1,
        "tool_calls": 0,
    }


async def test_ac3_concurrent_duplicates_execute_once(make_app, token):
    app = make_app()
    draft = v11_draft()
    store = app.state.local_response_store
    async with client(app, token) as api:
        submits = await asyncio.gather(
            *(
                api.post(
                    "/v1/local/reviews",
                    json={"draft": draft.model_dump(mode="json")},
                    headers={"Idempotency-Key": "concurrent-submit"},
                )
                for _ in range(12)
            )
        )
        assert {r.status_code for r in submits} == {200}
        assert len({r.text for r in submits}) == 1
        assert store.counts()["drafts"] == 1 and store.counts()["submissions"] == 1

        decisions = await asyncio.gather(
            *(decide(api, draft, key=f"concurrent-decide-{n}") for n in range(12))
        )
        assert sorted(r.status_code for r in decisions) == [200] + [409] * 11
        approval = next(r.json() for r in decisions if r.status_code == 200)["approval"]
        assert store.counts()["decisions"] == 1

        same_key = await asyncio.gather(
            *(publish(api, approval, draft, key="concurrent-publish") for _ in range(12))
        )
        assert {r.status_code for r in same_key} == {200} and len({r.text for r in same_key}) == 1
        other_keys = await asyncio.gather(
            *(publish(api, approval, draft, key=f"concurrent-publish-{n}") for n in range(12))
        )
        assert {r.status_code for r in other_keys} == {409}
    assert store.counts()["publications"] == 1


async def test_ac3_same_key_with_different_payload_conflicts(make_app, token):
    app = make_app()
    draft = v11_draft()
    async with client(app, token) as api:
        first = await api.post(
            "/v1/reviews",
            json={"draft": legacy_draft().model_dump(mode="json")},
            headers={"Idempotency-Key": "shared-key"},
        )
        assert first.status_code == 200
        changed = await api.post(
            "/v1/reviews",
            json={"draft": legacy_draft(content="다른 본문").model_dump(mode="json")},
            headers={"Idempotency-Key": "shared-key"},
        )
        assert changed.status_code == 409 and changed.json()["code"] == "idempotency_conflict"
        cross_contract = await api.post(
            "/v1/local/reviews",
            json={"draft": draft.model_dump(mode="json")},
            headers={"Idempotency-Key": "shared-key"},
        )
        assert cross_contract.status_code == 409

        await submit_v11(api, draft, key="submit-conflict")
        assert (await decide(api, draft, "rejected", key="decide-conflict")).status_code == 200
        flipped = await decide(api, draft, "approved", key="decide-conflict")
        assert flipped.status_code == 409 and flipped.json()["code"] == "idempotency_conflict"

        other = v11_draft("draft-conflict-2")
        await submit_v11(api, other, key="submit-conflict-2")
        approval = (await decide(api, other, key="decide-conflict-2")).json()["approval"]
        await publish(api, approval, other, key="publish-conflict")
        changed_publish = await publish(
            api, approval, other, key="publish-conflict", outcome="outcome_unknown"
        )
        assert changed_publish.json()["code"] == "idempotency_conflict"

        tool = tool_request(key="tool-conflict")
        assert (
            await api.post("/v1/tools/execute", json=tool.model_dump(mode="json"))
        ).status_code == 200
        changed_tool = tool_request(key="tool-conflict", term="approval")
        conflict = await api.post("/v1/tools/execute", json=changed_tool.model_dump(mode="json"))
        assert conflict.status_code == 409


def test_ac3_refuses_core_or_foreign_database_and_symlink(tmp_path, boundary, clock):
    core = tmp_path / "core.db"
    with sqlite3.connect(core) as connection:
        connection.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY)")
    core.chmod(0o644)
    with pytest.raises(LocalServiceError) as excinfo:
        create_local_response_app(db_path=core, boundary=boundary, clock=clock)
    assert excinfo.value.code == "configuration_error"
    connection = sqlite3.connect(core)
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        journal = connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()
    assert tables == {"runs"} and journal == "delete"
    assert stat.S_IMODE(core.stat().st_mode) == 0o644

    other_service = tmp_path / "runtime.db"
    with sqlite3.connect(other_service) as connection:
        connection.execute("CREATE TABLE service_meta (key TEXT PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO service_meta VALUES ('service', 'rfa-local-runtime')")
    with pytest.raises(LocalServiceError):
        create_local_response_app(db_path=other_service, boundary=boundary, clock=clock)

    target = tmp_path / "real.db"
    target.touch()
    link = tmp_path / "link.db"
    link.symlink_to(target)
    with pytest.raises(LocalServiceError):
        create_local_response_app(db_path=link, boundary=boundary, clock=clock)


# ---------------------------------------------------------------- AC4
async def test_ac4_response_http_adapter_pending_get_and_manual_approval(make_app, token):
    app = make_app()
    draft = legacy_draft()
    async with client(app, None) as http:
        adapter = ResponseHttpAdapter(
            ReferenceHttpClient(http, token=token, max_read_retries=1), simulated=True
        )
        decision = await adapter.submit_draft(
            draft,
            idempotency_key="run-1:work:review",
            simulation_scenario=SimulationScenario.SUCCESS,
        )
        assert isinstance(decision, ReviewDecision)
        assert decision.decision == ReviewStatus.PENDING and decision.simulated is True
        assert (
            decision.draft_id,
            decision.draft_version,
            decision.content_hash,
            decision.target,
        ) == (
            draft.draft_id,
            draft.version,
            draft.content_hash,
            draft.target,
        )
        assert (decision.request_id, decision.trace_id, decision.run_id) == (
            draft.request_id,
            draft.trace_id,
            draft.run_id,
        )
        assert (await adapter.get_decision(draft.draft_id)).decision == ReviewStatus.PENDING
        assert await adapter.get_decision("draft-unknown") is None

        async with client(app, token) as reviewer:
            assert (await decide(reviewer, draft, key="adapter-approve")).status_code == 200
        approved = await adapter.get_decision(draft.draft_id)
        assert approved.decision == ReviewStatus.APPROVED
        assert approved.publication_status == PublicationStatus.NOT_REQUESTED
        replay = await adapter.submit_draft(draft, idempotency_key="run-1:work:review")
        assert replay.decision == ReviewStatus.PENDING

        forged = ResponseHttpAdapter(
            ReferenceHttpClient(http, token=SecretStr("f" * 40), max_read_retries=0)
        )
        with pytest.raises(RfaError) as excinfo:
            await forged.get_decision(draft.draft_id)
        assert "401" in excinfo.value.safe_message


async def test_ac4_tool_http_adapter_read_allowlist_and_write_block(make_app, token):
    app = make_app()
    seen: list[str] = []

    async def record(request: httpx.Request) -> None:
        seen.append(request.url.path)

    async with client(app, None, event_hooks={"request": [record]}) as http:
        adapter = ToolHttpAdapter(ReferenceHttpClient(http, token=token, max_read_retries=1))
        read = await adapter.execute(tool_request(key="adapter-read"))
        assert read.status == ResultStatus.SUCCEEDED and read.simulated is True
        assert read.output["found"] is True
        denied = await adapter.execute(tool_request("shell.exec", key="adapter-shell", cmd="id"))
        assert denied.status == ResultStatus.DENIED and denied.error.code == "tool_not_allowed"
        assert seen == ["/v1/tools/execute", "/v1/tools/execute"]
        write = await adapter.execute(tool_request(effect=ToolEffect.WRITE, key="adapter-write"))
        assert write.status == ResultStatus.DENIED
        assert write.error.code == "external_writes_disabled"
        assert seen == ["/v1/tools/execute", "/v1/tools/execute"]
    assert app.state.local_response_store.counts()["tool_calls"] == 2


async def test_ac4_health_and_capabilities_report_local_mock_only(make_app, token):
    app = make_app()
    async with client(app, None) as anonymous:
        health = (await anonymous.get("/healthz")).json()
        assert health == {
            "status": "ok",
            "service": "rfa-local-response",
            "mode": "mock",
            "simulated": True,
            "supported_contract_versions": ["1.0", "1.1"],
        }
        assert (await anonymous.get("/v1/capabilities")).status_code == 401
        assert (await anonymous.get("/openapi.json")).status_code == 401
    async with client(app, token) as api:
        capabilities = (await api.get("/v1/capabilities")).json()
        assert capabilities["mode"] == "mock" and capabilities["simulated"] is True
        assert capabilities["authority"] == "reference_mock"
        assert capabilities["supported_contract_versions"] == ["1.0", "1.1"]
        assert capabilities["read_tools"] == ["synthetic.glossary_lookup", "synthetic.text_stats"]
        assert set(capabilities["real_integrations"]) >= {
            "mcp",
            "external_publication",
            "openshell",
        }
        assert not any(capabilities["real_integrations"].values())
        paths = set((await api.get("/openapi.json")).json()["paths"])
        assert {
            "/v1/reviews",
            "/v1/reviews/{draft_id}",
            "/v1/local/reviews",
            "/v1/local/reviews/{draft_id}",
            "/v1/local/reviews/{draft_id}/decision",
            "/v1/local/publications",
            "/v1/local/publications/{publication_id}",
            "/v1/tools/execute",
        } <= paths


@pytest.mark.parametrize(
    "kwargs",
    [
        {"allowed_hosts": ["0.0.0.0:8781"]},
        {"allowed_hosts": ["example.com:8781"]},
        {"allowed_hosts": ["127.0.0.1"]},
        {"allowed_hosts": []},
        {"service_token": SecretStr("short")},
        {"owner_id": "owner with spaces"},
    ],
)
def test_ac4_boundary_configuration_rejects_non_loopback_or_weak_values(kwargs):
    values = {
        "owner_id": OWNER,
        "service_token": SecretStr("t" * 32),
        "allowed_hosts": [HOST],
        **kwargs,
    }
    with pytest.raises((ValueError, TypeError)):
        LocalServiceBoundary.create(**values)
