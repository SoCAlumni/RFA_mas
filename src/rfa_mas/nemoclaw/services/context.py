from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from rfa_mas.nemoclaw.config import Assignments
from rfa_mas.nemoclaw.services.agents import AgentService
from rfa_mas.nemoclaw.services.me import MeService
from rfa_mas.nemoclaw.services.tasks import TaskService
from rfa_mas.nemoclaw.store import Store


@dataclass
class FrontendServices:
    store: Store
    agents: AgentService
    me: MeService
    tasks: TaskService


def build_frontend_services(*, assignments: Callable[[], Assignments], ask_deps, token: str | None,
                            store: Store | None = None) -> FrontendServices:
    """``assignments`` is a getter because a spawned team changes the roster at runtime."""
    store = store or Store()
    agents = AgentService(assignments)
    return FrontendServices(store=store, agents=agents, me=MeService(token),
                            tasks=TaskService(assignments, ask_deps, store))
