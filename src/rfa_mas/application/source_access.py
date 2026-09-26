"""Small shared current-source access rules. No identity or policy authority from text."""
from collections.abc import Callable
from dataclasses import dataclass

from rfa_mas.contracts import Audience, DomainId, DraftTarget, TrustedPrincipal
from rfa_mas.errors import RfaError

ProjectResolver = Callable[[str, str | None], frozenset[str]]
LOCAL_ENDPOINTS = frozenset({"local-preview", "local-model", "mock-model"})
BOUND_ROLES = frozenset({"supervisor", "paper_scout", "source_scout", "evidence_reviewer",
                         "result_analyst", "experiment_runner"})


def no_projects(user_id: str, company_id: str | None) -> frozenset[str]:
    return frozenset()


def fresh_principal(principal: TrustedPrincipal) -> TrustedPrincipal:
    try:
        return TrustedPrincipal.model_validate(principal.model_dump(warnings=False), strict=True)
    except (ValueError, AttributeError):
        raise RfaError("policy_denied", "현재 자료 권한이 충분하지 않습니다.") from None


def project_allowed(project, company, principal, resolve: ProjectResolver) -> bool:
    if project is None:
        return True
    if not principal.authenticated or not company or company != principal.company_id:
        return False
    grants = resolve(principal.user_id, principal.company_id)
    return isinstance(grants, frozenset) and project in grants


def permitted(row, principal, resolve, target, audiences, policy_version) -> bool:
    """row contains ACL only; title/body/URI never needed to make this decision."""
    audience = Audience(row["audience"])
    if audience not in audiences or row["policy_version"] != policy_version:
        return False
    if not project_allowed(row["project_id"], row["company_id"], principal, resolve):
        return False
    if target == Audience.PUBLIC and (audience != Audience.PUBLIC or row["project_id"]):
        return False
    if target == Audience.COMPANY and audience not in {Audience.PUBLIC, Audience.COMPANY}:
        return False
    if target == Audience.BUSINESS_UNIT and audience not in {
        Audience.PUBLIC, Audience.COMPANY, Audience.BUSINESS_UNIT
    }:
        return False
    if audience == Audience.PUBLIC:
        return True
    if not principal.authenticated:
        return False
    if audience in {Audience.PRIVATE, Audience.OWNER}:
        return bool(row["owner_id"] and row["owner_id"] == principal.user_id)
    if not row["company_id"] or row["company_id"] != principal.company_id:
        return False
    if audience == Audience.COMPANY:
        return True
    return bool(set(row["memberships"]) & principal.business_units)


@dataclass(frozen=True)
class BoundAccess:
    """Trusted composition object, never deserialized from a user request.

    The resolver returns fresh identity on every call; no grant snapshot is stored.
    Only local owner/public preview recipients are supported in this first reader.
    """
    principal: Callable[[], TrustedPrincipal]
    agent_id: str
    role: str
    domain_id: DomainId
    target: DraftTarget
    endpoint_id: str

    def identity(self, request):
        principal = fresh_principal(self.principal())
        if (
            self.role not in BOUND_ROLES or self.endpoint_id not in LOCAL_ENDPOINTS
            or self.target.channel != "preview"
            or self.target.destination != "local-preview"
            or self.target.audience not in {Audience.OWNER, Audience.PUBLIC}
            or request.principal != principal or request.agent_id != self.agent_id
            or request.role != self.role or request.domain_id != self.domain_id
            or request.target != self.target or request.endpoint_id != self.endpoint_id
        ):
            raise RfaError("policy_denied", "현재 자료 요청 범위를 지원하지 않습니다.")
        return principal
