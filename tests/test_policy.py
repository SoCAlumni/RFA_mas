from __future__ import annotations

import pytest

from rfa_mas.adapters.local import LocalPolicy
from rfa_mas.adapters.local import SqliteWorkRepository
from rfa_mas.adapters.mock import MockRetrieval
from rfa_mas.contracts import (
    Audience,
    DomainId,
    KnowledgeDocument,
    PolicyRequest,
    RetrievalRequest,
    SourceLocation,
    ToolEffect,
    TrustedPrincipal,
)
from rfa_mas.settings import Settings


def _principal(
    *,
    user_id: str = "principal-001",
    authenticated: bool = True,
    company_id: str | None = None,
    business_units: frozenset[str] = frozenset(),
    roles: frozenset[str] = frozenset(),
) -> TrustedPrincipal:
    return TrustedPrincipal(
        user_id=user_id,
        authenticated=authenticated,
        company_id=company_id,
        business_units=business_units,
        roles=roles,
    )


def _policy_request(
    *,
    principal: TrustedPrincipal,
    resource_audience: Audience,
    target_audience: Audience | None = None,
    resource_owner_id: str | None = None,
    resource_business_unit: str | None = None,
    resource_company_id: str | None = None,
    tool_effect: ToolEffect | None = None,
) -> PolicyRequest:
    return PolicyRequest(
        request_id="req-policy",
        trace_id="trace-policy",
        run_id="run-policy",
        agent_id="assistant-supervisor",
        domain_id=DomainId.TRIV3,
        action="retrieve" if tool_effect is None else "execute_tool",
        resource_audience=resource_audience,
        resource_owner_id=resource_owner_id,
        resource_business_unit=resource_business_unit,
        resource_company_id=resource_company_id,
        target_audience=target_audience,
        tool_effect=tool_effect,
        principal=principal,
    )


@pytest.mark.parametrize(
    ("principal", "resource_audience", "target_audience", "resource_fields", "allowed"),
    [
        (
            _principal(authenticated=False),
            Audience.PUBLIC,
            Audience.PUBLIC,
            {},
            True,
        ),
        (
            _principal(company_id="company-a"),
            Audience.COMPANY,
            Audience.COMPANY,
            {"resource_company_id": "company-a"},
            True,
        ),
        (
            _principal(company_id="company-a"),
            Audience.COMPANY,
            Audience.COMPANY,
            {"resource_company_id": "company-b"},
            False,
        ),
        (
            _principal(roles=frozenset({"company"})),
            Audience.COMPANY,
            Audience.COMPANY,
            {"resource_company_id": "company-a"},
            False,
        ),
        (
            _principal(company_id="company-a",business_units=frozenset({"triv3-team"})),
            Audience.BUSINESS_UNIT,
            Audience.BUSINESS_UNIT,
            {"resource_business_unit": "triv3-team", "resource_company_id":"company-a"},
            True,
        ),
        (
            _principal(business_units=frozenset({"other-team"})),
            Audience.BUSINESS_UNIT,
            Audience.BUSINESS_UNIT,
            {"resource_business_unit": "triv3-team"},
            False,
        ),
        (
            _principal(user_id="owner-001"),
            Audience.OWNER,
            Audience.OWNER,
            {"resource_owner_id": "owner-001"},
            True,
        ),
        (
            _principal(user_id="owner-001"),
            Audience.PRIVATE,
            Audience.PRIVATE,
            {"resource_owner_id": "owner-001"},
            True,
        ),
        (
            _principal(user_id="other-user"),
            Audience.PRIVATE,
            Audience.PRIVATE,
            {"resource_owner_id": "owner-001"},
            False,
        ),
        (
            _principal(user_id="other-user"),
            Audience.OWNER,
            Audience.OWNER,
            {"resource_owner_id": "owner-001"},
            False,
        ),
        (
            _principal(
                user_id="owner-001",
                company_id="company-a",
                business_units=frozenset({"triv3-team"}),
            ),
            Audience.OWNER,
            Audience.PUBLIC,
            {"resource_owner_id": "owner-001"},
            False,
        ),
    ],
    ids=(
        "public-does-not-require-membership",
        "matching-company",
        "different-company",
        "role-without-verified-company-membership",
        "matching-business-unit",
        "different-business-unit",
        "matching-owner",
        "matching-private-owner",
        "different-private-owner",
        "different-owner",
        "public-target-does-not-inherit-owner-access",
    ),
)
async def test_policy_combines_audience_with_verified_membership(
    principal: TrustedPrincipal,
    resource_audience: Audience,
    target_audience: Audience,
    resource_fields: dict[str, str],
    allowed: bool,
) -> None:
    decision = await LocalPolicy().evaluate(
        _policy_request(
            principal=principal,
            resource_audience=resource_audience,
            target_audience=target_audience,
            **resource_fields,
        )
    )

    assert decision.allowed is allowed
    if allowed:
        assert decision.code == "allowed"
    else:
        assert decision.code in {
            "membership_or_audience_denied",
            "target_company_membership_required",
            "target_business_unit_membership_required",
            "target_identity_required",
        }


