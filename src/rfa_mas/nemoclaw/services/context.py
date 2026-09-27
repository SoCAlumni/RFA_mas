from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from rfa_mas.nemoclaw import rank
from rfa_mas.nemoclaw.config import Assignments
from rfa_mas.nemoclaw.services.admin import AdminService
from rfa_mas.nemoclaw.services.agents import AgentService
from rfa_mas.nemoclaw.services.approvals import ApprovalsBackend
from rfa_mas.nemoclaw.services.chat import ChatService
from rfa_mas.nemoclaw.services.conversations import ConversationService
from rfa_mas.nemoclaw.services.inbox import InboxService
from rfa_mas.nemoclaw.services.intake import IntakeService
from rfa_mas.nemoclaw.services.me import MeService
from rfa_mas.nemoclaw.services.presentation import (
    Presentation,
    load_presentation,
    presentation_path,
)
from rfa_mas.nemoclaw.services.sources import SourceService
from rfa_mas.nemoclaw.services.task_create import TaskCreateService
from rfa_mas.nemoclaw.services.tasks import TaskService
from rfa_mas.nemoclaw.store import Store


@dataclass
class FrontendServices:
    store: Store
    agents: AgentService
    me: MeService
    tasks: TaskService
    conversations: ConversationService
    task_create: TaskCreateService
    admin: AdminService
    sources: SourceService
    inbox: InboxService
    chat: ChatService | None = None
    intake: IntakeService | None = None


class _PresentationCache:
    """``frontend.yaml`` re-read when its mtime changes (hand edits show up without a restart)."""

    def __init__(self, path: Path | None):
        self.path = path or presentation_path()
        self._mtime: float | None = None
        self._value = Presentation()

    def __call__(self) -> Presentation:
        mtime = self.path.stat().st_mtime if self.path.exists() else None
        if mtime != self._mtime:
            self._value, self._mtime = load_presentation(self.path), mtime
        return self._value


def build_frontend_services(*, assignments: Callable[[], Assignments], ask_deps, token: str | None,
                            store: Store | None = None, ask_service=None,
                            presentation_file: Path | None = None, ranker=None, composer=None,
                            teams=None, llm=None, runner=None, nemoclaw_bin: str = "nemoclaw", secret: bytes = b"",
                            routing=None, facade_url: str | None = None, approvals_url: str | None = None,
                            approvals_transport=None) -> FrontendServices:
    """``assignments`` is a getter because a spawned team changes the roster at runtime. ``ask_service``
    (the same one behind ``/ask``) powers the SSE chat; its head decides whether the chat ranks and
    composes with the LLM or deterministically (``rank.for_head``). ``teams`` (TeamService) backs
    ``POST /tasks``; ``llm`` (LlmControl), ``runner``, ``secret``, ``routing`` back the admin pages
    (without a runner the sandbox actions only store and say so). ``approvals_url`` is RFA_module's
    approvals server behind the 결재함 (D-20); ``ask_service`` also serves RFA_module's head requests."""
    store = store or Store()
    presentation = _PresentationCache(presentation_file)
    tasks = TaskService(assignments, ask_deps, store, presentation)
    me = MeService(token, presentation)
    conversations = ConversationService(store)
    chat = None
    if ask_service is not None:
        default_ranker, default_composer = rank.for_head(ask_service.deps.head)
        chat = ChatService(ask_service, me, tasks=tasks, conversations=conversations,
                           ranker=ranker or default_ranker, composer=composer or default_composer)
        conversations.is_busy = chat.is_running
    agents = AgentService(tasks, presentation)
    task_create = TaskCreateService(teams=teams, tasks=tasks, agents=agents, store=store, presentation=presentation,
                                    ask_deps=ask_deps)
    admin = AdminService(assignments=assignments, tasks=tasks, store=store, llm=llm, runner=runner,
                         nemoclaw_bin=nemoclaw_bin, secret=secret, routing=routing, facade_url=facade_url)
    sources = SourceService(store, tasks)
    intake = IntakeService(store, ask_service, sources) if ask_service is not None else None
    backend = ApprovalsBackend(approvals_url, transport=approvals_transport) if approvals_url else None
    inbox = InboxService(backend=backend, store=store, intake=intake or IntakeService(store, None, sources), tasks=tasks)
    tasks.pending = lambda: inbox.counts
    return FrontendServices(store=store, agents=agents, me=me, tasks=tasks, conversations=conversations,
                            task_create=task_create, admin=admin, sources=sources, inbox=inbox, chat=chat,
                            intake=intake)
