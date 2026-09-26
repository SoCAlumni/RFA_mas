"""P1-005A: DRAFT edits invalidate stale approvals; mock publication state is separate.

Synthetic fixtures only (offline settings, temp SQLite). The publisher is the in-process
MockPublisher: receipts are mode=mock with local-artifact references, never real writes.
"""

from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from rfa_mas.adapters.mock import MockPublisher
from rfa_mas.api.app import create_app, resolve_principal
from rfa_mas.application.drafts import DraftLifecycle
from rfa_mas.bootstrap import build_container
from rfa_mas.contracts import (
    AttachmentRef,
    Audience,
    DirectWorkRequest,
    DomainId,
    DraftEditRequest,
    DraftTarget,
    ExecutionMode,
    KnowledgeWrite,
    PublicationStatus,
    PublishRequest,
    ReviewDecision,
    ReviewStatus,
    TrustedPrincipal,
    WorkStatus,
    sha256_text,
)
from rfa_mas.errors import RfaError
from scripts.contract_baseline import offline_settings


@pytest.fixture
async def container(tmp_path):
    instance = build_container(offline_settings(tmp_path))
    await instance.startup()
    try:
        yield instance
    finally:
        await instance.shutdown()


class Authority:
    """Test-owned review ORIGINAL. Decisions are set explicitly, never inferred."""

    adapter_name = "fixture-review-authority"
    simulated = True

    def __init__(self, decision: ReviewStatus = ReviewStatus.APPROVED):
        self.next_decision = decision
        self.decision: ReviewDecision | None = None
        self.submissions = 0

    async def submit_draft(self, draft, **kwargs):
        self.submissions += 1
        self.decision = ReviewDecision(
            request_id=draft.request_id,
            trace_id=draft.trace_id,
            run_id=draft.run_id,
            agent_id=draft.agent_id,
            domain_id=draft.domain_id,
            draft_id=draft.draft_id,
            draft_version=draft.version,
            content_hash=draft.content_hash,
            target=draft.target,
            decision=self.next_decision,
            safe_reason="synthetic decision",
            simulated=True,
            adapter=self.adapter_name,
        )
        return self.decision

    async def get_decision(self, draft_id):
        return self.decision if self.decision and self.decision.draft_id == draft_id else None


def install(container, authority):
    container.service._dependencies = replace(container.service._dependencies, response=authority)
    container.service.start(container.checkpoints.saver)


def work(**overrides):
    return DirectWorkRequest(
        **{
            "query": "TRIV3 공개 트랙",
            "domain_id": DomainId.TRIV3,
            "target": DraftTarget(audience=Audience.PUBLIC),
            **overrides,
        }
    )


async def approved_run(container, **overrides):
    authority = Authority()
    install(container, authority)
    owner = await container.repository.local_principal()
    result = await container.service.run(work(**overrides), owner)
    assert result.status == WorkStatus.COMPLETED, result.errors
    return authority, owner, result


def attachment(label: str) -> AttachmentRef:
    return AttachmentRef(attachment_id=f"att-{label}", content_hash=sha256_text(label))


async def test_current_approval_publishes_once_with_mock_receipt_separate_from_run(container):
    _, owner, result = await approved_run(container)
    drafts = container.drafts
    state = await drafts.state(result.run_id, owner)
    assert state.approval_valid and state.invalid_reason is None
    assert state.publication_status == PublicationStatus.NOT_REQUESTED
    receipt = await drafts.publish(result.run_id, PublishRequest(idempotency_key="pub-1"), owner)
    assert receipt.status == PublicationStatus.SUCCEEDED
    assert receipt.mode == ExecutionMode.MOCK and receipt.mode != ExecutionMode.REAL
    assert receipt.external_result_ref.startswith("local-artifact:")
    assert receipt.binding.version == 1
    assert receipt.binding.content_hash == result.draft.content_hash
    publisher = drafts._publisher
    assert isinstance(publisher, MockPublisher) and len(publisher.sink) == 1
    # Replay returns the stored receipt; a different key cannot publish again.
    replay = await drafts.publish(result.run_id, PublishRequest(idempotency_key="pub-1"), owner)
    assert replay == receipt
    with pytest.raises(RfaError) as denied:
        await drafts.publish(result.run_id, PublishRequest(idempotency_key="pub-2"), owner)
    assert denied.value.code == "publication_exists"
    assert len(publisher.sink) == 1 and publisher.calls == 1
    # Run lifecycle is unchanged by publication; the run result keeps its own field.
    record = await container.repository.get_owned_run(result.run_id, owner)
    assert record.status == WorkStatus.COMPLETED
    assert record.result.publication_status == PublicationStatus.NOT_REQUESTED
    after = await drafts.state(result.run_id, owner)
    assert after.publication_status == PublicationStatus.SUCCEEDED
    assert after.publication_mode == ExecutionMode.MOCK
    # A published draft is frozen.
    with pytest.raises(RfaError) as frozen:
        await drafts.edit(
            result.run_id, DraftEditRequest(expected_version=1, content="late edit"), owner
        )
    assert frozen.value.code == "publication_exists"