async def test_p0_denies_write_even_when_feature_gate_is_true() -> None:
    settings = Settings(_env_file=None, allow_external_writes=True)
    assert settings.allow_external_writes is True

    decision = await LocalPolicy().evaluate(
        _policy_request(
            principal=_principal(
                user_id="owner-001",
                company_id="company-a",
                business_units=frozenset({"triv3-team"}),
            ),
            resource_audience=Audience.OWNER,
            target_audience=Audience.OWNER,
            resource_owner_id="owner-001",
            tool_effect=ToolEffect.WRITE,
        )
    )

    assert decision.allowed is False
    assert decision.code == "external_writes_disabled"
    assert decision.allowed_audiences == ()


@pytest.mark.parametrize(
    ("principal", "target", "expected_code"),
    [
        (
            _principal(authenticated=False),
            Audience.OWNER,
            "target_identity_required",
        ),
        (
            _principal(authenticated=True),
            Audience.COMPANY,
            "target_company_membership_required",
        ),
        (
            _principal(authenticated=True, company_id="company-a"),
            Audience.BUSINESS_UNIT,
            "target_business_unit_membership_required",
        ),
    ],
)
async def test_non_public_target_requires_matching_trusted_membership(
    principal: TrustedPrincipal,
    target: Audience,
    expected_code: str,
) -> None:
    decision = await LocalPolicy().evaluate(
        _policy_request(
            principal=principal,
            resource_audience=Audience.PUBLIC,
            target_audience=target,
        )
    )

    assert decision.allowed is False
    assert decision.code == expected_code
    assert decision.allowed_audiences == ()


async def test_public_policy_scope_prevents_private_canary_retrieval(tmp_path) -> None:
    canary = "SYNTHETIC_PRIVATE_CANARY_TRIV3_TEST_DO_NOT_DISCLOSE"
    documents = (
        KnowledgeDocument(
            source_id="triv3-public-test",
            source_revision="1",
            domain_id=DomainId.TRIV3,
            title="TRIV3 public material",
            content="TRIV3 public evidence for a safe public draft.",
            location=SourceLocation(uri="fixture://test/public", section="public"),
            audience=Audience.PUBLIC,
            classification="public",
            policy_version="local-v1",
        ),
        KnowledgeDocument(
            source_id="triv3-private-test",
            source_revision="1",
            domain_id=DomainId.TRIV3,
            title="TRIV3 owner-only material",
            content=f"Owner-only content containing {canary}.",
            location=SourceLocation(uri="fixture://test/private", section="owner"),
            audience=Audience.OWNER,
            classification="private",
            policy_version="local-v1",
            owner_id="fixture-owner-001",
            privacy_canary=True,
        ),
    )
    principal = _principal(
        user_id="fixture-owner-001",
        company_id="local-company",
        business_units=frozenset({"triv3-team"}),
    )
    policy = LocalPolicy()
    decision = await policy.evaluate(
        _policy_request(
            principal=principal,
            resource_audience=Audience.PUBLIC,
            target_audience=Audience.PUBLIC,
        )
    )

    assert decision.allowed is True
    assert decision.allowed_audiences == (Audience.PUBLIC,)

    repository = SqliteWorkRepository(tmp_path / "policy.db")
    await repository.initialize()
    await repository.upsert_documents(list(documents))
    retrieval = MockRetrieval(
        repository,
        policy_version=decision.policy_version,
    )
    evidence = await retrieval.search(
        RetrievalRequest(
            request_id="req-public-canary",
            trace_id="trace-public-canary",
            run_id="run-public-canary",
            agent_id="domain-supervisor:triv3",
            domain_id=DomainId.TRIV3,
            query="TRIV3 evidence",
            allowed_audiences=decision.allowed_audiences,
            principal=principal,
        )
    )

    assert [item.source_id for item in evidence.items] == ["triv3-public-test"]
    assert all(item.audience == Audience.PUBLIC for item in evidence.items)
    assert canary not in evidence.model_dump_json()
    assert "triv3-private-test" not in evidence.model_dump_json()


async def test_retrieval_requires_matching_company_membership(tmp_path) -> None:
    document = KnowledgeDocument(
        source_id="company-a-document",
        source_revision="1",
        domain_id=DomainId.TRIV3,
        title="company-a only",
        content="company-a synthetic evidence",
        location=SourceLocation(uri="fixture://test/company-a"),
        audience=Audience.COMPANY,
        classification="internal",
        policy_version="local-v1",
        company_id="company-a",
    )
    repository = SqliteWorkRepository(tmp_path / "company.db")
    await repository.initialize()
    await repository.upsert_documents([document])
    retrieval = MockRetrieval(repository, policy_version="local-v1")

    evidence = await retrieval.search(
        RetrievalRequest(
            request_id="req-cross-company",
            trace_id="trace-cross-company",
            run_id="run-cross-company",
            agent_id="domain-supervisor:triv3",
            domain_id=DomainId.TRIV3,
            query="company-a evidence",
            allowed_audiences=(Audience.PUBLIC, Audience.COMPANY),
            principal=_principal(company_id="company-b", roles=frozenset({"company"})),
        )
    )

    assert evidence.insufficient is True
    assert evidence.items == ()
