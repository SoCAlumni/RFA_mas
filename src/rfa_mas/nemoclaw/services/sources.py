"""「소스」: where a task's requests come from (GitHub repositories, Slack channels / DMs), with the
default grade of the requests that arrive through it (D-24). A request from a registered source takes
that source's grade and task; the scopes are informational (RFA_module decides what it polls)."""

from __future__ import annotations

import re
import uuid

from rfa_mas.nemoclaw import audit
from rfa_mas.nemoclaw.store import Store

KINDS = {"github": "GitHub", "slack": "Slack"}
SCOPES = {"github": ("이슈", "PR 코멘트", "Discussions"), "slack": ("멘션", "모든 메시지", "DM")}
GRADES = {"public": "사외", "company": "사내"}
GRADE_ALIASES = {"사외": "public", "사내": "company", "public": "public", "company": "company"}
_GITHUB = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SLACK = re.compile(r"^(#[A-Za-z0-9_가-힣.-]+|DM · .+)$")


class SourceError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class SourceService:
    def __init__(self, store: Store, tasks):
        self.store, self.tasks = store, tasks

    def _view(self, row: dict, stats: dict | None = None) -> dict:
        stats = stats or {}
        return {"id": row["id"], "taskId": row["task_id"], "kind": row["kind"], "kindLabel": KINDS[row["kind"]],
                "target": row["target"], "scopes": self.store.loads(row["scopes"], []), "grade": row["grade"],
                "gradeLabel": GRADES[row["grade"]], "status": "connected", "createdAt": row["created"],
                "requestCount": stats.get("n", 0), "lastRequestAt": stats.get("last")}

    def _stats(self) -> dict[str, dict]:
        rows = self.store.query("SELECT source_id, COUNT(DISTINCT url) AS n, MAX(started) AS last FROM intake "
                                "WHERE source_id IS NOT NULL GROUP BY source_id")
        return {r["source_id"]: r for r in rows}

    def _task(self, task_id: str) -> dict:
        task = self.tasks.get(task_id)
        if task is None or task.get("status") == "failed":
            raise SourceError(404, "unknown_task", "없는 태스크입니다.")
        return task

    def list(self, task_id: str) -> list[dict]:
        self._task(task_id)
        stats = self._stats()
        rows = self.store.query("SELECT * FROM sources WHERE task_id=? ORDER BY created, id", (task_id,))
        return [self._view(r, stats.get(r["id"])) for r in rows]

    def add(self, task_id: str, *, kind: str, target: str, scopes: list[str], grade: str | None,
            authenticated: bool) -> dict:
        if not authenticated:
            raise SourceError(403, "owner_only", "게스트는 바꿀 수 없습니다. 소유자에게 요청하세요.")
        self._task(task_id)
        if kind not in KINDS:
            raise SourceError(422, "invalid_kind", "소스 종류는 GitHub 또는 Slack 입니다.")
        target = (target or "").strip()
        if kind == "github" and not _GITHUB.fullmatch(target):
            raise SourceError(422, "invalid_target", "저장소를 owner/repo 형태로 적어 주세요.")
        if kind == "slack" and not _SLACK.fullmatch(target):
            raise SourceError(422, "invalid_target", '채널은 #채널이름, DM은 "DM · 팀이름"으로 적어 주세요.')
        picked = [s for s in dict.fromkeys(scopes or []) if s in SCOPES[kind]]
        if not picked:
            raise SourceError(422, "scopes_required", "받을 범위를 하나 이상 골라 주세요.")
        grade = GRADE_ALIASES.get((grade or "").strip()) or ("public" if kind == "github" else "company")
        if self.store.one("SELECT id FROM sources WHERE task_id=? AND kind=? AND target=?", (task_id, kind, target)):
            raise SourceError(409, "source_exists", "이미 연결된 소스입니다.")
        now, sid = self.store.now(), f"src-{uuid.uuid4().hex[:8]}"
        self.store.execute("INSERT INTO sources (id, task_id, kind, target, scopes, grade, created, updated) "
                           "VALUES (?,?,?,?,?,?,?,?)", (sid, task_id, kind, target, self.store.dumps(picked), grade, now, now))
        audit.record(kind="admin", verdict="applied", action="source-add",
                     detail={"task": task_id, "kind": kind, "target": target, "grade": grade, "scopes": picked})
        return self._view(self.store.one("SELECT * FROM sources WHERE id=?", (sid,)))

    def remove(self, task_id: str, source_id: str, authenticated: bool) -> None:
        if not authenticated:
            raise SourceError(403, "owner_only", "게스트는 바꿀 수 없습니다. 소유자에게 요청하세요.")
        row = self.store.one("SELECT * FROM sources WHERE id=? AND task_id=?", (source_id, task_id))
        if row is None:
            raise SourceError(404, "unknown_source", "없는 소스입니다.")
        self.store.execute("DELETE FROM sources WHERE id=?", (source_id,))
        audit.record(kind="admin", verdict="applied", action="source-remove",
                     detail={"task": task_id, "kind": row["kind"], "target": row["target"]})

    def match(self, channel: str, target: str, requester: str | None) -> dict | None:
        """The registered source a request came through: GitHub by repository (``owner/repo#N``), Slack
        by channel id (``#C0123ABC`` ↔ ``C0123ABC/ts``) or, for a DM source, by the other party's name."""
        rows = self.store.query("SELECT * FROM sources WHERE kind=? ORDER BY created", (channel,))
        if channel == "github":
            repo = target.split("#", 1)[0].lower()
            hit = next((r for r in rows if r["target"].lower() == repo), None)
        elif channel == "slack":
            chan = target.split("/", 1)[0].upper()
            hit = next((r for r in rows if r["target"].startswith("#") and r["target"][1:].upper() == chan), None)
            if hit is None and requester:
                hit = next((r for r in rows if r["target"].startswith("DM · ")
                            and r["target"][5:].strip() == requester.strip()), None)
        else:
            hit = None
        return self._view(hit) if hit else None
