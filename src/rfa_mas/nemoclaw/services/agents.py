from __future__ import annotations

from collections.abc import Callable

from rfa_mas.nemoclaw.services.presentation import Presentation
from rfa_mas.nemoclaw.services.tasks import TaskService

ASSISTANT_ID = "assistant"
TASK_TO_AGENT_STATUS = {"ready": "running", "applying": "applying", "failed": "stopped"}


class AgentService:
    """Chat partners: the assistant plus one agent per task (D-10). The censor is never listed."""

    def __init__(self, tasks: TaskService, presentation: Callable[[], Presentation]):
        self.tasks, self._presentation = tasks, presentation

    def assistant(self) -> dict:
        look = self._presentation().assistant
        return {"id": ASSISTANT_ID, "name": look.name, "kind": "assistant", "icon": look.icon,
                "color": look.color.model_dump(), "initials": "", "description": look.description,
                "taskId": None, "taskName": None, "desk": None, "status": "running", "itemCount": 0, "tags": [],
                "suggestions": list(look.suggestions)}

    @staticmethod
    def from_task(task: dict) -> dict:
        return {"id": task["agentId"], "name": task["agentName"], "kind": "task", "icon": task["icon"],
                "color": task["color"], "initials": task["initials"], "description": task["description"],
                "taskId": task["id"], "taskName": task["name"], "desk": task["desk"],
                "status": TASK_TO_AGENT_STATUS.get(task["status"], "running"), "itemCount": task["itemCount"],
                "tags": task["tags"], "suggestions": task["suggestions"]}

    def list(self) -> list[dict]:
        return [self.assistant(), *(self.from_task(t) for t in self.tasks.list())]

    def get(self, agent_id: str) -> dict | None:
        if agent_id == ASSISTANT_ID:
            return self.assistant()
        task = self.tasks.get(agent_id)
        return self.from_task(task) if task else None

    def names(self) -> set[str]:
        """Every name a new task or agent may not reuse (UI rule: agent names and task names, incl. the
        assistant and the censor)."""
        out = {self._presentation().assistant.name, "검열 에이전트"}
        for t in self.tasks.list(include_failed=False):
            out |= {t["name"], t["agentName"]}
        return out