async def test_edit_invalidates_prior_approval_and_replayed_or_forged_decisions(container):
    authority, owner, result = await approved_run(container)
    drafts = container.drafts
    v1_decision = authority.decision
    state = await drafts.edit(
        result.run_id, DraftEditRequest(expected_version=1, content="공개 FAQ 기준 수정본"), owner
    )
    assert state.current_version == 2 and state.versions == (1, 2)
    assert not state.approval_valid and state.invalid_reason == "draft_changed"
    # The previous approval (v1) replayed by the authority does not authorize v2.
    authority.decision = v1_decision
    with pytest.raises(RfaError) as stale:
        await drafts.publish(result.run_id, PublishRequest(idempotency_key="p"), owner)
    assert stale.value.code == "approval_required"
    # A decision claiming v2 but bound to another content hash or target is rejected.
    for forged in (
        {"draft_version": 2, "content_hash": sha256_text("different")},
        {"draft_version": 2, "content_hash": state.draft.content_hash,
         "target": DraftTarget(audience=Audience.COMPANY)},
    ):
        authority.decision = v1_decision.model_copy(update=forged)
        current = await drafts.state(result.run_id, owner)
        assert not current.approval_valid
        assert current.invalid_reason == "approval_binding_mismatch"
        with pytest.raises(RfaError):
            await drafts.publish(result.run_id, PublishRequest(idempotency_key="p"), owner)
    assert await container.repository.get_publication(result.run_id, owner) is None
    assert len(drafts._publisher.sink) == 0
    # Stale editors lose; the historical versions stay immutable.
    with pytest.raises(RfaError) as conflict:
        await drafts.edit(result.run_id, DraftEditRequest(expected_version=1, content="x"), owner)
    assert conflict.value.code == "draft_version_conflict"
    versions = await container.repository.draft_versions(result.run_id, owner)
    assert versions[0][0] == result.draft
    # Re-review of the current version restores a valid approval for exactly v2.
    authority.decision = None
    reviewed = await drafts.request_review(result.run_id, owner)
    assert reviewed.approval_valid and reviewed.review.draft_version == 2
    receipt = await drafts.publish(result.run_id, PublishRequest(idempotency_key="p"), owner)
    assert receipt.status == PublicationStatus.SUCCEEDED
    assert receipt.binding.version == 2
    assert receipt.binding.content_hash == sha256_text("공개 FAQ 기준 수정본")


@pytest.mark.parametrize("change", ["attachments", "target"])
async def test_attachment_or_target_change_is_a_new_version_needing_review(container, change):
    authority, owner, result = await approved_run(container)
    drafts = container.drafts
    body = {
        "attachments": DraftEditRequest(
            expected_version=1, content=result.draft.content, attachments=(attachment("a"),)
        ),
        "target": DraftEditRequest(
            expected_version=1,
            content=result.draft.content,
            target=DraftTarget(audience=Audience.PUBLIC, channel="faq", destination="faq-page"),
        ),
    }[change]
    state = await drafts.edit(result.run_id, body, owner)
    assert state.current_version == 2 and state.invalid_reason == "draft_changed"
    authority.decision = None
    await drafts.request_review(result.run_id, owner)
    receipt = await drafts.publish(result.run_id, PublishRequest(idempotency_key="k"), owner)
    v1_binding_hash = (await drafts.state(result.run_id, owner)).publication.binding.payload_hash
    assert receipt.binding.version == 2 and receipt.binding.payload_hash == v1_binding_hash
    if change == "attachments":
        assert state.attachments == (attachment("a"),)
    else:
        assert receipt.binding.target.destination == "faq-page"


async def test_private_markers_or_unshareable_target_are_refused_on_edit(container):
    _, owner, result = await approved_run(container)
    drafts = container.drafts
    with pytest.raises(RfaError) as marker:
        await drafts.edit(
            result.run_id,
            DraftEditRequest(expected_version=1, content="SYNTHETIC_PRIVATE_CANARY_EDIT"),
            owner,
        )
    assert marker.value.code == "policy_denied"
    state = await drafts.state(result.run_id, owner)
    assert state.current_version == 1 and state.approval_valid


