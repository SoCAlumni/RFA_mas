from __future__ import annotations

from collections.abc import Callable

from rfa_mas.nemoclaw.config import Assignments
from rfa_mas.nemoclaw.services.presentation import (
    Presentation,
    default_agent_name,
    default_description,
    default_suggestions,
    desk_for,
    initials_for,
)
from rfa_mas.nemoclaw.store import Store


def security_label(assignments: Assignments, sandbox: str | None) -> str:
    """Privilege level + security groups of the sandbox, e.g. ``L2 control-plane+intranet-ro``. This is the
    sandbox's network privilege, not the 사내/사외 grade (that one belongs to each request)."""
    if not sandbox or sandbox not in assignments.sandboxes:
        return "L? unplaced"
    groups = list(assignments.sandboxes[sandbox].groups)
    level = max((assignments.security_groups[g].privilege for g in groups if g in assignments.security_groups),
                default=0)
    return f"L{level} " + "+".join(groups) if groups else f"L{level} egress-none"


class TaskService:
    """Tasks the head can route to — the static ``ask.yaml`` catalogue plus spawned teams — each one a chat
    partner (D-10: one task = one agent). Display metadata comes from the SQLite ``tasks`` row (tasks made
    with ``POST /tasks``), else ``frontend.yaml``, else defaults. A row still being created (``applying``)
    is listed before its team reaches the catalogue; a ``failed`` one only answers ``get``."""

    def __init__(self, assignments: Callable[[], Assignments], ask_deps, store: Store,
                 presentation: Callable[[], Presentation], pending: Callable[[], dict[str, int]] | None = None):
        self._assignments, self.deps, self.store, self._presentation = assignments, ask_deps, store, presentation
        self.pending = pending or dict  # task id → pending approvals (결재함), refreshed by the routes

    def _rows(self) -> dict[str, dict]:
        return {r["id"]: r for r in self.store.query("SELECT * FROM tasks ORDER BY created, id")}

    def list(self, *, include_failed: bool = False) -> list[dict]:
        a = self._assignments()
        look = self._presentation()
        placement = a.placement()
        static_ids = [t.id for t in self.deps.config.tasks]
        rows = self._rows()
        catalog = {t.id: t for t in self.deps.tasks_catalog()}
        created = [i for i in rows if i not in static_ids]
        order = static_ids + created + [i for i in catalog if i not in static_ids and i not in rows]
        out = []
        for n, task_id in enumerate(order):
            row, spec = rows.get(task_id), catalog.get(task_id)
            status = (row or {}).get("status") or "ready"
            if spec is None and status == "ready":
                continue  # a row whose team was removed
            if status == "failed" and not include_failed:
                continue
            worker = spec.agent if spec else (row or {}).get("agent_id")
            sandbox = placement.get(worker) if worker else None
            name = (row or {}).get("name") or (spec.name if spec else task_id)
            out.append(self._view(task_id, name, row, look.tasks.get(task_id), look, n - len(static_ids),
                                  source="catalog" if task_id in static_ids else "team", status=status, worker=worker,
                                  sandbox=sandbox, security=security_label(a, sandbox),
                                  keywords=list(spec.keywords) if spec else []))
        return out

    def get(self, task_id: str) -> dict | None:
        return next((t for t in self.list(include_failed=True) if t["id"] == task_id), None)

    def _view(self, task_id: str, name: str, row: dict | None, file_look, look: Presentation, created_index: int, *,
              source: str, status: str, worker: str | None, sandbox: str | None, security: str,
              keywords: list[str]) -> dict:
        row = row or {}
        agent_name = row.get("agent_name") or (file_look.agent_name if file_look else None) or default_agent_name(name)
        tags = self.store.loads(row.get("tags"), None) or (list(file_look.tags) if file_look else [])
        color = self.store.loads(row.get("color"), None) or (
            file_look.color.model_dump() if file_look and file_look.color
            else look.palette[max(created_index, 0) % len(look.palette)].model_dump())
        return {
            "id": task_id, "name": name, "agentId": task_id, "agentName": agent_name,
            "desk": row.get("desk") or (file_look.desk if file_look else None)
                    or (worker if source == "team" and worker else desk_for(agent_name, task_id)),
            "icon": row.get("icon") or (file_look.icon if file_look else "generic"),
            "color": color, "initials": initials_for(agent_name),
            "description": row.get("description") or (file_look.description if file_look else "")
                           or default_description(name),
            "tags": tags,
            "suggestions": self.store.loads(row.get("suggestions"), None)
                           or (list(file_look.suggestions) if file_look and file_look.suggestions else None)
                           or default_suggestions(name, tags),
            "itemCount": self.pending().get(task_id, 0),   # pending approvals (RFA_module 결재 서버)
            "status": status, "error": row.get("error"), "source": source,
            "worker": worker, "sandbox": sandbox, "securityLabel": security, "keywords": keywords,
        }
