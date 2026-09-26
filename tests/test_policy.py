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


# -- P1-005 target-specific drafts: share, content screen, egress, staged context -------
import pytest as _pytest  # noqa: E402

from rfa_mas.application.graphs.domain import share_egress_filter  # noqa: E402
from rfa_mas.bootstrap import build_container as _build  # noqa: E402
from rfa_mas.contracts import (  # noqa: E402
    DirectWorkRequest as _Direct,
    DraftTarget as _Target,
    EvidenceBundle as _Bundle,
    EvidenceItem as _Item,
    KnowledgeWrite as _Write,
    SourceLocation as _Loc,
)
from rfa_mas.settings import Settings as _Settings  # noqa: E402

_CANARY = "CANARY_1ON1_Q7"


async def _policy_container(tmp_path):
    container = _build(_Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'p.db'}",
                                 trace_dir=(tmp_path / "traces").resolve()))
    await container.startup()
    owner = await container.repository.local_principal()
    for key, audience, content in (
        ("faq", "public", "SDK 공개 FAQ: 설치 절차는 pip install triv3-sdk"),
        ("mixed", "public", f"SDK 공개 안내와 섞인 개인 1:1 일정 {_CANARY}"),
        ("plan", "owner", "SDK 내부 검증 목표는 2026-10-12"),
    ):
        await container.knowledge.write(_Write.model_validate({
            "domain_id": "triv3", "provenance": {"provider": "note", "namespace": "p1005",
                                                 "external_id": key},
            "provider_revision": "r1", "title": f"SDK {key}", "content": content,
            "synthetic": True, "acl": {"audience": audience}}), owner)
    seen = []
    original = container.model.generate

    async def spy(request):
        seen.append(request)
        return await original(request)

    container.model.generate = spy
    return container, owner, seen


async def test_public_draft_uses_only_shareable_screened_evidence_from_staged_context(tmp_path):
    container, owner, seen = await _policy_container(tmp_path)
    try:
        public = await container.service.run(_Direct(
            query="SDK 설치 절차", domain_id=DomainId.TRIV3,
            target=_Target(audience=Audience.PUBLIC)), owner)
        request = seen[-1]
        assert request.evidence.adapter == "staged-context-v1"
        texts = " ".join(i.excerpt for i in request.evidence.items)
        assert "pip install" in texts and _CANARY not in texts and "2026-10-12" not in texts
        assert all(i.audience == Audience.PUBLIC for i in request.evidence.items)
        assert _CANARY not in public.model_dump_json()
        assert all(e.audience == Audience.PUBLIC for e in public.draft.allowed_evidence)
        mine = await container.service.run(_Direct(
            query="SDK 내부 검증 목표", domain_id=DomainId.TRIV3,
            target=_Target(audience=Audience.OWNER)), owner)
        owner_texts = " ".join(i.excerpt for i in seen[-1].evidence.items)
        assert "2026-10-12" in owner_texts  # Readable by the owner for the owner target.
        assert mine.status.value == "completed"
    finally:
        await container.shutdown()


async def test_staged_context_stats_and_insufficient_are_observable(tmp_path):
    container, owner, seen = await _policy_container(tmp_path)
    try:
        results = []
        original = container.runtime._handlers["domain_task"]

        async def capture(spec, task):
            result = await original(spec, task)
            results.append(result)
            return result

        container.runtime.register("domain_task", capture)
        await container.service.run(_Direct(query="SDK 설치 절차", domain_id=DomainId.TRIV3,
                                            target=_Target(audience=Audience.OWNER)), owner)
        stats = results[-1].output["context"]
        assert stats["loader"] == "staged-context-v1" and stats["loaded_characters"] > 0
        stages = {r["stage"] for r in stats["records"]}
        assert "L0" in stages and "L2" in stages
        assert stats["loaded_characters"] <= stats["budget_characters"]
        none = await container.service.run(_Direct(query="zzzqqq 없는 내용",
                                                   domain_id=DomainId.TRIV3,
                                                   target=_Target(audience=Audience.OWNER)), owner)
        assert seen[-1].evidence.insufficient is True and seen[-1].evidence.items == ()
        assert none.status.value in {"completed", "waiting_approval"}
    finally:
        await container.shutdown()


async def test_public_request_text_with_private_marker_is_denied_before_model(tmp_path):
    container, owner, seen = await _policy_container(tmp_path)
    try:
        before = len(seen)
        result = await container.service.run(_Direct(
            query=f"공개 답변에 {_CANARY} 포함", domain_id=DomainId.TRIV3,
            target=_Target(audience=Audience.PUBLIC)), owner)
        assert result.status.value == "failed" and len(seen) == before
        assert _CANARY not in result.model_dump_json()
    finally:
        await container.shutdown()


@_pytest.mark.parametrize("endpoint", ["local", "cloud"])
def test_egress_screen_withholds_whole_items_by_target_and_endpoint(endpoint):
    def item(sid, audience, text):
        return _Item(source_id=sid, source_revision="r1", location=_Loc(uri=f"rfa://{sid}"),
                     audience=audience, excerpt=text, content_hash="0" * 64,
                     policy_version="local-v1")

    bundle = _Bundle(request_id="r", trace_id="t", run_id="run", agent_id="a",
                     domain_id=DomainId.TRIV3, policy_version="local-v1", simulated=False,
                     adapter="x", items=(item("pub", Audience.PUBLIC, "공개"),
                                         item("co", Audience.COMPANY, "사내"),
                                         item("mine", Audience.OWNER, "개인"),
                                         item("leak", Audience.PUBLIC, f"공개 {_CANARY}")))
    owner, withheld = share_egress_filter(bundle, target=Audience.OWNER, endpoint=endpoint)
    kept = {i.source_id for i in owner.items}
    assert kept == ({"pub", "co", "mine", "leak"} if endpoint == "local" else {"pub", "leak"})
    company, withheld = share_egress_filter(bundle, target=Audience.COMPANY, endpoint=endpoint,
                                            markers=("사내",))
    assert {i.source_id for i in company.items} == {"pub"}
    assert withheld["sensitive"] >= 1
    public, withheld = share_egress_filter(bundle, target=Audience.PUBLIC, endpoint=endpoint)
    assert {i.source_id for i in public.items} == {"pub"} and withheld["share"] == 2