async def test_owner_evidence_cannot_be_retargeted_to_public(container):
    owner = await container.repository.local_principal()
    await container.knowledge.write(
        KnowledgeWrite(
            domain_id=DomainId.TRIV3,
            provenance={"provider": "note", "namespace": "p1005a", "external_id": "owner-note"},
            provider_revision="r1",
            title="개인 실험 메모",
            content="TRIV3 개인 실험 메모: 다음 주 재측정 계획은 소유자만 본다.",
            acl={"audience": "owner"},
            synthetic=True,
        ),
        owner,
    )
    _, owner, result = await approved_run(
        container,
        query="개인 실험 메모 재측정 계획",
        target=DraftTarget(audience=Audience.OWNER),
    )
    assert {ref.audience for ref in result.draft.allowed_evidence} == {Audience.OWNER}
    with pytest.raises(RfaError) as denied:
        await container.drafts.edit(
            result.run_id,
            DraftEditRequest(
                expected_version=1,
                content=result.draft.content,
                target=DraftTarget(audience=Audience.PUBLIC),
            ),
            owner,
        )
    assert denied.value.code == "policy_denied"
    assert (await container.drafts.state(result.run_id, owner)).versions == (1,)


@pytest.mark.parametrize("change", ["source_revision", "policy"])
async def test_source_or_policy_change_invalidates_and_withholds_draft(container, change):
    _, owner, result = await approved_run(container)
    if change == "source_revision":
        reference = result.draft.allowed_evidence[0]
        documents = await container.repository.list_documents(DomainId.TRIV3.value)
        document = next(item for item in documents if item.source_id == reference.source_id)
        await container.repository.upsert_documents(
            [document.model_copy(update={"source_revision": "synthetic-new-revision"})]
        )
        expected = "sources_changed"
    else:
        container.service._dependencies = replace(
            container.service._dependencies, policy_version=lambda: "policy-next"
        )
        expected = "policy_changed"
    state = await container.drafts.state(result.run_id, owner)
    assert not state.approval_valid and state.invalid_reason == expected
    assert state.draft is None and state.review is None
    assert result.draft.content not in state.model_dump_json()
    with pytest.raises(RfaError) as denied:
        await container.drafts.publish(result.run_id, PublishRequest(idempotency_key="k"), owner)
    assert denied.value.code == "approval_required"
    assert len(container.drafts._publisher.sink) == 0


async def test_pending_or_rejected_review_never_publishes(container):
    authority = Authority(ReviewStatus.PENDING)
    install(container, authority)
    owner = await container.repository.local_principal()
    result = await container.service.run(work(), owner)
    assert result.status == WorkStatus.WAITING_APPROVAL
    for decision in (ReviewStatus.PENDING, ReviewStatus.REVISION_REQUESTED, ReviewStatus.REJECTED):
        authority.decision = authority.decision.model_copy(update={"decision": decision})
        state = await container.drafts.state(result.run_id, owner)
        assert not state.approval_valid and state.invalid_reason == "review_" + decision.value
        with pytest.raises(RfaError) as denied:
            await container.drafts.publish(
                result.run_id, PublishRequest(idempotency_key="k"), owner
            )
        assert denied.value.code == "approval_required"
    assert await container.repository.get_publication(result.run_id, owner) is None


async def test_lost_ack_is_outcome_unknown_and_reconciled_by_query_without_republish(container):
    _, owner, result = await approved_run(container)
    owned = DraftLifecycle._owned_key(owner, result.run_id, "k-unknown")
    publisher = MockPublisher(lose_ack=frozenset({owned}))
    container.drafts._publisher = publisher
    unknown = await container.drafts.publish(
        result.run_id, PublishRequest(idempotency_key="k-unknown"), owner
    )
    assert unknown.status == PublicationStatus.OUTCOME_UNKNOWN and unknown.next_action == "query"
    assert len(publisher.sink) == 1  # the effect happened; only the ack was lost
    again = await container.drafts.publish(
        result.run_id, PublishRequest(idempotency_key="k-unknown"), owner
    )
    assert again.status == PublicationStatus.SUCCEEDED
    assert publisher.calls == 1 and len(publisher.sink) == 1
    assert again.publication_id == unknown.publication_id


