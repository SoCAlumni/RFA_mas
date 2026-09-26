"""Revalidate immutable draft references against current source/parent policy."""

from rfa_mas.contracts import Audience, DraftBundle, PolicyRequest, TrustedPrincipal
from rfa_mas.errors import RfaError
from rfa_mas.ports import PolicyPort, WorkRepositoryPort


class ResumePolicy:
    def __init__(self, repository: WorkRepositoryPort, policy: PolicyPort) -> None:
        self.repository, self.policy = repository, policy

    async def __call__(self, draft: DraftBundle, principal: TrustedPrincipal) -> None:
        if not principal.authenticated or draft.policy_version != self.policy.policy_version:
            raise RfaError("resume_review_required", "현재 정책으로 새 초안을 검토해야 합니다.")
        reads = await self.repository.read_sources(
            draft.domain_id, principal, draft.allowed_evidence,
            audiences=tuple(Audience), target=draft.target.audience,
            endpoint="local-preview", policy_version=self.policy.policy_version,
        )
        for read in reads:
            document = read.metadata
            membership = next(
                iter(sorted(set(document.required_memberships) & principal.business_units)), None
            )
            decision = await self.policy.evaluate(
                PolicyRequest(
                    request_id=draft.request_id,
                    trace_id=draft.trace_id,
                    run_id=draft.run_id,
                    agent_id=draft.agent_id,
                    domain_id=draft.domain_id,
                    action="resume_share",
                    resource_audience=document.reference.audience,
                    resource_owner_id=document.owner_id,
                    resource_business_unit=membership,
                    resource_company_id=document.company_id,
                    target_audience=draft.target.audience,
                    principal=principal,
                )
            )
            if not decision.allowed or decision.policy_version != draft.policy_version:
                raise RfaError("policy_denied", "현재 정책으로 자료를 공유할 수 없습니다.")
