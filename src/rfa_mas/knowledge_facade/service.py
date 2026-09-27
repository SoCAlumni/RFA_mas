"""실무대장(Task supervisor) facade behind 승희's knowledge contract.

Composes the existing core ports in the same order as the domain TaskGraph
(policy -> audience-limited retrieval -> share/sensitive/egress screen -> model) and
projects the result onto `KnowledgeResult`. The caller (RFA_module writer) only ever
receives material that the configured facade audience may read AND share; evidence is
withheld whole, never partially redacted here. Confidential review, drafting and
publication remain 승희's pipeline; this service never approves or publishes anything.

Not a graph node and not a new backend: adapters are injected by the caller.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime

from rfa_mas.application.feedback import FeedbackService
from rfa_mas.application.graphs.domain import (
    PRIVATE_CANARY_PATTERN,
    SHAREABLE,
    _sensitive,
    share_egress_filter,
)
from rfa_mas.contracts import (
    Audience,
    DomainId,
    DraftTarget,
    ModelRequest,
    PolicyRequest,
    RetrievalRequest,
    TrustedPrincipal,
    new_id,
)
from rfa_mas.errors import RfaError
from rfa_mas.knowledge_facade.contract import KnowledgeResult, TaskInfo
from rfa_mas.knowledge_facade.notes import NoteStore

FACADE_AGENT_ID = "task-supervisor-facade"
KNOWLEDGE_CHANNEL = "knowledge"
KNOWLEDGE_DESTINATION = "rfa-module"
_TERM = re.compile(r"[0-9A-Za-z가-힣]{2,}")


@dataclass(frozen=True)
class TaskCatalogEntry:
    """Human-readable projection of a knowledge domain as one of 승희's "tasks"."""

    id: str
    name: str
    description: str
    domain_id: DomainId


DEFAULT_CATALOG: tuple[TaskCatalogEntry, ...] = (
    TaskCatalogEntry(
        id=DomainId.TRIV3.value,
        name="TRIV3 벤치마크",
        description="TRIV3 합성 벤치마크 트랙의 진행·결과·근거 (공개 fixture 기준)",
        domain_id=DomainId.TRIV3,
    ),
    TaskCatalogEntry(
        id=DomainId.QUANTIZATION_RESEARCH.value,
        name="Quantization Research",
        description="양자화 연구 비교 기준·실험 노트 (공개 fixture 기준)",
        domain_id=DomainId.QUANTIZATION_RESEARCH,
    ),
)


def lexical_confidence(question: str, excerpts: tuple[str, ...]) -> float:
    """Share of question terms present in the returned evidence (0..1).

    Same meaning as the RFA_module stub's "겹침 비율": a coverage hint for the editor,
    never a factual-accuracy or authorization signal.
    """
    terms = {term.lower() for term in _TERM.findall(question)}
    if not terms or not excerpts:
        return 0.0
    haystack = "\n".join(excerpts).lower()
    hits = sum(1 for term in terms if term in haystack)
    return round(hits / len(terms), 2)


def source_line(title: str | None, excerpt: str, *, limit: int = 160) -> str:
    """ "제목: 요약" one-liner from the first non-empty excerpt line (already screened)."""
    first = next((line.strip() for line in excerpt.splitlines() if line.strip()), "")
    if len(first) > limit:
        first = first[: limit - 1].rstrip() + "…"
    return f"{title}: {first}" if title else first


