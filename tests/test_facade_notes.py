"""Task-team notes (deploy/nemoclaw/kb/t-*.jsonl) served by the facade beside core domains."""

from __future__ import annotations

import asyncio
import json
import re

from rfa_mas.knowledge_facade.notes import KB_DIR, NOTE_TASKS, NoteStore, NoteTask, load_notes
from rfa_mas.knowledge_facade.service import KnowledgeFacadeService
from rfa_mas.nemoclaw.config import load_teams

GRADE = re.compile(r"^\[샘플·(공개|사내|기밀)\] ")


def test_every_team_has_a_graded_mixed_kb():
    declared = {team.team_id for team in load_teams().teams}
    keys = []
    for task in NOTE_TASKS:
        team_id = task.file.removesuffix(".jsonl")
        assert team_id in declared
        notes = load_notes(KB_DIR / task.file)
        keys += [n["key"] for n in notes]
        assert notes and {n["domain_id"] for n in notes} == {task.id}
        assert all(GRADE.match(n["title"]) for n in notes), task.id
        kinds = {n["kind"] for n in notes}
        # conversations, notes and a link list, so retrieval sees every style the desk sends
        assert kinds & {"slack", "github", "email"} and "links" in kinds, (task.id, kinds)
        assert any("http" in n["content"] for n in notes), task.id
    assert len(keys) == len(set(keys))


def test_request_questions_find_their_notes():
    store = NoteStore()
    cases = {
        "npu_compiler_sdk": ("Conv3D에서 unsupported op 오류. 우회 방법이 있나요?", "t-npu-kb-09"),
        "ondevice_training": ("LoRA 1B rank 8 학습 메모리, 8GB 기기 가능?", "t-training-kb-11"),
        "agent_automation": ("에이전트가 배포 승인까지 직접 할 수 있나요?", "t-automation-kb-08"),
        "inference_optimization": ("INT4 캘리브레이션 데이터 샘플 수와 분포", "t-inference-kb-08"),
    }
    for task_id, (question, key) in cases.items():
        result = store.ask(task_id, question)
        assert f"[note:{key}]" in result.sources[0], (task_id, result.sources[:2])
        assert result.answer.startswith("근거 노트:") and 0 < result.confidence <= 1


def test_unrelated_question_and_private_markers_return_nothing(tmp_path):
    note = {"key": "k1", "domain_id": "x", "kind": "doc", "grade": "공개",
            "title": "[샘플·공개] Conv3D", "content": "Conv3D 우회 SYNTHETIC_PRIVATE_CANARY_ABC"}
    line = json.dumps(note, ensure_ascii=False) + "\n"
    (tmp_path / "t-x.jsonl").write_text(line, encoding="utf-8")
    store = NoteStore(tasks=(NoteTask("x", "X", "d", "t-x.jsonl"),), kb_dir=tmp_path)
    # withheld whole, like the core egress screen
    assert store.ask("x", "Conv3D 우회").sources == []
    assert NoteStore().ask("npu_compiler_sdk", "지난 분기 회식비 정산 내역").sources == []


class _Repository:
    async def local_principal(self):
        return None


def test_facade_lists_and_routes_note_tasks():
    facade = KnowledgeFacadeService(repository=_Repository(), policy=None, retrieval=None,
                                    model=None, catalog=(), notes=NoteStore())
    assert {t.id for t in asyncio.run(facade.list_tasks())} == {t.id for t in NOTE_TASKS}
    assert facade.has_task("npu_compiler_sdk") and not facade.has_task("nope")
    result = asyncio.run(facade.ask("npu_compiler_sdk", "LayerNorm 단계에서 변환 실패 원인"))
    assert result.sources and "LayerNorm" in result.answer
