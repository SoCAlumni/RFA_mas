"""P1-008: authenticated review callbacks bound to the current draft; replays idempotent.

A callback is only a signed wake-up from the review authority. It is verified on raw bytes
before parsing, bound to the installation approver and to the CURRENT draft version/hash/
target, and never grants approval by itself: the consumer still queries the authority.
Synthetic test secret only (never from .env).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from rfa_mas.adapters.http import (
    ReviewCallback,
    ReviewCallbackVerifier,
    sign_callback,
)
from rfa_mas.bootstrap import build_container
from rfa_mas.contracts import (
    Audience,
    DirectWorkRequest,
    DomainId,
    DraftBundle,
    DraftTarget,
    ResumeRequest,
    ReviewStatus,
    SimulationScenario,
    WorkStatus,
    sha256_text,
)
from rfa_mas.errors import RfaError
from scripts.contract_baseline import offline_settings

SECRET = SecretStr("synthetic-callback-secret-p1008-0001")
APPROVER = "installation-owner-callback"
CANARY = "CALLBACK-PRIVATE-CANARY-7c1d"


def draft(**overrides) -> DraftBundle:
    content = overrides.pop("content", "callback 대상 공개 요약")
    return DraftBundle(
        **{
            "request_id": "req-callback",
            "trace_id": "trace-callback",
            "run_id": "run-callback",
            "agent_id": "domain-supervisor:triv3",
            "domain_id": DomainId.TRIV3,
            "draft_id": "draft-callback",
            "version": 1,
            "content_hash": sha256_text(content),
            "target": DraftTarget(audience=Audience.PUBLIC),
            "audience": Audience.PUBLIC,
            "policy_version": "local-v1",
            "allowed_evidence": (),
            "content": content,
            "simulated": True,
            "adapter": "fixture",
            **overrides,
        }
    )


def event(current: DraftBundle, **overrides) -> ReviewCallback:
    return ReviewCallback(
        **{
            "event_id": "evt-1",
            "run_id": current.run_id,
            "draft_id": current.draft_id,
            "draft_version": current.version,
            "content_hash": current.content_hash,
            "target": current.target,
            "decision": ReviewStatus.APPROVED,
            "approver_id": APPROVER,
            "issued_at": datetime.now(UTC),
            **overrides,
        }
    )


def signed(callback: ReviewCallback, *, secret: SecretStr = SECRET, at: float | None = None):
    body = callback.model_dump_json().encode()
    timestamp = str(int(at if at is not None else time.time()))
    return body, sign_callback(body, secret=secret, timestamp=timestamp), timestamp


def verifier(**overrides) -> ReviewCallbackVerifier:
    return ReviewCallbackVerifier(secret=SECRET, expected_approver=APPROVER, **overrides)


def test_valid_callback_is_bound_to_the_current_draft():
    current = draft()
    body, signature, timestamp = signed(event(current))
    check = verifier()
    callback = check.verify(body, signature=signature, timestamp=timestamp)
    decision, new = check.accept(callback, current)
    assert new and decision.decision == ReviewStatus.APPROVED
    assert (decision.run_id, decision.draft_id, decision.draft_version) == (
        current.run_id, current.draft_id, current.version
    )
    assert (decision.content_hash, decision.target) == (current.content_hash, current.target)
    assert (decision.request_id, decision.trace_id, decision.agent_id) == (
        current.request_id, current.trace_id, current.agent_id
    )


@pytest.mark.parametrize("attack", ["no_signature", "no_timestamp", "forged", "tampered"])
def test_unauthenticated_callbacks_are_rejected_before_parsing(attack):
    current = draft()
    body, signature, timestamp = signed(event(current, event_id="evt-attack"))
    if attack == "no_signature":
        signature = None
    elif attack == "no_timestamp":
        timestamp = None
    elif attack == "forged":
        _, signature, timestamp = signed(
            event(current), secret=SecretStr("attacker-guessed-secret-000001")
        )
    else:
        body = body.replace(b'"approved"', b'"rejected"') + CANARY.encode()
    with pytest.raises(RfaError) as denied:
        verifier().verify(body, signature=signature, timestamp=timestamp)
    assert denied.value.code == "authentication_required"
    assert CANARY not in denied.value.safe_message


def test_stale_callback_is_rejected_even_when_signed():
    current = draft()
    body, signature, timestamp = signed(event(current), at=time.time() - 3600)
    with pytest.raises(RfaError) as stale:
        verifier(max_skew=timedelta(minutes=5)).verify(
            body, signature=signature, timestamp=timestamp
        )
    assert stale.value.code == "callback_expired"


@pytest.mark.parametrize(
    "mismatch",
    [
        {"approver_id": "someone-else"},
        {"run_id": "run-other"},
        {"draft_version": 2},
        {"content_hash": sha256_text("다른 본문")},
        {"target": DraftTarget(audience=Audience.OWNER)},
    ],
    ids=["approver", "run", "version", "hash", "target"],
)
def test_approver_run_version_hash_or_target_mismatch_is_rejected(mismatch):
    current = draft()
    body, signature, timestamp = signed(event(current, **mismatch))
    check = verifier()
    with pytest.raises(RfaError) as denied:
        callback = check.verify(body, signature=signature, timestamp=timestamp)
        check.accept(callback, current)
    assert denied.value.code == "approval_binding_mismatch"


def test_callback_for_a_superseded_version_is_rejected():
    stale = draft()
    body, signature, timestamp = signed(event(stale))
    current = draft(version=2, content="수정된 공개 요약")
    check = verifier()
    callback = check.verify(body, signature=signature, timestamp=timestamp)
    with pytest.raises(RfaError) as denied:
        check.accept(callback, current)
    assert denied.value.code == "approval_binding_mismatch"


def test_replayed_event_is_idempotent_and_reused_id_with_other_payload_conflicts():
    current = draft()
    check = verifier()
    body, signature, timestamp = signed(event(current))
    first, new = check.accept(check.verify(body, signature=signature, timestamp=timestamp), current)
    again, replayed_new = check.accept(
        check.verify(body, signature=signature, timestamp=timestamp), current
    )
    assert new and not replayed_new and again == first
    other_body, other_signature, other_timestamp = signed(
        event(current, decision=ReviewStatus.REJECTED)
    )
    with pytest.raises(RfaError) as conflict:
        check.accept(
            check.verify(other_body, signature=other_signature, timestamp=other_timestamp),
            current,
        )
    assert conflict.value.code == "idempotency_conflict"


def test_short_secret_is_refused():
    with pytest.raises(ValueError):
        ReviewCallbackVerifier(secret=SecretStr("short"), expected_approver=APPROVER)


async def test_callback_only_wakes_a_query_and_never_grants_approval(tmp_path):
    container = build_container(offline_settings(tmp_path.resolve()))
    await container.startup()
    try:
        owner = await container.repository.local_principal()
        approver = await container.service_owner_id()
        check = ReviewCallbackVerifier(secret=SECRET, expected_approver=approver)
        result = await container.service.run(
            DirectWorkRequest(
                query="TRIV3 공개 트랙",
                domain_id=DomainId.TRIV3,
                target=DraftTarget(audience=Audience.PUBLIC),
                simulation_scenario=SimulationScenario.REVISION_REQUESTED,
            ),
            owner,
        )
        assert result.status == WorkStatus.WAITING_APPROVAL
        current = (await container.repository.draft_versions(result.run_id, owner))[-1][0]
        # A verified callback claiming approval while the authority's record is not approved.
        body, signature, timestamp = signed(event(current, approver_id=approver))
        callback = check.verify(body, signature=signature, timestamp=timestamp)
        check.accept(callback, current)
        woken = await container.service.resume(
            result.run_id, ResumeRequest(event_id=callback.event_id), owner
        )
        assert woken.status == WorkStatus.WAITING_APPROVAL  # The query decides, not the push.
        # A forged callback never even triggers a wake-up.
        forged = signed(event(current, event_id="evt-forged", approver_id=approver),
                        secret=SecretStr("attacker-guessed-secret-000001"))
        with pytest.raises(RfaError):
            check.verify(forged[0], signature=forged[1], timestamp=forged[2])
        # The authority records the manual approval, then signs the callback.
        response = container.response
        await response.decide(
            current.draft_id,
            draft_version=current.version,
            content_hash=current.content_hash,
            decision=ReviewStatus.APPROVED,
        )
        body, signature, timestamp = signed(
            event(current, event_id="evt-approved", approver_id=approver)
        )
        check.accept(check.verify(body, signature=signature, timestamp=timestamp), current)
        done = await container.service.resume(
            result.run_id, ResumeRequest(event_id="evt-approved"), owner
        )
        assert done.status == WorkStatus.COMPLETED
    finally:
        await container.shutdown()
