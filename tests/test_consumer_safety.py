"""P1-008: one consumer, two transports; proofs before protected actions.

The same WorkService/DraftLifecycle consumer runs over the in-process mock ports and over
reference HTTP (1.0 review fixture + the P1-008C publication stand-in, both in-process via
ASGI behind one loopback base URL). Synthetic fixtures and test tokens only; an autouse
guard fails on any real socket connect. A mock/stand-in success is never a real
publication, MCP or OpenShell result: receipts stay mode=mock.
"""

from __future__ import annotations

import ast
import socket
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from rfa_mas.adapters.http import (
    PublicationHttpAdapter,
    ReferenceHttpClient,
    core_payload_hash,
    local_review_draft,
)
from rfa_mas.adapters.mock import MockPublisher
from rfa_mas.application.drafts import DraftLifecycle, draft_binding
from rfa_mas.bootstrap import build_container
from rfa_mas.contracts import (
    Audience,
    DirectWorkRequest,
    DomainId,
    DraftBundle,
    DraftEditRequest,
    DraftTarget,
    EvidenceRef,
    ExecutionMode,
    PublicationStatus,
    PublishRequest,
    SimulationScenario,
    SourceLocation,
    TrustedPrincipal,
    WorkStatus,
    sha256_text,
)
from rfa_mas.errors import RfaError
from rfa_mas.reference import create_reference_contract_app
from rfa_mas.reference.local_response import create_local_response_app
from rfa_mas.reference.local_security import LocalServiceBoundary
from scripts.contract_baseline import offline_settings

TOKEN = SecretStr("synthetic-consumer-token-p1008-000001")
HOST = "127.0.0.1:8781"
BASE = f"http://{HOST}"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("real network attempted")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


async def _discard(message):
    return None


class Router:
    """One loopback 'Response service': 1.0 review fixture + P1-008C publication stand-in."""

    def __init__(self, reference):
        self.reference = reference
        self.stand_in = None
        self.fail_next_publication: str | None = None
        self.publication_posts = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/v1/local/"):
            if scope["method"] == "POST" and scope["path"] == "/v1/local/publications":
                self.publication_posts += 1
                mode, self.fail_next_publication = self.fail_next_publication, None
                if mode == "lost_request":
                    raise httpx.ReadTimeout("synthetic timeout before delivery")
                if mode == "lost_ack":
                    await self.stand_in(scope, receive, _discard)
                    raise httpx.ReadTimeout("synthetic timeout after delivery")
            await self.stand_in(scope, receive, send)
            return
        await self.reference(scope, receive, send)


class Stack:
    def __init__(self, transport, container, router=None, operator=None):
        self.transport, self.container = transport, container
        self.router, self.operator = router, operator

    async def owner(self):
        return await self.container.repository.local_principal()

    def protected_actions(self) -> int:
        if self.transport == "mock":
            return len(self.container.drafts._publisher.sink)
        return self.router.stand_in.state.local_response_store.counts()["publications"]

    def publisher_calls(self) -> int:
        if self.transport == "mock":
            return self.container.drafts._publisher.calls
        return self.router.publication_posts

    async def latest_draft(self, run_id):
        versions = await self.container.repository.draft_versions(run_id, await self.owner())
        return versions[-1][0]

    async def approve_at_publication_authority(self, run_id, *, draft=None):
        """Operator step at the stand-in (the approval ORIGINAL for publication).

        Mock mode needs nothing extra: the mock review authority is also the publisher's.
        """
        if self.transport == "mock":
            return None
        draft = draft or await self.latest_draft(run_id)
        mirror = local_review_draft(draft, policy_decision_id="policy-decision-p1008")
        label = f"{draft.draft_id}-{draft.version}-{mirror.content_hash[:8]}"
        submitted = await self.operator.post(
            "/v1/local/reviews",
            json={"draft": mirror.model_dump(mode="json")},
            headers={"Idempotency-Key": f"mirror-{label}"},
        )
        assert submitted.status_code == 200, submitted.text
        decided = await self.operator.post(
            f"/v1/local/reviews/{draft.draft_id}/decision",
            json={
                "draft_version": mirror.version,
                "content_hash": mirror.content_hash,
                "payload_hash": mirror.payload_hash,
                "target": mirror.target.model_dump(mode="json"),
                "decision": "approved",
            },
            headers={"Idempotency-Key": f"decide-{label}"},
        )
        assert decided.status_code == 200, decided.text
        return decided.json()["approval"]