class KnowledgeFacadeService:
    def __init__(
        self,
        *,
        repository,
        policy,
        retrieval,
        model,
        audience: Audience = Audience.PUBLIC,
        model_endpoint: str = "local",
        disclosure_markers=None,
        catalog: tuple[TaskCatalogEntry, ...] = DEFAULT_CATALOG,
        limit: int = 5,
        notes: NoteStore | None = None,
    ) -> None:
        if audience in {Audience.OWNER, Audience.PRIVATE}:
            # Owner-only material must never be served to an external writer channel.
            raise ValueError("facade audience must be public, company or business_unit")
        self.repository, self.policy, self.retrieval, self.model = (
            repository,
            policy,
            retrieval,
            model,
        )
        self.audience, self.model_endpoint = audience, model_endpoint
        self.disclosure_markers = disclosure_markers
        self.catalog = {entry.id: entry for entry in catalog}
        self.limit = limit
        # Task-team notes (deploy/nemoclaw/kb/t-*.jsonl) beside the core domains; opt-in so the
        # pinned core catalog stays as is (`make knowledge-facade` sets RFA_FACADE_TEAM_NOTES=1).
        if notes is None and os.environ.get("RFA_FACADE_TEAM_NOTES") == "1":
            notes = NoteStore(limit=limit)
        self.notes = notes
        self.started_on: date = datetime.now(UTC).date()

    @classmethod
    def from_container(cls, container, *, audience: Audience = Audience.PUBLIC, **overrides):
        settings = container.settings
        # P1-005B owner disclosure preferences only ever narrow what leaves the facade.
        feedback = FeedbackService(container.repository)
        return cls(
            repository=container.repository,
            policy=container.policy,
            retrieval=container.retrieval,
            model=container.model,
            audience=audience,
            model_endpoint="local" if settings.model_provider == "mock" else "cloud",
            disclosure_markers=feedback.disclosure_markers,
            **overrides,
        )

    def target(self) -> DraftTarget:
        return DraftTarget(
            audience=self.audience, channel=KNOWLEDGE_CHANNEL, destination=KNOWLEDGE_DESTINATION
        )

    async def _principal(self) -> TrustedPrincipal:
        # Server-verified installation owner; the HTTP caller carries no identity claim.
        return await self.repository.local_principal()

    async def _readable_audiences(
        self, principal: TrustedPrincipal, domain_id: DomainId, ids: dict[str, str]
    ) -> tuple[Audience, ...] | None:
        decision = await self.policy.evaluate(
            PolicyRequest(
                request_id=ids["request_id"],
                trace_id=ids["trace_id"],
                run_id=ids["run_id"],
                agent_id=FACADE_AGENT_ID,
                domain_id=domain_id,
                action="retrieve",
                resource_audience=Audience.PUBLIC,
                target_audience=self.audience,
                principal=principal,
            )
        )
        if not decision.allowed:
            return None
        shareable = SHAREABLE[self.audience]
        return tuple(a for a in decision.allowed_audiences if a in shareable)

    async def list_tasks(self) -> list[TaskInfo]:
        principal = await self._principal()
        tasks: list[TaskInfo] = []
        for entry in self.catalog.values():
            ids = _ids()
            audiences = await self._readable_audiences(principal, entry.domain_id, ids)
            if not audiences:
                continue
            metadata = await self.repository.authorized_metadata(
                entry.domain_id,
                principal,
                audiences=audiences,
                policy_version=self.policy.policy_version,
                limit=1,
            )
            if not metadata:
                # Nothing this audience may read: the task is not "alive" for the writer.
                continue
            tasks.append(
                TaskInfo(
                    id=entry.id,
                    name=entry.name,
                    description=entry.description,
                    updated_at=await self._updated_on(principal, entry.domain_id),
                )
            )
        return tasks + (self.notes.list_tasks() if self.notes else [])

    async def _updated_on(self, principal: TrustedPrincipal, domain_id: DomainId) -> date:
        try:
            revisions = await self.repository.list_knowledge(principal, domain_id=domain_id)
        except RfaError:
            revisions = []
        latest = max((r.created_at for r in revisions), default=None)
        return latest.date() if latest is not None else self.started_on

    def has_task(self, task_id: str) -> bool:
        return task_id in self.catalog or bool(self.notes and self.notes.has_task(task_id))

    async def ask(self, task_id: str, question: str) -> KnowledgeResult:
        if task_id not in self.catalog and self.notes:
            return self.notes.ask(task_id, question)
        entry = self.catalog[task_id]
        empty = KnowledgeResult(task_id=task_id, answer="", confidence=0.0, sources=[])
        principal = await self._principal()
        ids = _ids()
        audiences = await self._readable_audiences(principal, entry.domain_id, ids)
        if not audiences:
            return empty
        markers: tuple[str, ...] = ()
        if self.disclosure_markers is not None:
            markers = tuple(
                await self.disclosure_markers(principal, entry.domain_id, target=self.target())
            )
        # The question comes from an external channel: private markers in it mean the
        # writer already holds something it must not, so answer nothing.
        if _sensitive(question, markers):
            return empty
        evidence = await self.retrieval.search(
            RetrievalRequest(
                **ids,
                agent_id=FACADE_AGENT_ID,
                domain_id=entry.domain_id,
                query=question,
                allowed_audiences=audiences,
                principal=principal,
                limit=self.limit,
            )
        )
        screened, _withheld = share_egress_filter(
            evidence, target=self.audience, endpoint=self.model_endpoint, markers=markers
        )
        if screened.insufficient or not screened.items:
            return empty
        titles = await self._titles(principal, entry.domain_id, audiences, question)
        result = await self.model.generate(
            ModelRequest(
                **ids,
                agent_id=FACADE_AGENT_ID,
                domain_id=entry.domain_id,
                query=question,
                evidence=screened,
                target=self.target(),
            )
        )
        answer = PRIVATE_CANARY_PATTERN.sub("[REDACTED_PRIVATE_CANARY]", result.content)
        excerpts = tuple(item.excerpt for item in screened.items)
        return KnowledgeResult(
            task_id=task_id,
            answer=answer,
            confidence=lexical_confidence(question, excerpts),
            sources=[
                source_line(titles.get(item.source_id), item.excerpt) for item in screened.items
            ],
        )

    async def _titles(self, principal, domain_id, audiences, question) -> dict[str, str]:
        metadata = await self.repository.authorized_metadata(
            domain_id,
            principal,
            audiences=audiences,
            policy_version=self.policy.policy_version,
            query=question,
            limit=self.limit,
        )
        return {m.reference.source_id: m.title for m in metadata}


def _ids() -> dict[str, str]:
    return {
        "request_id": new_id("req"),
        "trace_id": new_id("trace"),
        "run_id": new_id("run"),
    }


__all__ = [
    "DEFAULT_CATALOG",
    "FACADE_AGENT_ID",
    "KnowledgeFacadeService",
    "TaskCatalogEntry",
    "lexical_confidence",
    "source_line",
]
