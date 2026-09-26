"""P1-004C deterministic extraction cues: decisions, actions, tracked items and follow-ups.

Synthetic notes shaped like the E2E fixtures (tracked issue with fields and a gate, a meeting
mention, a hypothesis memo, a closed issue). Local SQLite only; no model, network or keys.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from rfa_mas.application.candidates import CandidateService, rank
from rfa_mas.application.knowledge import (
    _normalized,
    is_action,
    is_decision,
    is_field_only,
    sentences,
)
from rfa_mas.bootstrap import build_container
from rfa_mas.contracts import CandidateDecision, DomainId, KnowledgeWrite
from rfa_mas.settings import Settings

CLOCK = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)  # 2026-10-01T09:00:00+09:00
ISSUE = ("상태: open\n담당: 본인\n마감: 2026-10-02\n내용: benchmark B 결과의 실행 환경 checksum을 "
         "확인해야 한다. 확인 전에는 B 결과를 출시 판단의 확정 근거로 쓰지 않는다.")
ISSUE_TITLE = "B 결과 환경 checksum 확인"
MENTION = ("주간 회의 메모: issue #17(B 결과 환경 checksum 확인)을 10월 2일까지 처리해야 한다는 "
           "점을 다시 확인했다.")
IDEA = ("연구 메모. 다른 양자화 방법 C를 적용하면 지연이 6.0ms까지 줄 수 있다는 가설이 있다. "
        "아직 검증 전이며 확정 실측이 아니다. 후속 실험 기한은 정하지 않았다.")


@pytest.mark.parametrize(("sentence", "expected"), [
    ("[합성 fixture R01] TRIV-DEMO SDK의 공식 출시일은 2026-10-20이다.", True),
    ("TRIV-DEMO SDK의 공식 출시일은 2026-10-27로 변경되었다.", True),
    ("결정: SDK 공개 출시일은 2026-10-20이다.", True),
    ("출시일은 2026-10-27로 결정", True),
    ("팀은 금요일에 배포하기로 했다.", True),
    ("설계 문서 템플릿에 결정 기록과 대안 비교 항목이 추가되었다.", False),
    ("내부 검증 목표일은 2026-10-12이다.", False),
    ("이 날짜는 사업부 내부 계획이며 공식 출시일과 다른 항목이다.", False),
    ("확인 전에는 B 결과를 출시 판단의 확정 근거로 쓰지 않는다.", False),
])
def test_decision_cues_need_a_label_predicate_or_official_dated_fact(sentence, expected):
    assert is_decision(sentence) is expected


def test_sentences_fields_and_actions_are_split_verbatim():
    parts = sentences(ISSUE)
    assert "내용: benchmark B 결과의 실행 환경 checksum을 확인해야 한다." in parts
    assert "확인 전에는 B 결과를 출시 판단의 확정 근거로 쓰지 않는다." in parts
    assert sentences("지연 10.0ms, 정확도 81.0%") == ["지연 10.0ms, 정확도 81.0%"]
    assert is_field_only("마감: 2026-10-02") and is_field_only("[합성 fixture R05] 상태: open")
    assert not is_action("마감: 2026-10-02")  # a field is metadata, not an action
    assert is_action("B 결과 환경 checksum 확인 필요, 10월 2일 마감")
    assert is_action(parts[-2]) and not is_action(parts[-1])


def note(key, content, title, revision="r1", expected=None, audience="owner"):
    return KnowledgeWrite.model_validate({
        "domain_id": "triv3", "provenance": {"provider": "note", "namespace": "p1004c",
                                             "external_id": key},
        "provider_revision": revision, "expected_revision": expected, "title": title,
        "content": content, "synthetic": True, "acl": {"audience": audience}})


@pytest.fixture
async def env(tmp_path):
    container = build_container(Settings(_env_file=None,
                                         database_url=f"sqlite:///{tmp_path / 'p1004c.db'}",
                                         trace_dir=(tmp_path / "traces").resolve()))
    await container.startup()
    owner = await container.repository.local_principal()
    service = CandidateService(container.repository, container.knowledge.accumulator,
                               clock=lambda: CLOCK)
    try:
        yield container, owner, service
    finally:
        await container.shutdown()


async def test_tracked_issue_mention_and_idea_become_ranked_deduplicated_candidates(env):
    container, owner, service = env
    kb = container.knowledge
    issue = await kb.write(note("i17", ISSUE, ISSUE_TITLE), owner)
    first = await service.discover(DomainId.TRIV3, owner)
    [alone] = first
    # Action sentence with its source's due field and gate: a blocker due 2026-10-02.
    assert alone.kind == "todo" and alone.content.endswith("checksum을 확인해야 한다.")
    assert alone.due_date == "2026-10-02" and alone.blocker is True
    await kb.write(note("weekly", MENTION, "주간 회의 메모"), owner)
    memo = await kb.write(note("idea", IDEA, "연구 메모: 방법 C 가설"), owner)
    found = await service.discover(DomainId.TRIV3, owner)
    tracked = [c for c in found if c.kind == "todo"]
    assert [c.candidate_id for c in tracked] == [alone.candidate_id]  # the mention merged
    assert {p.source_id for p in tracked[0].parents} >= {issue.document.source_id}
    assert len(tracked[0].parents) == 2 and tracked[0].content == alone.content
    [idea] = [c for c in found if c.kind == "follow_up"]
    assert idea.epistemic_state == "tentative" and idea.due_date is None
    assert "가설" in idea.content and idea.parents[0].source_id == memo.document.source_id
    ranked = rank(found, now=CLOCK)
    assert [r.candidate.kind for r in ranked] == ["todo", "follow_up"]
    # Replay changes nothing.
    again = await service.discover(DomainId.TRIV3, owner)
    assert sorted(c.candidate_id for c in again) == sorted(c.candidate_id for c in found)
    # Closing the tracked issue supersedes it although the older mention still exists.
    closed = ("상태: closed\n완료: 2026-10-01\n내용: benchmark B 결과의 실행 환경 checksum을 "
              "확인했고 A와 같은 fixture 환경임을 확인했다.")
    await kb.write(note("i17", closed, ISSUE_TITLE, revision="r2",
                        expected=issue.document.source_revision), owner)
    remaining = await service.discover(DomainId.TRIV3, owner)
    assert [c.kind for c in remaining] == ["follow_up"]
    hidden = await service.list(DomainId.TRIV3, owner, include_hidden=True)
    assert {c.candidate_id: c.state for c in hidden}[alone.candidate_id] == "superseded"


async def test_closed_issue_never_proposed_and_rejected_follow_up_not_renotified(env):
    container, owner, service = env
    kb = container.knowledge
    await kb.write(note("i15", "상태: closed\n완료: 2026-09-27\n내용: A 로그 수집을 완료했다.",
                        "benchmark A 로그 수집"), owner)
    memo = await kb.write(note("idea", IDEA, "연구 메모: 방법 C 가설"), owner)
    [idea] = await service.discover(DomainId.TRIV3, owner)
    assert idea.kind == "follow_up"
    await service.decide(idea.candidate_id, CandidateDecision(
        decision="reject", reason="범위 밖", resurface_on_new_evidence=False), owner)
    assert await service.discover(DomainId.TRIV3, owner) == []
    await kb.write(note("idea", IDEA + "\n참고: 로그", "연구 메모: 방법 C 가설", revision="r2",
                        expected=memo.document.source_revision), owner)
    assert await service.discover(DomainId.TRIV3, owner) == []
    # A hypothesis in a source that already has an action item adds no second candidate.
    await kb.write(note("todo", "TODO: 다른 방법 6.0ms 재현 확인 필요 (검증 전 가설)", "메모"),
                   owner)
    kinds = [c.kind for c in await service.discover(DomainId.TRIV3, owner)]
    assert kinds == ["todo"]


async def test_extracted_contents_are_verbatim_and_private_parents_stay_owner_only(env):
    container, owner, _ = env
    kb = container.knowledge
    await kb.write(note("i17", ISSUE, ISSUE_TITLE), owner)
    await kb.write(note("r01", "TRIV-DEMO SDK의 공식 출시일은 2026-10-20이다. 공개 자료다.",
                        "릴리즈 안내", audience="public"), owner)
    await kb.write(note("n06", "설계 문서 템플릿에 결정 기록과 대안 비교 항목이 추가되었다.",
                        "템플릿"), owner)
    acc = kb.accumulator
    proposals = await acc.extract(DomainId.TRIV3, owner)
    bodies = {r.metadata.reference.source_id: _normalized(r.content) for r in
              await container.repository.read_sources(
                  DomainId.TRIV3, owner, [p.parents[0] for p in proposals])}
    for proposal in proposals:
        body = bodies[proposal.parents[0].source_id]
        assert proposal.content in body
        assert all(condition in body for condition in proposal.conditions)
    decisions = [p for p in proposals if p.kind == "decision"]
    assert [d.content for d in decisions] == ["TRIV-DEMO SDK의 공식 출시일은 2026-10-20이다."]
    report = await acc.accumulate(DomainId.TRIV3, proposals, owner)
    assert all(item.review_state == "accepted" and item.epistemic_state == "cited"
               for item in report.items)
    derived = await acc.list_derived(DomainId.TRIV3, owner)
    audiences = {d.reference.audience.value for d in derived}
    public = [d for d in derived if d.reference.audience.value == "public"]
    assert audiences == {"owner", "public"} and len(public) == 1  # only the public decision