@asynccontextmanager
async def build_stack(kind, tmp_path):
    root = tmp_path.resolve()
    if kind == "mock":
        container = build_container(offline_settings(root))
        await container.startup()
        try:
            yield Stack("mock", container)
        finally:
            await container.shutdown()
        return
    router = Router(create_reference_contract_app(service_token=TOKEN, manual_decisions=True))
    settings = offline_settings(
        root, response_backend="http", response_base_url=BASE, response_api_token=TOKEN
    )
    container = build_container(settings, http_transport=httpx.ASGITransport(app=router))
    await container.startup()
    router.stand_in = create_local_response_app(
        db_path=root / "stand-in" / "response.db",
        boundary=LocalServiceBoundary.create(
            owner_id=await container.service_owner_id(),
            service_token=TOKEN,
            allowed_hosts=[HOST],
        ),
    )
    operator = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=router),
        base_url=BASE,
        headers={"Authorization": f"Bearer {TOKEN.get_secret_value()}"},
    )
    try:
        assert isinstance(container.drafts._publisher, PublicationHttpAdapter)
        yield Stack("http", container, router, operator)
    finally:
        await operator.aclose()
        await container.shutdown()


@pytest.fixture(params=["mock", "http"])
async def stack(request, tmp_path):
    async with build_stack(request.param, tmp_path) as value:
        yield value


@pytest.fixture
async def http_stack(tmp_path):
    async with build_stack("http", tmp_path) as value:
        yield value


def work(**overrides):
    return DirectWorkRequest(
        **{
            "query": "TRIV3 공개 트랙",
            "domain_id": DomainId.TRIV3,
            "target": DraftTarget(audience=Audience.PUBLIC),
            **overrides,
        }
    )


async def completed_run(stack, **overrides):
    owner = await stack.owner()
    result = await stack.container.service.run(work(**overrides), owner)
    assert result.status == WorkStatus.COMPLETED, result.errors
    await stack.approve_at_publication_authority(result.run_id)
    return owner, result


async def publish(stack, run_id, key, owner=None):
    return await stack.container.drafts.publish(
        run_id, PublishRequest(idempotency_key=key), owner or await stack.owner()
    )


# -- AC1/AC4: seven consumer cases, identical meaning over mock and reference HTTP -------


async def test_normal_approval_publishes_one_mock_receipt(stack):
    owner, result = await completed_run(stack)
    receipt = await publish(stack, result.run_id, "pub-normal", owner)
    assert receipt.status == PublicationStatus.SUCCEEDED
    assert receipt.mode == ExecutionMode.MOCK  # Never a real publication.
    assert receipt.external_result_ref.startswith("local-artifact:")
    assert receipt.binding == draft_binding(await stack.latest_draft(result.run_id), ())
    assert stack.protected_actions() == 1 and stack.publisher_calls() == 1
    [effect] = [e for e in await stack.container.service.effects(result.run_id, owner)
                if e.kind == "publication"]
    assert (effect.state, effect.outcome) == ("completed", "succeeded")


async def test_policy_deny_produces_no_draft_and_no_protected_action(stack):
    owner = await stack.owner()
    result = await stack.container.service.run(
        work(simulation_scenario=SimulationScenario.POLICY_DENIED), owner
    )
    assert result.status == WorkStatus.FAILED and result.draft is None
    with pytest.raises(RfaError) as denied:
        await publish(stack, result.run_id, "pub-denied", owner)
    assert denied.value.code == "not_found"
    assert stack.protected_actions() == 0 and stack.publisher_calls() == 0


async def test_missing_core_approval_never_publishes(stack):
    owner = await stack.owner()
    result = await stack.container.service.run(
        work(simulation_scenario=SimulationScenario.REVISION_REQUESTED), owner
    )
    assert result.status == WorkStatus.WAITING_APPROVAL
    # Even an approval at the publication authority cannot bypass the core review.
    await stack.approve_at_publication_authority(result.run_id)
    with pytest.raises(RfaError) as denied:
        await publish(stack, result.run_id, "pub-unapproved", owner)
    assert denied.value.code == "approval_required"
    assert stack.protected_actions() == 0 and stack.publisher_calls() == 0


