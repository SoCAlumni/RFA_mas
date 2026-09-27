from __future__ import annotations

from collections.abc import Callable

from rfa_mas.nemoclaw.config import Assignments
from rfa_mas.nemoclaw.store import Store


def grade_label(assignments: Assignments, sandbox: str | None) -> str:
    """Privilege level + security groups of the sandbox, e.g. ``L2 control-plane+intranet-ro``."""
    if not sandbox or sandbox not in assignments.sandboxes:
        return "L? unplaced"
    groups = list(assignments.sandboxes[sandbox].groups)
    level = max((assignments.security_groups[g].privilege for g in groups if g in assignments.security_groups),
                default=0)
    return f"L{level} " + "+".join(groups) if groups else f"L{level} egress-none"


class TaskService:
    """Tasks the head can route to: the static ``ask.yaml`` catalogue plus spawned teams. The SQLite
    ``tasks`` table only holds front-end overrides (name/grade label); placement stays in YAML."""

    def __init__(self, assignments: Callable[[], Assignments], ask_deps, store: Store):
        self._assignments, self.deps, self.store = assignments, ask_deps, store

    def list(self) -> list[dict]:
        a = self._assignments()
        placement = a.placement()
        static_ids = {t.id for t in self.deps.config.tasks}
        overrides = {r["id"]: r for r in self.store.query("SELECT * FROM tasks")}
        out = []
        for task in self.deps.tasks_catalog():
            sandbox = placement.get(task.agent)
            row = overrides.get(task.id, {})
            out.append({"id": task.id, "name": row.get("name") or task.name, "agentId": task.agent, "sandbox": sandbox,
                        "gradeLabel": row.get("grade_label") or grade_label(a, sandbox),
                        "source": "catalog" if task.id in static_ids else "team"})
        return out

    def get(self, task_id: str) -> dict | None:
        return next((t for t in self.list() if t["id"] == task_id), None)
