"""Conservative local assignee resolution, independent of intent and authorization.

No LLM or external egress. No team creation. The assistant fallback reads authorized
KB excerpts directly; it does NOT execute an unrelated domain worker.
"""

import re

from rfa_mas.adapters.retrieval import query_words
from rfa_mas.application.chat import TeamCatalogPort
from rfa_mas.contracts import Audience, DomainId, SourceRevisionRef
from rfa_mas.errors import RfaError

DOMAIN_LABELS = {"triv3": "TRIV3 담당", "quantization_research": "양자화 연구 담당"}
GENERIC = {
    "메모",
    "노트",
    "기록",
    "저장",
    "프로젝트",
    "연구",
    "실험",
    "벤치마크",
    "benchmark",
    "research",
    "검증",
    "분석",
    "결과",
    "마감",
    "회의",
    "일정",
    "자료",
}


def subjects(text):
    return {
        word for word, _, distinctive, _ in query_words(text) if distinctive and word not in GENERIC
    }


class LocalChatRouter:
    def __init__(self, repository, catalog: TeamCatalogPort, *, policy_version):
        self.repository, self.catalog, self.policy_version = repository, catalog, policy_version

    @staticmethod
    def describe(record):
        """Safe UI summary of one owner Task team (no runtime/identity claims)."""
        return {
            "task_id": record.task.task_id,
            "team_id": record.task.team_id,
            "goal": record.task.goal[:160],
            "domain_id": record.task.domain_id.value,
            "pattern": record.team.spec.template.pattern,
            "status": record.task.status,
            "team_state": record.reason,
            "selectable": record.task.status == "active" and record.reason == "ready",
        }

    async def assignees(self):
        principal = await self.repository.local_principal()
        return [self.describe(r) for r in await self.catalog.list_for(principal)]

    @staticmethod
    def task_route(record, reason):
        return {
            "kind": "task",
            "label": record.task.goal[:80],
            "reason": reason,
            "task_id": record.task.task_id,
            "team_id": record.task.team_id,
            "domain_id": record.task.domain_id.value,
        }

    async def resolve(self, body):
        principal = await self.repository.local_principal()
        teams = await self.catalog.list_for(principal)
        if body.task_id:
            # Explicit assignee: must be one of the owner's own selectable Task teams.
            for record in teams:
                if record.task.task_id == body.task_id:
                    if record.task.status != "active" or record.reason != "ready":
                        raise RfaError(
                            "task_unavailable", "선택한 Task 팀은 지금 사용할 수 없습니다."
                        )
                    return self.task_route(record, "explicit_task")
            raise RfaError("not_found", "선택한 Task 팀을 찾을 수 없습니다.")
        words = subjects(body.text)
        matches = []
        for record in teams:
            if (
                record.task.status != "active"
                or record.reason != "ready"
                or (body.domain_id and record.task.domain_id != body.domain_id)
            ):
                continue
            goal_words = subjects(record.task.goal)
            shared = goal_words & words
            # An explicit subject must match; generic 'research/result' is insufficient.
            if shared:
                matches.append((len(shared), record))
        matches.sort(key=lambda item: -item[0])
        if matches and (len(matches) == 1 or matches[0][0] > matches[1][0]):
            return self.task_route(matches[0][1], "existing_task_subject_match")
        if matches:
            return self.fallback("ambiguous_tasks")
        domains = []
        if re.search(r"(?<![a-z0-9])triv3(?![a-z0-9])", body.text.lower()):
            domains.append("triv3")
        if "양자화" in body.text or re.search(r"\bquantization\b", body.text.lower()):
            domains.append("quantization_research")
        if body.domain_id:
            domains = [body.domain_id.value]
        if len(domains) == 1:
            domain = domains[0]
            return {
                "kind": "domain",
                "label": DOMAIN_LABELS[domain],
                "domain_id": domain,
                "task_id": None,
                "team_id": None,
                "reason": "explicit_domain" if body.domain_id else "domain_subject_match",
            }
        return self.fallback("ambiguous_domains" if domains else "no_suitable_assignee")

    @staticmethod
    def fallback(reason):
        return {
            "kind": "assistant",
            "label": "비서 직접 처리",
            "reason": reason,
            "domain_id": None,
            "task_id": None,
            "team_id": None,
        }

    async def find_refs(self, query, *, public=False):
        principal = await self.repository.local_principal()
        result = []
        for domain in DomainId:
            metadata = await self.repository.authorized_metadata(
                domain,
                principal,
                query=query,
                limit=3,
                target=Audience.PUBLIC if public else Audience.OWNER,
                audiences=(Audience.PUBLIC,) if public else tuple(Audience),
                policy_version=self.policy_version,
            )
            result.extend(
                {"domain_id": domain.value, "reference": item.reference.model_dump(mode="json")}
                for item in metadata
            )
        return result

    async def excerpts(self, refs, *, public=False):
        principal = await self.repository.local_principal()
        result = []
        for item in refs:
            try:
                reads = await self.repository.read_sources(
                    DomainId(item["domain_id"]),
                    principal,
                    [SourceRevisionRef.model_validate(item["reference"])],
                    target=Audience.PUBLIC if public else Audience.OWNER,
                    audiences=(Audience.PUBLIC,) if public else tuple(Audience),
                    policy_version=self.policy_version,
                )
                result.extend(
                    {
                        "source_id": r.metadata.reference.source_id,
                        "source_revision": r.metadata.reference.source_revision,
                        "excerpt": r.content[:1800],
                    }
                    for r in reads
                )
            except RfaError:
                continue  # Revoked/stale references never replay a cached answer.
        return result