@pytest.mark.parametrize("change", ["content", "source_acl", "policy"])
async def test_change_after_approval_requires_review_again(stack, change):
    owner, result = await completed_run(stack)
    if change == "content":
        await stack.container.drafts.edit(
            result.run_id,
            DraftEditRequest(expected_version=1, content="승인 뒤 바뀐 공개 요약"),
            owner,
        )
    elif change == "source_acl":
        reference = result.draft.allowed_evidence[0]
        documents = await stack.container.repository.list_documents(DomainId.TRIV3.value)
        document = next(item for item in documents if item.source_id == reference.source_id)
        await stack.container.repository.upsert_documents(
            [document.model_copy(update={"source_revision": "synthetic-revoked-revision"})]
        )
    else:
        service = stack.container.service
        service._dependencies = replace(service._dependencies, policy_version=lambda: "next")
    with pytest.raises(RfaError) as denied:
        await publish(stack, result.run_id, f"pub-{change}", owner)
    assert denied.value.code == "approval_required"
    assert stack.protected_actions() == 0 and stack.publisher_calls() == 0


async def test_duplicate_publication_request_is_one_protected_action(stack):
    owner, result = await completed_run(stack)
    first = await publish(stack, result.run_id, "pub-dup", owner)
    replay = await publish(stack, result.run_id, "pub-dup", owner)
    assert replay == first
    with pytest.raises(RfaError) as other:
        await publish(stack, result.run_id, "pub-other-key", owner)
    assert other.value.code == "publication_exists"
    assert stack.protected_actions() == 1 and stack.publisher_calls() == 1


async def test_lost_publication_ack_is_unknown_and_never_republished(stack):
    owner, result = await completed_run(stack)
    if stack.transport == "mock":
        owned = DraftLifecycle._owned_key(owner, result.run_id, "pub-timeout")
        stack.container.drafts._publisher = MockPublisher(lose_ack=frozenset({owned}))
    else:
        stack.router.fail_next_publication = "lost_ack"
    unknown = await publish(stack, result.run_id, "pub-timeout", owner)
    assert unknown.status == PublicationStatus.OUTCOME_UNKNOWN and unknown.next_action == "query"
    replay = await publish(stack, result.run_id, "pub-timeout", owner)
    # Same meaning on both transports: the effect happened once, nothing is re-sent.
    assert stack.protected_actions() == 1 and stack.publisher_calls() == 1
    assert replay.publication_id == unknown.publication_id
    if stack.transport == "mock":
        assert replay.status == PublicationStatus.SUCCEEDED  # found by result lookup
    else:
        # The stand-in offers no lookup by idempotency key and no receipt reference came
        # back, so the publication stays unknown until an original reference exists.
        assert replay.status == PublicationStatus.OUTCOME_UNKNOWN
    record = await stack.container.repository.get_owned_run(result.run_id, owner)
    assert record.status == WorkStatus.COMPLETED  # Publication state is separate from run.


# -- AC2/AC3: proofs before protected actions (reference HTTP publication stand-in) ---------


def forged_publisher(stack, **overrides):
    token = overrides.pop("token", TOKEN)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=stack.router), base_url=BASE)
    stack.container.http_clients.append(client)
    return PublicationHttpAdapter(
        ReferenceHttpClient(client, token=token, max_read_retries=0), **overrides
    )


@pytest.mark.parametrize(
    "proof",
    ["identity_missing", "identity_forged", "approval_missing", "approval_pending",
     "approver_forged", "approval_other_content", "approval_expired"],
)
async def test_missing_forged_or_expired_proof_means_zero_protected_actions(http_stack, proof):
    stack = http_stack
    owner = await stack.owner()
    result = await stack.container.service.run(work(), owner)
    assert result.status == WorkStatus.COMPLETED
    draft = await stack.latest_draft(result.run_id)
    if proof in {"identity_missing", "identity_forged"}:
        await stack.approve_at_publication_authority(result.run_id)
        token = None if proof == "identity_missing" else SecretStr("forged-token-p1008-000000001")
        stack.container.drafts._publisher = forged_publisher(stack, token=token)
    elif proof == "approval_pending":
        mirror = local_review_draft(draft, policy_decision_id="policy-decision-p1008")
        response = await stack.operator.post(
            "/v1/local/reviews", json={"draft": mirror.model_dump(mode="json")},
            headers={"Idempotency-Key": "pending-only"},
        )
        assert response.status_code == 200
    elif proof == "approver_forged":
        await stack.approve_at_publication_authority(result.run_id)

        async def someone_else():
            return "not-the-installation-owner"

        stack.container.drafts._publisher = forged_publisher(stack, expected_approver=someone_else)
    elif proof == "approval_other_content":
        other = draft.model_copy(
            update={"content": "다른 승인 본문", "content_hash": sha256_text("다른 승인 본문")}
        )
        await stack.approve_at_publication_authority(result.run_id, draft=other)
    elif proof == "approval_expired":
        await stack.approve_at_publication_authority(result.run_id)

        def later():
            return datetime.now(UTC) + timedelta(days=1)

        stack.container.drafts._publisher = forged_publisher(stack, clock=later)
    receipt = await publish(stack, result.run_id, f"pub-{proof}", owner)
    # A definite refusal: nothing was published and the slot is not silently reused.
    assert receipt.status == PublicationStatus.FAILED and receipt.external_result_ref is None
    assert stack.router.publication_posts == 0
    assert stack.protected_actions() == 0


