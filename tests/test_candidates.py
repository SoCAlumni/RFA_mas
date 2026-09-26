"""P1-004B/P2-003 work candidates: local SQLite, synthetic notes, no network or keys."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import httpx
import pytest

from rfa_mas.api.app import create_app, resolve_principal
from rfa_mas.application.candidates import CandidateService, fingerprint
from rfa_mas.bootstrap import build_container
from rfa_mas.contracts import CandidateDecision, DomainId, KnowledgeWrite
from rfa_mas.errors import RfaError
from rfa_mas.settings import Settings

CLOCK = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)  # 2026-10-01T09:00:00+09:00


def note(key, content, revision="r1", expected=None, audience="owner"):
    return KnowledgeWrite.model_validate({
        "domain_id": "triv3", "provenance": {"provider": "note", "namespace": "cand",
                                             "external_id": key},
        "provider_revision": revision, "expected_revision": expected, "title": f"note {key}",
        "content": content, "synthetic": True, "acl": {"audience": audience}})


@pytest.fixture
async def env(tmp_path):
    container = build_container(Settings(_env_file=None,
                                         database_url=f"sqlite:///{tmp_path / 'cand.db'}",
                                         trace_dir=(tmp_path / "traces").resolve()))
    await container.startup()
    owner = await container.repository.local_principal()
    service = CandidateService(container.repository, container.knowledge.accumulator,
                               clock=lambda: CLOCK)
    try:
        yield container, owner, service
    finally:
        await container.shutdown()


def count(container, table):
    with sqlite3.connect(container.repository.path) as db:
        return db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


async def test_discovery_dedups_same_change_and_never_creates_tasks(env):
    container, owner, service = env
    issue = await container.knowledge.write(note(
        "issue17", "issue #17: B 결과 환경 checksum 확인 필요, 담당 본인, 10월 2일 마감, open"), owner)
    await container.knowledge.write(note("memo", "회의 메모: #17 checksum 확인 필요 (중복 언급)"), owner)
    first = await service.discover(DomainId.TRIV3, owner)
    again = await service.discover(DomainId.TRIV3, owner)
    assert [c.candidate_id for c in first] == [c.candidate_id for c in again]
    todo = [c for c in first if "#17" in c.content]
    assert len(todo) == 1  # Two sources mention the same issue: one candidate.
    assert todo[0].due_date == "2026-10-02" and todo[0].blocker is True
    assert todo[0].epistemic_state == "cited" and todo[0].state == "proposed"
    assert todo[0].parents[0].source_id in {issue.document.source_id,
                                            todo[0].parents[0].source_id}
    assert count(container, "product_tasks") == 0 and count(container, "team_slots") == 0
    accepted = await service.decide(todo[0].candidate_id,
                                    CandidateDecision(decision="accept", reason="이번 주"), owner)
    assert accepted.state == "accepted" and count(container, "product_tasks") == 0


async def test_rejected_candidate_stays_hidden_until_new_revision_and_closed_is_superseded(env):
    container, owner, service = env
    first = await container.knowledge.write(note("idea", "TODO: 새 양자화 아이디어 검토 필요"), owner)
    [candidate] = await service.discover(DomainId.TRIV3, owner)
    await service.decide(candidate.candidate_id, CandidateDecision(decision="reject",
                                                                   reason="범위 밖"), owner)
    assert await service.discover(DomainId.TRIV3, owner) == []  # No new evidence.
    await container.knowledge.write(note("idea", "TODO: 새 양자화 아이디어 검토 필요",
                                         revision="r2", expected=first.document.source_revision
                                         ), owner)
    resurfaced = await service.discover(DomainId.TRIV3, owner)
    assert [c.state for c in resurfaced] == ["proposed"]
    assert "resurfaced:new_revision" in resurfaced[0].history[-1]
    await service.decide(resurfaced[0].candidate_id, CandidateDecision(
        decision="reject", reason="보류 없음", resurface_on_new_evidence=False), owner)
    latest = (await container.knowledge.get(first.document.source_id, owner)).document
    await container.knowledge.write(note("idea", "TODO: 새 양자화 아이디어 검토 필요",
                                         revision="r3", expected=latest.source_revision), owner)
    assert await service.discover(DomainId.TRIV3, owner) == []
    # Completion supersedes an open candidate.
    issue = await container.knowledge.write(note("i", "#17 checksum 확인 필요, 10월 2일 마감"),
                                            owner)
    assert len(await service.discover(DomainId.TRIV3, owner)) == 1
    await container.knowledge.write(note("i", "#17 checksum 확인 완료, closed", revision="r2",
                                         expected=issue.document.source_revision), owner)
    assert await service.discover(DomainId.TRIV3, owner) == []
    hidden = await service.list(DomainId.TRIV3, owner, include_hidden=True)
    assert {c.state for c in hidden} == {"rejected", "superseded"}
    with pytest.raises(RfaError):
        await service.decide(next(c for c in hidden if c.state == "superseded").candidate_id,
                             CandidateDecision(decision="accept"), owner)


async def test_other_owner_cannot_read_or_decide_and_api_routes(env):
    container, owner, service = env
    await container.knowledge.write(note("x", "TODO: 설치 가이드 확인 필요"), owner)
    [candidate] = await service.discover(DomainId.TRIV3, owner)
    stranger = owner.model_copy(update={"user_id": "someone-else"})
    assert await service.list(DomainId.TRIV3, stranger) == []
    with pytest.raises(RfaError):
        await service.decide(candidate.candidate_id, CandidateDecision(decision="accept"),
                             stranger)
    app = create_app(container=container)
    app.dependency_overrides[resolve_principal] = lambda: owner
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        listed = await client.post("/v1/candidates/discover", params={"domain_id": "triv3"})
        assert listed.status_code == 200 and len(listed.json()) == 1
        decided = await client.post(f"/v1/candidates/{candidate.candidate_id}/decision",
                                    json={"decision": "defer", "reason": "다음 주"})
        assert decided.status_code == 200 and decided.json()["state"] == "deferred"
        bad = await client.post(f"/v1/candidates/{candidate.candidate_id}/decision",
                                json={"decision": "accept", "task_id": "forged"})
        assert bad.status_code == 422
        derived = await client.post("/v1/knowledge/derive", params={"domain_id": "triv3"})
        assert derived.status_code == 200
        items = await client.get("/v1/knowledge/derived", params={"domain_id": "triv3"})
        assert items.status_code == 200 and items.json()


def test_fingerprint_is_issue_scoped_and_order_independent():
    assert fingerprint("todo", "issue #17 확인", ["a"]) == fingerprint("todo", "#17 다른 문장", ["b"])
    assert fingerprint("todo", "X  확인", ["b", "a"]) == fingerprint("todo", "x 확인", ["a", "b"])

