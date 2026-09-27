"""Task-team knowledge served straight from ``deploy/nemoclaw/kb/t-<team>.jsonl``.

The four task teams (deploy/nemoclaw/teams.yaml) read synthetic notes built from the front-end
mock's requests: Slack/GitHub/email threads, plans, measurement and meeting notes, approval
records and link lists. They are files, not core knowledge sources: a core ``DomainId`` per team
would change the frozen shared contracts. Ranking and screening mirror the core path — the same
Korean-bigram lexical terms, BM25, relevance gate and private-marker screen — and a note's
disclosure grade sits in its title (``[샘플·공개|사내|기밀]``) for the censor and the team's task
spec to act on.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from rfa_mas.adapters.retrieval import bm25_scores, lexical_terms, relevance_insufficient
from rfa_mas.application.graphs.domain import PRIVATE_CANARY_PATTERN, _sensitive
from rfa_mas.knowledge_facade.contract import KnowledgeResult, TaskInfo

KB_DIR = Path(__file__).resolve().parents[3] / "deploy" / "nemoclaw" / "kb"
_NON_TOKEN = re.compile(r"[^0-9a-z가-힣]+")


@dataclass(frozen=True)
class NoteTask:
    id: str
    name: str
    description: str
    file: str  # under KB_DIR


NOTE_TASKS: tuple[NoteTask, ...] = (
    NoteTask(
        id="ondevice_training",
        name="On Device LLM Training",
        description="온디바이스 LLM 학습(QAT·LoRA·체크포인트) 계획서·측정 노트·문의 대화",
        file="t-training.jsonl",
    ),
    NoteTask(
        id="inference_optimization",
        name="Inference 최적화 연구",
        description="ORBIT 벤치마크·INT4/INT8 양자화·캘리브레이션 진행 노트와 결재 규칙",
        file="t-inference.jsonl",
    ),
    NoteTask(
        id="agent_automation",
        name="사내 Agent 자동화 구축",
        description="PRISM 아키텍처·메신저 연동·에이전트 권한 정책·배포 연동 사례",
        file="t-automation.jsonl",
    ),
    NoteTask(
        id="npu_compiler_sdk",
        name="NPU Compiler SDK",
        description="연산자 지원표·릴리스 노트·알려진 문제·변환 오류 사례",
        file="t-npu.jsonl",
    ),
)


def load_notes(path: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _first_line(content: str, limit: int = 160) -> str:
    first = next((line.strip() for line in content.splitlines() if line.strip()), "")
    return first if len(first) <= limit else first[: limit - 1].rstrip() + "…"


class NoteStore:
    """Read-only; the files are re-read when they change, so a KB edit needs no restart."""

    def __init__(self, tasks: tuple[NoteTask, ...] = NOTE_TASKS, kb_dir: Path = KB_DIR,
                 limit: int = 5) -> None:
        self.tasks = {task.id: task for task in tasks}
        self.kb_dir, self.limit = kb_dir, limit
        self._cache: dict[str, tuple[float, list[dict]]] = {}

    def has_task(self, task_id: str) -> bool:
        return task_id in self.tasks and (self.kb_dir / self.tasks[task_id].file).is_file()

    def _notes(self, task: NoteTask) -> list[dict]:
        path = self.kb_dir / task.file
        mtime = path.stat().st_mtime
        cached = self._cache.get(task.id)
        if cached is None or cached[0] != mtime:
            cached = (mtime, load_notes(path))
            self._cache[task.id] = cached
        return cached[1]

    def list_tasks(self) -> list[TaskInfo]:
        infos = []
        for task in self.tasks.values():
            if not self.has_task(task.id) or not self._notes(task):
                continue  # nothing to read: the task is not "alive" for the writer
            mtime = (self.kb_dir / task.file).stat().st_mtime
            infos.append(TaskInfo(id=task.id, name=task.name, description=task.description,
                                  updated_at=datetime.fromtimestamp(mtime, UTC).date()))
        return infos

    def search(self, task_id: str, question: str) -> list[dict]:
        """Top notes by BM25 over title + content; [] when the relevance gate withholds."""
        notes = [n for n in self._notes(self.tasks[task_id]) if not _sensitive(n["content"], ())]
        terms = lexical_terms(question)
        if not terms or not notes:
            return []
        rows = []
        for note in notes:
            text = f"{note['title']} {note['content']}".lower()
            bounded = f" {_NON_TOKEN.sub(' ', text)} "
            counts = [(bounded if boundary else text).count(f" {term} " if boundary else term)
                      for term, boundary in terms]
            rows.append((note["key"], len(text), *counts))
        frequency = {term: sum(1 for row in rows if row[2 + i] > 0)
                     for i, (term, _) in enumerate(terms)}
        if relevance_insufficient(question, frequency):
            return []
        scores = bm25_scores(rows, len(terms))
        by_key = {note["key"]: note for note in notes}
        ranked = sorted(scores, key=lambda key: (-scores[key], key))
        return [by_key[key] for key in ranked[: self.limit]]

    def ask(self, task_id: str, question: str) -> KnowledgeResult:
        hits = self.search(task_id, question)
        if not hits:
            return KnowledgeResult(task_id=task_id, answer="", confidence=0.0, sources=[])
        body = "\n".join(f"- {n['title']}\n{n['content']} [note:{n['key']}]" for n in hits)
        answer = PRIVATE_CANARY_PATTERN.sub("[REDACTED_PRIVATE_CANARY]", f"근거 노트:\n{body}")
        haystack = "\n".join(f"{n['title']}\n{n['content']}" for n in hits).lower()
        words = {term for term, _ in lexical_terms(question) if len(term) >= 2}
        confidence = round(sum(1 for w in words if w in haystack) / len(words), 2) if words else 0.0
        return KnowledgeResult(
            task_id=task_id,
            answer=answer,
            confidence=min(confidence, 1.0),
            sources=[f"{note['title']}: {_first_line(note['content'])} [note:{note['key']}]"
                     for note in hits],
        )
