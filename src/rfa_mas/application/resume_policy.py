"""Revalidate persisted draft references against current local knowledge/policy.

Until the KB owns an explicit current-revision pointer, multiple revisions of a
referenced source are ambiguous and require a new draft, never an old approval.
"""

from rfa_mas.contracts import Audience, DraftBundle, PolicyRequest, TrustedPrincipal, sha256_text
from rfa_mas.errors import RfaError
from rfa_mas.ports import PolicyPort, WorkRepositoryPort


class ResumePolicy:
    def __init__(self, repository: WorkRepositoryPort, policy: PolicyPort) -> None:
        self.repository, self.policy = repository, policy

    async def __call__(self, draft: DraftBundle, principal: TrustedPrincipal) -> None:
        if not principal.authenticated or draft.policy_version != self.policy.policy_version:
            raise RfaError("resume_review_required", "현재 정책으로 새 초안을 검토해야 합니다.")
        documents = await self.repository.list_documents(draft.domain_id.value)
        for reference in draft.allowed_evidence:
            matches = [item for item in documents if item.source_id == reference.source_id]
            if len(matches) != 1:
                raise RfaError("resume_review_required", "현재 근거를 확정할 수 없습니다.")
            document = matches[0]
            if (
                document.source_revision != reference.source_revision
                or sha256_text(document.content) != reference.content_hash
                or document.audience != reference.audience
                or document.location != reference.location
                or document.policy_version != draft.policy_version
            ):
                raise RfaError("resume_review_required", "근거가 변경되어 새 검토가 필요합니다.")
            membership = next(
                iter(sorted(set(document.required_memberships) & principal.business_units)), None
            )
            # Missing trusted ownership/membership metadata cannot relax policy.
            if (
                document.audience == Audience.BUSINESS_UNIT
                and (
                    membership is None
                    or not document.company_id
                    or not principal.company_id
                    or document.company_id != principal.company_id
                )
                or document.audience == Audience.COMPANY
                and not document.company_id
            ):
                raise RfaError("policy_denied", "현재 자료 권한이 충분하지 않습니다.")
            decision = await self.policy.evaluate(
                PolicyRequest(
                    request_id=draft.request_id,
                    trace_id=draft.trace_id,
                    run_id=draft.run_id,
                    agent_id=draft.agent_id,
                    domain_id=draft.domain_id,
                    action="resume_share",
                    resource_audience=document.audience,
                    resource_owner_id=document.owner_id,
                    resource_business_unit=membership,
                    resource_company_id=document.company_id,
                    target_audience=draft.target.audience,
                    principal=principal,
                )
            )
            if not decision.allowed or decision.policy_version != draft.policy_version:
                raise RfaError("policy_denied", "현재 정책으로 자료를 공유할 수 없습니다.")
