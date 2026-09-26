"""Owner-authenticated note/export storage. No connector, search engine or egress."""

import json
import re

from rfa_mas.contracts import (
    AccumulatedItem,
    AccumulationReport,
    Audience,
    DerivedItemProposal,
    DomainId,
    KnowledgeDelete,
    KnowledgeExport,
    KnowledgeImportResult,
    KnowledgeImportRow,
    KnowledgeRevision,
    KnowledgeWrite,
    TrustedPrincipal,
    sha256_text,
)
from rfa_mas.errors import RfaError
from rfa_mas.ports import PolicyPort, WorkRepositoryPort


class KnowledgeService:
    def __init__(self, repository: WorkRepositoryPort, policy: PolicyPort):
        self.repository, self.policy = repository, policy
        # P1-004A Supervisor gate shares the same repository/policy; no second KB.
        self.accumulator = KnowledgeAccumulator(repository, policy)

    async def write(
        self,
        request: KnowledgeWrite,
        principal: TrustedPrincipal,
        *,
        source_id: str | None = None,
    ) -> KnowledgeRevision:
        return await self.repository.write_knowledge(
            request,
            principal,
            policy_version=self.policy.policy_version,
            source_id=source_id,
        )

    async def get(
        self, source_id: str, principal: TrustedPrincipal, *, revision: str | None = None
    ):
        return await self.repository.get_knowledge(source_id, principal, revision=revision)

    async def list(self, principal: TrustedPrincipal, *, domain_id: DomainId | None = None):
        return await self.repository.list_knowledge(principal, domain_id=domain_id)

    async def delete(self, source_id: str, request: KnowledgeDelete, principal: TrustedPrincipal):
        return await self.repository.delete_knowledge(source_id, request, principal)

    async def import_export(self, batch: KnowledgeExport, principal: TrustedPrincipal):
        # A transport-authenticated principal is still rechecked by the repository
        # on every independent row. A row never supplies identity or membership grants.
        batch = KnowledgeExport.model_validate_json(batch.model_dump_json(), strict=True)
        try:
            principal = TrustedPrincipal.model_validate(principal.model_dump(), strict=True)
            if principal.authenticated is not True or not principal.user_id:
                raise ValueError("authentication required")
        except (ValueError, AttributeError):
            raise RfaError("authentication_required", "자료 접근에 인증이 필요합니다.") from None
        receipts = []
        for index, row in enumerate(batch.rows):
            try:
                id_field, revision_field = (
                    ("number", "revision")
                    if batch.provider == "github_issue"
                    else ("id", "version")
                )
                allowed = {
                    id_field,
                    revision_field,
                    "title",
                    "body",
                    "updated_at",
                    "acl",
                    "expected_revision",
                }
                if set(row) - allowed:
                    raise ValueError("unknown export field")
                external_id = row[id_field]
                if type(external_id) is int:
                    external_id = str(external_id)
                if not isinstance(external_id, str) or not external_id:
                    raise ValueError("invalid source ID")
                request = KnowledgeWrite.model_validate(
                    {
                        "domain_id": batch.domain_id,
                        "provenance": {
                            "provider": batch.provider,
                            "namespace": batch.namespace,
                            "external_id": external_id,
                        },
                        "provider_revision": row[revision_field],
                        "title": row["title"],
                        "content": row["body"],
                        "acl": row.get("acl", {}),
                        "source_modified_at": row.get("updated_at"),
                        "expected_revision": row.get("expected_revision"),
                        "synthetic": batch.synthetic,
                    }
                )
                record = await self.write(request, principal)
                receipts.append(
                    KnowledgeImportRow(
                        row=index,
                        status="accepted",
                        source_id=record.document.source_id,
                        source_revision=record.document.source_revision,
                    )
                )
            except (ValueError, KeyError, TypeError):
                receipts.append(
                    KnowledgeImportRow(row=index, status="rejected", error_code="invalid_input")
                )
            except RfaError as exc:
                if exc.code not in {"policy_denied", "idempotency_conflict", "not_found"}:
                    # Storage/authentication failures are not successful partial imports.
                    raise
                receipts.append(
                    KnowledgeImportRow(row=index, status="rejected", error_code=exc.code)
                )
        return KnowledgeImportResult(rows=tuple(receipts))

