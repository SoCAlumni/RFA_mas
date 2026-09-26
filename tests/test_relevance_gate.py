"""P1-001E: relevance gate after authorization, and derived items ranked after their parent.

Deterministic, offline; the gate only ever sees the caller's AUTHORIZED documents.
"""
import re

import pytest

from rfa_mas.adapters.local import LocalPolicy, SqliteWorkRepository
from rfa_mas.adapters.retrieval import (
    RELEVANCE_RULES_VERSION,
    LocalRetrieval,
    lexical_terms,
    query_words,
    relevance_insufficient,
)
from rfa_mas.application.knowledge import KnowledgeService
from rfa_mas.contracts import (
    Audience,
    DirectWorkRequest,
    DomainId,
    DraftTarget,
    KnowledgeWrite,
    RetrievalRequest,
    TrustedPrincipal,
)

OWNER = TrustedPrincipal(user_id="gate-owner", authenticated=True, company_id="company-a",
                         business_units=frozenset({"unit-a"}))
OTHER = TrustedPrincipal(user_id="gate-other", authenticated=True, company_id="company-b",
                         business_units=frozenset({"unit-b"}))
DATE = re.compile(r"20\d\d-\d\d-\d\d")
LAUNCH = ("TRIV-DEMO SDK 출시 결정", "공식 출시일: 2026-10-20\n내부 검증 목표일: 2026-10-12")


def note(identity, title, content, audience="private", **changes):
    acl = {"audience": audience}
    if audience == "company":
        acl["company_id"] = "company-a"
    return KnowledgeWrite.model_validate({
        "domain_id": "triv3", "provenance": {"provider": "note", "namespace": "relevance",
                                             "external_id": identity},
        "provider_revision": "r1", "title": title, "content": content, "synthetic": True,
        "acl": acl, **changes,
    })


def frequency(query, documents):
    """Authorized-document frequency per lexical term, as the SQLite ranker computes it."""
    texts = [d.lower() for d in documents]
    return {term: sum((f" {term} " in f" {t} ") if boundary else (term in t) for t in texts)
            for term, boundary in lexical_terms(query)}


def gate(query, documents):
    return relevance_insufficient(query, frequency(query, documents))


def search(query, principal=OWNER, **changes):
    return RetrievalRequest(request_id="r", trace_id="t", run_id="u", agent_id="a",
                            domain_id=DomainId.TRIV3, query=query,
                            allowed_audiences=tuple(Audience), principal=principal, **changes)


@pytest.fixture
async def kb(tmp_path):
    repo = SqliteWorkRepository(tmp_path / "kb.db")
    await repo.initialize()
    return repo, KnowledgeService(repo, LocalPolicy())


def body_spy(repo, monkeypatch):
    reads = []
    original = repo._read_source_body

    def spy(connection, metadata):
        reads.append(metadata.reference.source_id)
        return original(connection, metadata)

    monkeypatch.setattr(repo, "_read_source_body", spy)
    return reads


# -- rule units ------------------------------------------------------------------------------
def test_rules_are_versioned_and_distinctive_words_skip_request_and_question_words():
    assert RELEVANCE_RULES_VERSION == "relevance-gate-v1"
    words = {w: d for w, _, d in query_words("오늘 경쟁사 SDK 출시일이 언제야? 요약해줘 a")}
    assert words["경쟁사"] and words["sdk"] and words["출시일"]
    assert not words["오늘"] and not words["a"] and not words.get("언제야", False)


def test_r1_no_distinctive_term_covered_is_insufficient():
    docs = [" ".join(LAUNCH)]
    assert gate("모바일 기기 전력 소비", docs)
    assert not gate("SDK 출시일이 언제야?", docs)


def test_r2_unknown_korean_qualifier_before_a_known_latin_subject_is_insufficient():
    docs = [" ".join(LAUNCH)]
    assert gate("경쟁사 SDK 출시일이 언제야?", docs)  # "경쟁사" is in no authorized document
    # An uncovered Latin word is an English synonym candidate, never a withholding reason.
    assert not gate("SDK launch 출시일", docs)
    # A Hangul word counts as covered through its bigrams (the P1-001D matching rule).
    assert not gate("출시일정 SDK", docs)


def test_terms_the_ranker_did_not_count_are_treated_as_covered():
    assert not relevance_insufficient("경쟁사 SDK", {})
    assert relevance_insufficient("경쟁사 SDK", {"경쟁사": 0, "경쟁": 0, "쟁사": 0, "sdk": 0})


