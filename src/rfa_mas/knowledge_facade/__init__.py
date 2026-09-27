"""Provider of RFA_module's knowledge contract (실무대장 facade), outside the frozen DTOs."""

from rfa_mas.knowledge_facade.app import create_knowledge_facade_app
from rfa_mas.knowledge_facade.contract import AskRequest, KnowledgeResult, TaskInfo
from rfa_mas.knowledge_facade.service import KnowledgeFacadeService

__all__ = [
    "AskRequest",
    "KnowledgeFacadeService",
    "KnowledgeResult",
    "TaskInfo",
    "create_knowledge_facade_app",
]
