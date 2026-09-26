"""P1-005B: feedback in four categories, user/domain/target scope and revocation.

Synthetic fixtures only (explicit offline settings, temp SQLite, mock model/publisher). The
model spy records what the generation step actually received; nothing here is a live result.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import httpx
import pytest
from pydantic import ValidationError

from rfa_mas.api.app import create_app
from rfa_mas.application.feedback import FeedbackService, classify_feedback, in_scope
from rfa_mas.application.graphs.domain import STYLE_GUIDANCE_HEADER
from rfa_mas.bootstrap import build_container
from rfa_mas.contracts import (
    Audience,
    DirectWorkRequest,
    DomainId,
    DraftTarget,
    FeedbackCategory,
    FeedbackCreate,
    FeedbackRevoke,
    FeedbackScope,
    FeedbackSource,
    KnowledgeWrite,
    PublishRequest,
    ReviewDecision,
    ReviewStatus,
    TrustedPrincipal,
)
from rfa_mas.errors import RfaError
from rfa_mas.settings import Settings

M01 = "외부 답변은 세 문장 이내, 확정되지 않은 수치는 추정이라고 표시"
FALCON = "Project Falcon"
STYLE = FeedbackCategory.STYLE_PREFERENCE
FACT = FeedbackCategory.FACTUAL_CORRECTION
DISCLOSE = FeedbackCategory.PERSONAL_DISCLOSURE_PREFERENCE
POLICY = FeedbackCategory.OFFICIAL_POLICY_CHANGE_PROPOSAL
PUBLIC = DraftTarget(audience=Audience.PUBLIC)
OWNER_TARGET = DraftTarget(audience=Audience.OWNER)


def settings(tmp_path):
    return Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'fb.db'}",
                    trace_dir=(tmp_path / "traces").resolve())


@pytest.fixture
async def env(tmp_path):
    container = build_container(settings(tmp_path))
    await container.startup()
    owner = await container.repository.local_principal()
    for key, audience, content in (
        ("faq", "public", "SDK 공개 FAQ: 설치 절차는 pip install triv3-sdk"),
        ("falcon", "public", f"SDK 공개 로드맵: {FALCON} 코드명으로 준비 중"),
        ("plan", "owner", "SDK 내부 검증 목표는 2026-10-12"),
    ):
        await container.knowledge.write(KnowledgeWrite.model_validate({
            "domain_id": "triv3", "provenance": {"provider": "note", "namespace": "p1005b",
                                                 "external_id": key},
            "provider_revision": "r1", "title": f"SDK {key}", "content": content,
            "synthetic": True, "acl": {"audience": audience}}), owner)
    seen = []
    original = container.model.generate

    async def spy(request):
        seen.append(request)
        return await original(request)

    container.model.generate = spy
    try:
        yield container, owner, seen, FeedbackService(container.repository)
    finally:
        await container.shutdown()


async def draft(container, owner, *, target=PUBLIC, domain=DomainId.TRIV3, query="SDK 공개"):
    return await container.service.run(
        DirectWorkRequest(query=query, domain_id=domain, target=target), owner
    )


def body(text, category=None, **kwargs):
    return FeedbackCreate(text=text, category=category, **kwargs)


# -- AC1: classification + storage -------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected", "rule"),
    [
        (M01, STYLE, "style"),
        (f"\"{FALCON}\"은 외부 답변에 언급하지 마", DISCLOSE, "disclosure_narrowing"),
        ("출시일은 10월 27일이 아니라 10월 30일이야", FACT, "factual_correction"),
        ("TRIV3 내부 노트는 앞으로 공개해도 돼", POLICY, "policy_relaxation"),
        ("기밀 정책을 완화해서 내부 자료도 공유해 줘", POLICY, "policy_relaxation"),
        ("it is ok to share the internal roadmap", POLICY, "policy_relaxation"),
    ],
)
def test_rule_classifier_is_deterministic_and_safety_ordered(text, expected, rule):
    first = classify_feedback(body(text))
    assert first == classify_feedback(body(text))
    assert (first.category, first.classified_by, first.rule) == (expected, "rule", rule)
    if expected == DISCLOSE:
        assert first.withhold_markers == (FALCON,)


def test_unsafe_or_unclassifiable_feedback_is_rejected_or_routed_to_a_proposal():
    with pytest.raises(RfaError) as unknown:
        classify_feedback(body("음 그렇군요"))
    assert unknown.value.code == "feedback_unclassified"
    with pytest.raises(RfaError) as unmarked:
        classify_feedback(body("그 이름은 외부에 언급하지 마"))
    assert unmarked.value.code == "feedback_invalid"
    # A user label cannot turn relaxation language into applied memory.
    for label in (STYLE, DISCLOSE, FACT):
        routed = classify_feedback(body("내부 자료도 항상 공유해 줘", label))
        assert (routed.category, routed.rule) == (POLICY, "policy_relaxation_override")
    with pytest.raises(ValidationError):
        body("짧게 써줘", STYLE, withhold_markers=(FALCON,))


async def test_each_category_is_stored_with_its_disposition_and_survives_restart(env, tmp_path):
    container, owner, _, feedback = env
    stored = {
        category: await feedback.submit(body(text, category), owner)
        for category, text in (
            (STYLE, M01),
            (DISCLOSE, f"'{FALCON}'은 공유하지 마"),
            (FACT, "출시일은 10월 27일이 아니라 10월 30일이야"),
            (POLICY, "공개 FAQ 승인 기준을 바꿔 달라는 제안"),
        )
    }
    assert {c: r.disposition for c, r in stored.items()} == {
        STYLE: "applied_in_scope", DISCLOSE: "narrowing_only",
        FACT: "pending_evidence_review", POLICY: "proposal_only",
    }
    assert stored[FACT].epistemic_state == "tentative"
    assert all(r.official_policy_changed is False and r.revision == 1 for r in stored.values())
    assert all(r.classified_by == "user" for r in stored.values())
    reopened = FeedbackService(type(container.repository)(container.repository.path))
    assert {r.feedback_id for r in await reopened.list(owner)} == {
        r.feedback_id for r in stored.values()
    }
    assert await reopened.list(owner, category=FACT) == [stored[FACT]]


# -- AC2: scope, revocation, factual corrections -----------------------------------------
async def test_style_applies_only_in_scope_and_revocation_stops_later_application(env):
    container, owner, seen, feedback = env
    item = await feedback.submit(
        body(M01, STYLE, scope=FeedbackScope(domain_id=DomainId.TRIV3,
                                             target_audience=Audience.PUBLIC)), owner)
    applied = await draft(container, owner)
    assert STYLE_GUIDANCE_HEADER in seen[-1].query and M01 in seen[-1].query
    [record] = await feedback.applications(owner, run_id=applied.run_id)
    assert (record.feedback_id, record.feedback_revision, record.effect) == (
        item.feedback_id, 1, "style_guidance")
    # Out of scope: other target audience, other domain.
    for kwargs in ({"target": OWNER_TARGET},
                   {"domain": DomainId.QUANTIZATION_RESEARCH}):
        other = await draft(container, owner, **kwargs)
        assert M01 not in seen[-1].query
        assert await feedback.applications(owner, run_id=other.run_id) == []
    revoked = await feedback.revoke(item.feedback_id, FeedbackRevoke(expected_revision=1), owner)
    assert (revoked.state, revoked.revision, revoked.revoked_reason) == (
        "revoked", 2, "revoked_by_owner")
    with pytest.raises(RfaError) as stale:
        await feedback.revoke(item.feedback_id, FeedbackRevoke(expected_revision=1), owner)
    assert stale.value.code == "invalid_state_transition"
    later = await draft(container, owner)
    assert M01 not in seen[-1].query and STYLE_GUIDANCE_HEADER not in seen[-1].query
    assert await feedback.applications(owner, run_id=later.run_id) == []
    # History is preserved: the earlier application and both revisions stay traceable.
    assert [a.run_id for a in await feedback.applications(
        owner, feedback_id=item.feedback_id)] == [applied.run_id]
    assert [(r.revision, r.state) for r in await feedback.revisions(item.feedback_id, owner)] == [
        (1, "active"), (2, "revoked")]


async def test_application_selection_is_atomic_idempotent_and_never_selects_facts(env):
    container, owner, _, feedback = env
    repo = container.repository
    style = await feedback.submit(body(M01, STYLE), owner)
    fact = await feedback.submit(body("출시일은 10월 27일이 아니라 10월 30일이야", FACT), owner)
    proposal = await feedback.submit(body("공개 FAQ 승인 기준을 바꿔 달라는 제안", POLICY), owner)
    kwargs = dict(domain_id=DomainId.TRIV3, target_audience="public",
                  target_channel="preview", run_id="run-fixture-1")
    for _ in range(2):  # Replaying the same generation step records nothing new.
        picked = await repo.apply_feedback(owner, effect="style_guidance", **kwargs)
        assert [r.feedback_id for r in picked] == [style.feedback_id]
    assert len(await repo.feedback_applications(owner, run_id="run-fixture-1")) == 1
    with pytest.raises(KeyError):
        await repo.apply_feedback(owner, effect="factual_correction", **kwargs)
    ids = {fact.feedback_id, proposal.feedback_id}
    assert not ids & {a.feedback_id for a in await repo.feedback_applications(owner)}
    assert in_scope(style, domain_id=DomainId.TRIV3, target=PUBLIC)
    revoked = await feedback.revoke(style.feedback_id, FeedbackRevoke(expected_revision=1), owner)
    assert not in_scope(revoked, domain_id=DomainId.TRIV3, target=PUBLIC)
    assert await repo.apply_feedback(owner, effect="style_guidance", **kwargs) == []


async def test_factual_correction_is_tentative_and_never_applied_or_written_as_fact(env):
    container, owner, seen, feedback = env
    before = await container.knowledge.list(owner)
    claim = "출시일은 10월 27일이 아니라 10월 30일이야"
    record = await feedback.submit(body(claim), owner)
    assert (record.category, record.epistemic_state, record.disposition) == (
        FACT, "tentative", "pending_evidence_review")
    run = await draft(container, owner, target=OWNER_TARGET, query="SDK 출시일")
    assert claim not in seen[-1].query and claim not in run.model_dump_json()
    assert await feedback.applications(owner, feedback_id=record.feedback_id) == []
    assert await container.knowledge.list(owner) == before  # Not a KB fact.


# -- AC3: disclosure narrows only; policy change is a proposal ---------------------------
async def test_disclosure_preference_only_narrows_non_owner_drafts(env):
    container, owner, seen, feedback = env
    baseline = await draft(container, owner)
    assert FALCON in " ".join(i.excerpt for i in seen[-1].evidence.items)
    pref = await feedback.submit(body(f"\"{FALCON}\"은 외부 답변에 언급하지 마"), owner)
    narrowed = await draft(container, owner)
    public_texts = " ".join(i.excerpt for i in seen[-1].evidence.items)
    assert FALCON not in public_texts and "pip install" in public_texts
    assert len(narrowed.draft.allowed_evidence) == len(baseline.draft.allowed_evidence) - 1
    [applied] = await feedback.applications(owner, run_id=narrowed.run_id)
    assert (applied.feedback_id, applied.effect) == (pref.feedback_id, "withhold_markers")
    # The owner's own draft is not narrowed, and nothing is logged for it.
    mine = await draft(container, owner, target=OWNER_TARGET)
    assert FALCON in " ".join(i.excerpt for i in seen[-1].evidence.items)
    assert await feedback.applications(owner, run_id=mine.run_id) == []
    # A preference never re-admits what policy excludes: owner-only material stays out.
    assert "2026-10-12" not in public_texts
    assert all(e.audience == Audience.PUBLIC for e in narrowed.draft.allowed_evidence)
    # A public request naming the withheld term is denied before the model.
    calls = len(seen)
    denied = await draft(container, owner, query=f"{FALCON} 일정 공개 답변")
    assert denied.status.value == "failed" and len(seen) == calls


class Authority:
    """Test-owned review original: approves explicitly, never inferred."""

    adapter_name = "fixture-review-authority"
    simulated = True

    def __init__(self):
        self.decision = None

    async def submit_draft(self, draft, **kwargs):
        self.decision = ReviewDecision(
            request_id=draft.request_id, trace_id=draft.trace_id, run_id=draft.run_id,
            agent_id=draft.agent_id, domain_id=draft.domain_id, draft_id=draft.draft_id,
            draft_version=draft.version, content_hash=draft.content_hash, target=draft.target,
            decision=ReviewStatus.APPROVED, safe_reason="synthetic decision", simulated=True,
            adapter=self.adapter_name)
        return self.decision

    async def get_decision(self, draft_id):
        return self.decision if self.decision and self.decision.draft_id == draft_id else None


async def test_approval_and_policy_proposals_never_change_policy_or_its_version(env):
    container, owner, seen, feedback = env
    container.service._dependencies = replace(container.service._dependencies,
                                              response=Authority())
    container.service.start(container.checkpoints.saver)
    version = container.policy.policy_version
    approved = await draft(container, owner)
    assert approved.status.value == "completed"
    receipt = await container.drafts.publish(approved.run_id, PublishRequest(
        idempotency_key="pub-fb-1"), owner)
    assert receipt.mode.value == "mock"
    # One approval/publication is not learned as feedback or policy.
    assert await feedback.list(owner) == []
    assert container.policy.policy_version == version
    source = FeedbackSource(kind="draft_review", run_id=approved.run_id,
                            draft_id=approved.draft.draft_id, draft_version=1)
    proposals = [
        await feedback.submit(body("이번처럼 TRIV3 내부 노트는 앞으로 공개해도 돼",
                                   source=source), owner),
        await feedback.submit(body("내부 자료도 항상 공유해 줘", DISCLOSE), owner),
    ]
    assert all((p.category, p.disposition, p.official_policy_changed) == (
        POLICY, "proposal_only", False) for p in proposals)
    assert proposals[0].source == source
    after = await draft(container, owner)
    assert container.policy.policy_version == version
    assert after.draft.policy_version == approved.draft.policy_version
    assert all(p.text not in seen[-1].query for p in proposals)
    assert "2026-10-12" not in " ".join(i.excerpt for i in seen[-1].evidence.items)
    assert await feedback.applications(owner, run_id=after.run_id) == []


async def test_other_users_feedback_is_invisible_and_never_applied(env):
    container, owner, _, feedback = env
    repo = container.repository
    mine = await feedback.submit(body(M01, STYLE), owner)
    other = TrustedPrincipal(user_id="fixture-other-user", authenticated=True)
    for call in (
        feedback.get(mine.feedback_id, other),
        feedback.revoke(mine.feedback_id, FeedbackRevoke(expected_revision=1), other),
        feedback.revisions(mine.feedback_id, other),
    ):
        with pytest.raises(RfaError) as hidden:
            await call
        assert hidden.value.code == "not_found"
    assert await feedback.list(other) == []
    assert await repo.apply_feedback(other, effect="style_guidance", domain_id=DomainId.TRIV3,
                                     target_audience="public", target_channel="preview",
                                     run_id="run-other") == []
    run = await draft(container, owner)
    with pytest.raises(RfaError) as foreign:
        await feedback.submit(body(M01, STYLE, source=FeedbackSource(
            kind="draft_review", run_id=run.run_id)), other)
    assert foreign.value.code == "not_found"
    unauthenticated = TrustedPrincipal(user_id="fixture-owner-001", authenticated=False)
    with pytest.raises(RfaError) as anonymous:
        await feedback.submit(body(M01, STYLE), unauthenticated)
    assert anonymous.value.code == "authentication_required"


def test_migration_12_is_recorded_and_idempotent(tmp_path):
    import asyncio

    from rfa_mas.adapters.local import SqliteWorkRepository

    repo = SqliteWorkRepository(tmp_path / "m.db")
    asyncio.run(repo.initialize())
    asyncio.run(repo.initialize())
    with sqlite3.connect(repo.path) as db:
        versions = [r[0] for r in db.execute(
            "SELECT version FROM rfa_schema_migrations ORDER BY version")]
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert 12 in versions and versions == sorted(set(versions))
    assert {"feedback_items", "feedback_item_revisions", "feedback_applications"} <= tables


# -- M01 over HTTP: classify, confirm, apply in a new session, trace, revoke --------------
async def test_m01_http_flow_traces_the_applied_revision_in_a_new_session(env):
    container, owner, seen, _ = env
    app = create_app(container=container)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as api:
        preview = await api.post("/v1/feedback/classify", json={"text": M01})
        assert preview.status_code == 200
        assert (preview.json()["category"], preview.json()["classified_by"]) == (
            "style_preference", "rule")
        created = await api.post("/v1/feedback", json={
            "text": M01, "category": "style_preference",
            "scope": {"domain_id": "triv3", "target_audience": "public"}})
        assert created.status_code == 201, created.text
        feedback_id = created.json()["feedback_id"]
        session = (await api.post("/v1/sessions")).json()["session_id"]
        run = await api.post(f"/v1/sessions/{session}/work", json={
            "query": "SDK 공개", "domain_id": "triv3", "target": {"audience": "public"}})
        assert run.status_code == 201
        run_id = run.json()["run_id"]
        assert M01 in seen[-1].query
        trace = await api.get(f"/v1/runs/{run_id}/feedback")
        assert [(a["feedback_id"], a["feedback_revision"]) for a in trace.json()] == [
            (feedback_id, 1)]
        revoked = await api.post(f"/v1/feedback/{feedback_id}/revoke",
                                 json={"expected_revision": 1, "reason": "no longer wanted"})
        assert revoked.status_code == 200 and revoked.json()["state"] == "revoked"
        again = await api.post(f"/v1/feedback/{feedback_id}/revoke",
                               json={"expected_revision": 1})
        assert again.status_code == 409
        revisions = await api.get(f"/v1/feedback/{feedback_id}/revisions")
        assert [r["revision"] for r in revisions.json()] == [1, 2]
        listed = await api.get("/v1/feedback", params={"state": "active"})
        assert listed.json() == []
        unclassified = await api.post("/v1/feedback", json={"text": "음 그렇군요"})
        assert unclassified.status_code == 400
        assert unclassified.json()["code"] == "feedback_unclassified"
        proposal = await api.post("/v1/feedback", json={"text": "TRIV3 노트는 공개해도 돼"})
        assert proposal.status_code == 201
        assert proposal.json()["disposition"] == "proposal_only"
        assert proposal.json()["official_policy_changed"] is False
        missing = await api.get("/v1/feedback/feedback-absent")
        assert missing.status_code == 404
        foreign_run = await api.get("/v1/runs/run-absent/feedback")
        assert foreign_run.status_code == 404