async def test_unauthenticated_principal_reaches_no_publication(http_stack):
    stack = http_stack
    owner, result = await completed_run(stack)
    for principal in (
        TrustedPrincipal(user_id=owner.user_id, authenticated=False),
        TrustedPrincipal(user_id="someone-else", authenticated=True),
    ):
        with pytest.raises(RfaError):
            await publish(stack, result.run_id, "pub-anon", principal)
    assert stack.router.publication_posts == 0 and stack.protected_actions() == 0


async def test_lost_publication_request_is_unknown_with_zero_effects(http_stack):
    stack = http_stack
    owner, result = await completed_run(stack)
    stack.router.fail_next_publication = "lost_request"
    unknown = await publish(stack, result.run_id, "pub-lost", owner)
    again = await publish(stack, result.run_id, "pub-lost", owner)
    assert unknown.status == again.status == PublicationStatus.OUTCOME_UNKNOWN
    assert stack.router.publication_posts == 1 and stack.protected_actions() == 0


async def test_stand_in_unknown_receipt_is_reconciled_by_status_query_only(http_stack):
    stack = http_stack
    owner, result = await completed_run(stack)
    publisher = stack.container.drafts._publisher
    publisher._simulate = "outcome_unknown"
    unknown = await publish(stack, result.run_id, "pub-stand-in-unknown", owner)
    queried = await stack.container.drafts.query(result.run_id, owner)
    replay = await publish(stack, result.run_id, "pub-stand-in-unknown", owner)
    assert unknown.status == queried.status == replay.status == PublicationStatus.OUTCOME_UNKNOWN
    assert stack.router.publication_posts == 1 and stack.protected_actions() == 1


def test_consumer_payload_hash_matches_the_core_binding(tmp_path):
    content = "합성 공개 요약"
    draft = DraftBundle(
        request_id="r", trace_id="t", run_id="run-hash", agent_id="assistant-supervisor",
        domain_id=DomainId.TRIV3, draft_id="draft-hash", version=2,
        content_hash=sha256_text(content), target=DraftTarget(audience=Audience.PUBLIC),
        audience=Audience.PUBLIC, policy_version="local-v1",
        allowed_evidence=(EvidenceRef(
            source_id="src-1", source_revision="rev-1",
            location=SourceLocation(uri="fixture://src-1", section="s"),
            audience=Audience.PUBLIC, content_hash=sha256_text("evidence")),),
        content=content, simulated=True, adapter="fixture",
    )
    binding = draft_binding(draft, ())
    mirror = local_review_draft(draft, policy_decision_id="policy-decision-p1008")
    assert mirror.binding().sources == binding.sources
    assert core_payload_hash(
        draft_id=draft.draft_id, version=draft.version, content=draft.content,
        content_hash=draft.content_hash, attachments=(), target=draft.target,
        policy_version=draft.policy_version, sources=mirror.binding().sources,
    ) == binding.payload_hash


# -- AC4: the graph stays free of URL/auth/MCP SDK -----------------------------------------


def test_graph_and_application_have_no_transport_auth_or_mcp_sdk():
    forbidden_modules = {"httpx", "requests", "aiohttp", "mcp", "fastapi", "urllib"}
    forbidden_text = ("http://", "https://", "Authorization", "Bearer ")
    files = sorted((ROOT / "src/rfa_mas/application").rglob("*.py"))
    assert files
    for path in files:
        source = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert name.split(".")[0] not in forbidden_modules, (path.name, name)
        if "graphs" in path.parts:
            assert not any(token in source for token in forbidden_text), path.name
