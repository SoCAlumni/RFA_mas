from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from rfa_mas.nemoclaw.config import Assignments
from rfa_mas.nemoclaw.services.agents import AgentService
from rfa_mas.nemoclaw.services.chat import ChatService
from rfa_mas.nemoclaw.services.me import MeService
from rfa_mas.nemoclaw.services.tasks import TaskService
from rfa_mas.nemoclaw.store import Store


@dataclass
class FrontendServices:
    store: Store
    agents: AgentService
    me: MeService
    tasks: TaskService
    chat: ChatService | None = None


def build_frontend_services(*, assignments: Callable[[], Assignments], ask_deps, token: str | None,
                            store: Store | None = None, ask_service=None) -> FrontendServices:
    """``assignments`` is a getter because a spawned team changes the roster at runtime. ``ask_service``
    (the same one behind ``/ask``) powers the SSE chat."""
    store = store or Store()
    agents = AgentService(assignments)
    me = MeService(token)
    return FrontendServices(store=store, agents=agents, me=me, tasks=TaskService(assignments, ask_deps, store),
                            chat=ChatService(ask_service, me) if ask_service is not None else None)