async def test_crash_after_durable_intent_is_unknown_never_resent(container):
    _, owner, result = await approved_run(container)
    publisher = MockPublisher()
    container.drafts._publisher = publisher
    state = await container.drafts.state(result.run_id, owner)
    from rfa_mas.application.drafts import draft_binding
    from rfa_mas.contracts import PublicationReceipt

    pending = PublicationReceipt(
        publication_id="pub-crashed",
        run_id=result.run_id,
        idempotency_key="k-crash",
        binding=draft_binding(state.draft, ()),
        approval_id="review:synthetic",
        status=PublicationStatus.PENDING,
        mode=ExecutionMode.MOCK,
        next_action="query",
    )
    await container.repository.put_publication(pending, owner, expected_status=None)
    queried = await container.drafts.query(result.run_id, owner)
    assert queried.status == PublicationStatus.OUTCOME_UNKNOWN and queried.next_action == "query"
    replay = await container.drafts.publish(
        result.run_id, PublishRequest(idempotency_key="k-crash"), owner
    )
    assert replay.status == PublicationStatus.OUTCOME_UNKNOWN
    assert publisher.calls == 0 and publisher.sink == []


async def test_definite_rejection_is_failed_and_terminal(container):
    _, owner, result = await approved_run(container)
    owned = DraftLifecycle._owned_key(owner, result.run_id, "k-fail")
    container.drafts._publisher = MockPublisher(fail=frozenset({owned}))
    failed = await container.drafts.publish(
        result.run_id, PublishRequest(idempotency_key="k-fail"), owner
    )
    assert failed.status == PublicationStatus.FAILED and failed.next_action == "review"
    assert (await container.drafts.query(result.run_id, owner)).status == PublicationStatus.FAILED


async def test_versions_and_receipts_survive_restart_and_other_users_are_refused(tmp_path):
    first = build_container(offline_settings(tmp_path))
    await first.startup()
    try:
        _, owner, result = await approved_run(first)
        await first.drafts.edit(
            result.run_id, DraftEditRequest(expected_version=1, content="재시작 전 수정본"), owner
        )
    finally:
        await first.shutdown()
    second = build_container(offline_settings(tmp_path))
    await second.startup()
    try:
        state = await second.drafts.state(result.run_id, owner)
        assert state.versions == (1, 2) and state.draft.content == "재시작 전 수정본"
        assert not state.approval_valid  # the in-memory mock approval is not durable authority
        stranger = TrustedPrincipal(
            user_id="someone-else", authenticated=True, company_id="other-company",
            business_units=frozenset(), roles=frozenset({"company"}),
        )
        with pytest.raises(RfaError) as hidden:
            await second.drafts.state(result.run_id, stranger)
        assert hidden.value.code == "not_found"
        with pytest.raises(RfaError):
            await second.drafts.publish(
                result.run_id, PublishRequest(idempotency_key="x"), stranger
            )
    finally:
        await second.shutdown()


async def test_non_mock_response_backend_gets_no_silent_mock_publisher(tmp_path, container):
    _, owner, result = await approved_run(container)
    container.drafts._publisher = None
    with pytest.raises(RfaError) as missing:
        await container.drafts.publish(result.run_id, PublishRequest(idempotency_key="k"), owner)
    assert missing.value.code == "not_implemented"
    assert await container.repository.get_publication(result.run_id, owner) is None


async def test_http_routes_map_lifecycle_errors(container):
    _, owner, result = await approved_run(container)
    app = create_app(container=container)

    async def current():
        return owner

    app.dependency_overrides[resolve_principal] = current
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        state = await client.get(f"/v1/runs/{result.run_id}/draft")
        assert state.status_code == 200 and state.json()["approval_valid"] is True
        edited = await client.post(
            f"/v1/runs/{result.run_id}/draft/edits",
            json={"expected_version": 1, "content": "HTTP 수정본"},
        )
        assert edited.status_code == 201 and edited.json()["invalid_reason"] == "draft_changed"
        stale = await client.post(
            f"/v1/runs/{result.run_id}/draft/edits",
            json={"expected_version": 1, "content": "늦은 수정"},
        )
        assert stale.status_code == 409
        refused = await client.post(
            f"/v1/runs/{result.run_id}/publication", json={"idempotency_key": "h1"}
        )
        assert refused.status_code == 409 and refused.json()["code"] == "approval_required"
        assert "HTTP 수정본" not in refused.text
        reviewed = await client.post(f"/v1/runs/{result.run_id}/draft/review")
        assert reviewed.status_code == 200 and reviewed.json()["approval_valid"] is True
        published = await client.post(
            f"/v1/runs/{result.run_id}/publication", json={"idempotency_key": "h1"}
        )
        body = published.json()
        assert published.status_code == 200 and body["status"] == "succeeded"
        assert body["mode"] == "mock" and body["external_result_ref"].startswith("local-artifact:")
        fetched = await client.get(f"/v1/runs/{result.run_id}/publication")
        assert fetched.json() == body
        assert (await client.get("/v1/runs/run_missing/publication")).status_code == 404
        assert json.dumps(body).count("HTTP 수정본") == 0