# -- P1-004A reviewed knowledge accumulation -------------------------------------------
DECISION_TERMS = ("결정", "decided", "decision:", "확정:")
TODO_TERMS = ("todo", "해야 할", "해야함", "확인 필요", "마감", "action item")
TENTATIVE_TERMS = ("가설", "검증 전", "미검증", "잠정", "추정", "tentative", "unverified")
URL_PATTERN = re.compile(r"https?://[^\s)>\]]+")


def _normalized(text: str) -> str:
    return " ".join(text.split())


class KnowledgeAccumulator:
    """Deterministic Supervisor gate: proposals become derived knowledge only here.

    Every parent is re-read through the current ACL/revision closure; a "cited" item must
    quote a parent verbatim or it is downgraded to "inferred"; simulated/tentative labels
    are preserved; conflicting decisions stay unresolved. Nothing is promoted to fact.
    """

    def __init__(self, repository: WorkRepositoryPort, policy: PolicyPort):
        self.repository, self.policy = repository, policy

    async def _parents(self, domain_id, principal, parents):
        reads = await self.repository.read_sources(
            domain_id, principal, parents, policy_version=self.policy.policy_version
        )
        if len(reads) != len(parents):
            raise RfaError("resume_review_required", "현재 근거를 확인할 수 없습니다.")
        return reads

    async def accumulate(
        self, domain_id: DomainId, proposals, principal: TrustedPrincipal
    ) -> AccumulationReport:
        proposals = [DerivedItemProposal.model_validate(p.model_dump()) for p in proposals]
        decided: dict[str, set[str]] = {}
        for p in proposals:
            if p.kind == "decision":
                decided.setdefault(_normalized(p.title).lower(), set()).add(_normalized(p.content))
        items = []
        for proposal in proposals:
            try:
                reads = await self._parents(domain_id, principal, proposal.parents)
            except RfaError:
                items.append(AccumulatedItem(kind=proposal.kind, title=proposal.title,
                                             review_state="rejected",
                                             epistemic_state=proposal.epistemic_state,
                                             reason="stale_or_restricted_parent"))
                continue
            state, reason = proposal.epistemic_state, "accepted"
            if state == "cited" and not any(
                _normalized(proposal.content) in _normalized(read.content) for read in reads
            ):
                state, reason = "inferred", "downgraded_uncited"
            if proposal.kind == "decision" and len(
                decided.get(_normalized(proposal.title).lower(), ())
            ) > 1:
                state, reason = "conflicting", "conflicting_decisions"
            public = all(read.metadata.reference.audience == Audience.PUBLIC for read in reads)
            digest = sha256_text(json.dumps([proposal.kind, proposal.title, proposal.content,
                                             [p.source_id for p in proposal.parents]]))
            body = proposal.content
            if proposal.conditions:
                body += "\n조건: " + "; ".join(proposal.conditions)
            if proposal.uncertainty:
                body += "\n불확실성: " + proposal.uncertainty
            write = KnowledgeWrite.model_validate({
                "domain_id": domain_id,
                "provenance": {"provider": "note", "namespace": f"derived/{proposal.kind}",
                               "external_id": f"{proposal.origin_ref}:{digest[:24]}"},
                "provider_revision": f"{state}-{digest[:16]}",
                "title": f"[{proposal.kind}] {proposal.title}"[:1000],
                "content": body,
                "synthetic": True,
                "acl": {"audience": "public" if public else "owner"},
            })
            try:
                stored = await self.repository.write_derived_knowledge(
                    write, principal, parents=tuple(read.metadata.reference for read in reads),
                    policy_version=self.policy.policy_version, epistemic_state=state,
                )
            except RfaError as exc:
                if exc.code != "idempotency_conflict":
                    raise
                items.append(AccumulatedItem(kind=proposal.kind, title=proposal.title,
                                             review_state="rejected", epistemic_state=state,
                                             reason="idempotency_conflict"))
                continue
            items.append(AccumulatedItem(
                kind=proposal.kind, title=proposal.title, review_state="accepted",
                epistemic_state=state, reason=reason,
                source_id=stored.document.source_id,
                source_revision=stored.document.source_revision,
                parents=tuple(read.metadata.reference for read in reads),
            ))
        return AccumulationReport(items=tuple(items))

    async def refresh_changed(self, domain_id: DomainId, principal: TrustedPrincipal,
                              source_ids: set[str], *,
                              origin_ref: str = "kb_refresh") -> AccumulationReport:
        """P0-024 kb_refresh: re-derive only items whose parents have new revisions.

        Same Supervisor gate as accumulate(); unchanged sources are not reprocessed.
        """
        proposals = [
            proposal
            for proposal in await self.extract(domain_id, principal, origin_ref=origin_ref)
            if any(parent.source_id in source_ids for parent in proposal.parents)
        ]
        return await self.accumulate(domain_id, proposals, principal)

    async def extract(self, domain_id: DomainId, principal: TrustedPrincipal,
                      *, origin_ref: str = "extract") -> list[DerivedItemProposal]:
        """Line rules over the principal's current, authorized, non-derived sources."""
        metadata = await self.repository.authorized_metadata(
            domain_id, principal, policy_version=self.policy.policy_version
        )
        originals = [m.reference for m in metadata if not m.parents]
        reads = await self.repository.read_sources(
            domain_id, principal, originals, policy_version=self.policy.policy_version
        ) if originals else []
        proposals, seen = [], set()
        for read in reads:
            ref = read.metadata.reference
            for raw in read.content.splitlines():
                line = _normalized(raw)
                if not line:
                    continue
                lowered = line.lower()
                found = []
                if any(t in lowered for t in DECISION_TERMS):
                    found.append(("decision", "cited"))
                if any(t in lowered for t in TODO_TERMS):
                    found.append(("todo", "cited"))
                if any(t in lowered for t in TENTATIVE_TERMS):
                    found.append(("summary", "tentative"))
                for url in URL_PATTERN.findall(line):
                    found.append(("link", "cited"))
                for kind, state in found:
                    content = URL_PATTERN.findall(line)[0] if kind == "link" else line
                    key = (kind, content)
                    if key in seen:
                        continue
                    seen.add(key)
                    proposals.append(DerivedItemProposal(
                        kind=kind, title=f"{read.metadata.title[:80]} — {kind}",
                        content=content[:5000], epistemic_state=state, parents=(ref,),
                        uncertainty="검증 전 주장" if state == "tentative" else None,
                        origin_ref=f"{origin_ref}:{ref.source_id}"[:160],
                    ))
        return proposals

    async def team_proposals(self, domain_id: DomainId, principal: TrustedPrincipal, result):
        """Proposals from a completed TeamRunResult; numbers stay simulated/tentative."""
        metadata = await self.repository.authorized_metadata(
            domain_id, principal, policy_version=self.policy.policy_version
        )
        refs = {(m.reference.source_id, m.reference.source_revision): m.reference
                for m in metadata}
        runs = {r.get("label"): r for r in
                result.findings.get("experiment_runner", {}).get("runs", [])}
        proposals = []
        for item in result.findings.get("result_analyst", {}).get("comparisons", []):
            pair = [runs.get(item.get("baseline")), runs.get(item.get("candidate"))]
            parents = tuple(refs[(r["source_id"], r["source_revision"])] for r in pair
                            if r and (r["source_id"], r["source_revision"]) in refs)
            if len(parents) != 2:
                continue
            proposals.append(DerivedItemProposal(
                kind="summary",
                title=f"{item['baseline']}→{item['candidate']} 지표 비교",
                content=(f"{item['baseline']}→{item['candidate']}: 지연 "
                         f"{item.get('latency_change_pct')}% 변화, 정확도 "
                         f"{item.get('accuracy_delta_pp')}%p 변화"),
                epistemic_state="simulated",
                parents=parents,
                conditions=("동일 fixture 환경",) if item.get("same_environment") else (),
                uncertainty="합성 로그 파싱·계산이며 실측이 아님",
                origin_ref=f"team:{result.run_id}"[:160],
            ))
        for run in runs.values():
            key = (run.get("source_id"), run.get("source_revision"))
            if run.get("tentative") and key in refs:
                proposals.append(DerivedItemProposal(
                    kind="summary", title=f"{run.get('label')} 미검증 수치",
                    content=f"{run.get('label')}: 지연 {run.get('latency_ms')}ms (검증 전 가설)",
                    epistemic_state="tentative", parents=(refs[key],),
                    uncertainty="원문이 검증 전 가설로 표시", origin_ref=f"team:{result.run_id}"[:160],
                ))
        return proposals

    async def list_derived(self, domain_id: DomainId, principal: TrustedPrincipal):
        """Current derived items whose whole parent closure is still readable."""
        metadata = await self.repository.authorized_metadata(
            domain_id, principal, policy_version=self.policy.policy_version
        )
        return [m for m in metadata if m.parents]