# -- retrieval --------------------------------------------------------------------------------
async def test_partial_term_match_becomes_insufficient_without_reading_bodies(kb, monkeypatch):
    repo, service = kb
    launch = await service.write(note("R01", *LAUNCH), OWNER)
    adapter = LocalRetrieval(repo, policy_version="local-v1")
    reads = body_spy(repo, monkeypatch)
    missing = await adapter.search(search("경쟁사 SDK 출시일이 언제야?"))
    assert missing.insufficient and not missing.items and reads == []
    found = await adapter.search(search("SDK 출시일이 언제야?"))
    assert not found.insufficient
    assert [i.source_id for i in found.items] == [launch.document.source_id]


async def test_gate_counts_only_documents_the_caller_may_read(kb):
    repo, service = kb
    await service.write(note("R01", *LAUNCH, audience="public"), OWNER)
    rival = await service.write(
        note("rival", "경쟁사 동향", "경쟁사 SDK 출시일: 2026-11-01 PRIVATE_CANARY"), OWNER)
    adapter = LocalRetrieval(repo, policy_version="local-v1")
    other = await adapter.search(search("경쟁사 SDK 출시일이 언제야?", OTHER))
    # A private document that mentions the term never makes another reader's query answerable.
    assert other.insufficient and not other.items
    assert "PRIVATE_CANARY" not in other.model_dump_json()
    own = await adapter.search(search("경쟁사 SDK 출시일이 언제야?"))
    assert own.items[0].source_id == rival.document.source_id
    public_target = await repo.authorized_metadata(
        DomainId.TRIV3, OWNER, target=Audience.PUBLIC, query="경쟁사 SDK 출시일이 언제야?")
    assert public_target == []  # owner drafting for a public audience: public evidence only


async def test_domain_draft_states_insufficiency_without_a_number_or_date(container, principal):
    await container.knowledge.write(note("R01", *LAUNCH, audience="public"), principal)

    async def run(query):
        return await container.service.run(
            DirectWorkRequest(query=query, domain_id=DomainId.TRIV3,
                              target=DraftTarget(audience=Audience.OWNER)), principal)

    missing = await run("경쟁사 SDK 출시일이 언제야?")
    assert missing.draft is not None and not missing.draft.allowed_evidence
    assert "근거가 부족" in missing.draft.content
    assert not DATE.findall(missing.draft.content) and "2026" not in missing.draft.content
    answered = await run("SDK 출시일이 언제야?")
    assert answered.draft is not None and answered.draft.allowed_evidence
    assert "2026-10-20" in answered.draft.content


# -- observation 7: a source outranks its own derived summary ---------------------------------
async def test_derived_item_ranks_after_its_matching_parent_only(kb):
    repo, service = kb
    parent = await service.write(note(
        "bench", "benchmark 실행 로그",
        "환경: env-01\n지연: 10.0ms\n정확도: 81.0%\n" + "측정 반복 기록 " * 40), OWNER)
    reference = next(m.reference for m in await repo.authorized_metadata(DomainId.TRIV3, OWNER)
                     if m.reference.source_id == parent.document.source_id)
    derived = await repo.write_derived_knowledge(
        note("bench-summary", "benchmark 지연 요약", "benchmark 지연 10.0ms"), OWNER,
        parents=(reference,), policy_version="local-v1")
    orphan_parent = await service.write(note("idea", "아이디어 메모", "새 캐시 방식 검토"), OWNER)
    orphan_ref = next(m.reference for m in await repo.authorized_metadata(DomainId.TRIV3, OWNER)
                      if m.reference.source_id == orphan_parent.document.source_id)
    orphan = await repo.write_derived_knowledge(
        note("idea-summary", "benchmark 지연 가설", "benchmark 지연 가설 요약"), OWNER,
        parents=(orphan_ref,), policy_version="local-v1")
    ranked = [m.reference.source_id for m in await repo.authorized_metadata(
        DomainId.TRIV3, OWNER, query="benchmark 지연")]
    # The short derived summary scores higher than its long parent, yet ranks after it; a
    # derived item whose parent did not match keeps its score position.
    assert ranked.index(parent.document.source_id) < ranked.index(derived.document.source_id)
    assert ranked[-1] == derived.document.source_id
    assert ranked.index(orphan.document.source_id) < ranked.index(parent.document.source_id)
