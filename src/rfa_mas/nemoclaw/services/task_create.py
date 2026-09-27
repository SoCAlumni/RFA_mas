"""「태스크 추가」 (D-6): one task = one agent (a team whose supervisor answers for the task). The request
is checked up front (owner only, names, uniqueness); then the creation runs in the background and its
steps stream as ``task.*`` events — requirements analysis → team design (pattern selection from the
approved role catalogue) → spawning (declare → ``agents apply`` → seed). A client that disconnects does
not cancel it; ``GET /tasks/{id}`` shows ``applying → ready | failed``. No 사내/사외 grade is chosen: the
grade belongs to each request that later reaches the task (D-0.4)."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import AsyncIterator

from rfa_mas.nemoclaw import audit, logs
from rfa_mas.nemoclaw.services.agents import AgentService
from rfa_mas.nemoclaw.services.presentation import (
    Presentation,
    clean_tags,
    default_agent_name,
    default_description,
    default_suggestions,
    desk_for,
)
from rfa_mas.nemoclaw.services.tasks import TaskService
from rfa_mas.nemoclaw.store import Store

FALLBACK_CAPABILITIES = ["intranet_evidence"]   # a task that names no capability still answers from company knowledge
_LATIN = re.compile(r"[a-z0-9]+")


class TaskCreateError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def task_id_for(name: str, agent_name: str, taken: set[str]) -> str:
    """Latin tokens of the task name (else of the agent name, else a hash), made unique."""
    for source in (name, agent_name):
        tokens = _LATIN.findall(source.lower())
        if tokens and tokens[0][0].isalpha():
            base = "-".join(tokens)[:28].rstrip("-")
            break
    else:
        base = "task-" + hashlib.sha1(name.encode()).hexdigest()[:8]
    out, n = base, 2
    while out in taken:
        out, n = f"{base}-{n}", n + 1
    return out


class TaskCreateService:
    def __init__(self, *, teams, tasks: TaskService, agents: AgentService, store: Store, presentation, ask_deps):
        self.teams, self.tasks, self.agents, self.store = teams, tasks, agents, store
        self._presentation, self.deps = presentation, ask_deps
        self._jobs: set[asyncio.Task] = set()

    def prepare(self, *, name: str, agent_name: str | None, description: str | None, tags: list[str],
                authenticated: bool) -> dict:
        """Validate and reserve the task (``applying`` row). Raises ``TaskCreateError`` with the UI's messages."""
        if self.teams is None:
            raise TaskCreateError(503, "tasks_unavailable", "지금은 태스크를 추가할 수 없습니다.")
        if not authenticated:
            raise TaskCreateError(403, "owner_only", "게스트는 태스크와 에이전트를 추가할 수 없습니다. 소유자에게 요청하세요.")
        name = " ".join((name or "").split())
        if not name:
            raise TaskCreateError(422, "task_name_required", "태스크명을 적어 주세요.")
        agent_name = " ".join((agent_name or "").split()) or default_agent_name(name)
        if name in self.agents.names() or agent_name in self.agents.names():
            raise TaskCreateError(409, "name_conflict", "같은 이름의 태스크나 에이전트가 이미 있습니다.")
        tags = clean_tags(tags)
        taken = {t.id for t in self.deps.tasks_catalog()} | {r["id"] for r in self.store.query("SELECT id FROM tasks")}
        task_id = task_id_for(name, agent_name, taken)
        look: Presentation = self._presentation()
        created = len(self.store.query("SELECT id FROM tasks"))
        color = look.palette[created % len(look.palette)].model_dump()
        now = self.store.now()
        row = {"id": task_id, "name": name, "agent_id": task_id, "agent_name": agent_name,
               "description": (description or "").strip() or default_description(name), "tags": self.store.dumps(tags),
               "suggestions": self.store.dumps(default_suggestions(name, tags)), "desk": desk_for(agent_name, task_id),
               "icon": "generic", "color": self.store.dumps(color), "status": "applying"}
        self.store.execute(f"INSERT INTO tasks ({', '.join(row)}, created, updated) VALUES "
                           f"({', '.join('?' for _ in row)}, ?, ?)", (*row.values(), now, now))
        audit.record(kind="tasks", verdict="applying", action="create", detail={"task": task_id, "tags": tags})
        return {**row, "tags": tags}

    async def run(self, row: dict) -> AsyncIterator[tuple[str, dict]]:
        """Stream the creation; the job itself survives a closed stream."""
        queue: asyncio.Queue = asyncio.Queue()
        job = asyncio.create_task(self._create(row, lambda kind, data: queue.put_nowait((kind, data))))
        self._jobs.add(job)
        job.add_done_callback(self._jobs.discard)
        yield "task.start", {"taskId": row["id"], "name": row["name"], "agentName": row["agent_name"]}
        while True:
            kind, data = await queue.get()
            yield kind, data
            if kind in ("task.done", "task.error"):
                break

    async def _create(self, row: dict, emit) -> None:
        task_id, timer = row["id"], logs.Timer()
        stage_started: dict[str, logs.Timer] = {}
        current = {"stage": "analyze"}

        def progress(kind: str, data: dict) -> None:
            if kind == "stage":
                current["stage"] = data["stage"]
                if data["status"] == "running":
                    stage_started[data["stage"]] = logs.Timer()
                t = stage_started.get(data["stage"])
                emit("task.stage", {**data, "ms": t.ms if t and data["status"] != "running" else 0,
                                    **({} if "detail" in data else {"detail": {}})})
            else:
                emit("task.log", data)

        description = row["description"] + (f"\n태그: {', '.join(row['tags'])}" if row["tags"] else "")
        try:
            status, view = await self.teams.create(name=row["name"], description=description, task_id=task_id,
                                                   sandbox=None, keywords=row["tags"],
                                                   fallback_capabilities=FALLBACK_CAPABILITIES, progress=progress)
        except Exception as exc:  # never leave the row applying
            logs.error("task_create", task=task_id, exception=type(exc).__name__)
            status, view = 500, {"code": "task_create_failed", "detail": type(exc).__name__}
        if status in (200, 201) and view.get("status") == "ready":
            self._set(task_id, "ready", None)
            task = self.tasks.get(task_id)
            emit("task.done", {"task": task, "agent": AgentService.from_task(task), "ms": timer.ms})
        else:
            code = view.get("code") or ("apply_failed" if view.get("status") == "failed" else "task_create_failed")
            detail = view.get("error") or view.get("detail") or code
            self._set(task_id, "failed", str(detail)[:300])
            message = {"no_role_for_requirement": "요구사항에 맞는 역할을 승인된 역할 카탈로그에서 찾지 못했습니다.",
                       "invalid_team": "팀 설계를 검증하지 못했습니다.",
                       "apply_failed": "샌드박스에 에이전트를 올리지 못했습니다."}.get(code, "태스크를 만들지 못했습니다.")
            emit("task.error", {"stage": current["stage"], "code": code, "message": message, "detail": str(detail)[:300]})
        audit.record(kind="tasks", verdict=self.store.one("SELECT status FROM tasks WHERE id=?", (task_id,))["status"],
                     action="create", detail={"task": task_id, "ms": timer.ms})

    def _set(self, task_id: str, status: str, error: str | None) -> None:
        self.store.execute("UPDATE tasks SET status=?, error=?, updated=? WHERE id=?", (status, error, self.store.now(), task_id))
