"""P1-005C: the staged-context retrieval boundary is a container-owned, observable component.

Synthetic fixtures only (offline settings, temp SQLite). No capability or fallback is added.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rfa_mas.application.evaluation import fixture_principal
from rfa_mas.bootstrap import StagedContextBoundary, build_container
from rfa_mas.contracts import Audience, DomainId, DraftTarget, WorkRequest, WorkStatus
from rfa_mas.errors import RfaError
from scripts.contract_baseline import offline_settings


@pytest.fixture
def settings(tmp_path):
    return offline_settings(Path(tmp_path).resolve())


@pytest.fixture
async def container(settings):
    instance = build_container(settings)
    await instance.startup()
    try:
        yield instance
    finally:
        await instance.shutdown()


def _observe(container):
    calls: list[tuple[str, bool]] = []
    original = container.context.load

    async def spy(principal, work, request):
        result = await original(principal, work, request)
        calls.append((work.target.audience.value, result is not None))
        return result

    container.context.load = spy
    return calls


async def test_public_target_retrieves_through_the_container_owned_staged_boundary(container):
    assert isinstance(container.context, StagedContextBoundary)
    calls = _observe(container)
    owner = await container.repository.local_principal()
    result = await container.service.run(
        WorkRequest(
            query="TRIV3 공개 트랙",
            domain_id=DomainId.TRIV3,
            target=DraftTarget(audience=Audience.PUBLIC),
        ),
        owner,
    )
    assert result.status in {WorkStatus.COMPLETED, WorkStatus.WAITING_APPROVAL}, result.errors
    # The graph resolved the boundary at call time (late lookup), so the spy saw the call.
    assert calls == [("public", True)]
    assert result.draft is not None
    assert result.draft.adapter.endswith("staged-context-v1")


async def test_unsupported_target_returns_none_and_uses_the_retrieval_port(container):
    calls = _observe(container)
    searched: list[str] = []
    original_search = container.retrieval.search

    async def search(request, **kwargs):
        searched.append(request.run_id)
        return await original_search(request, **kwargs)

    container.retrieval.search = search
    colleague = fixture_principal("colleague")  # fixed trusted identity with company scope
    result = await container.service.run(
        WorkRequest(
            query="사내 비교 privacy 검사 규칙",
            domain_id=DomainId.TRIV3,
            target=DraftTarget(audience=Audience.COMPANY),
        ),
        colleague,
    )
    # No staged service for this target: None, and the ACL-bound retrieval port is used.
    assert calls == [("company", False)]
    assert searched == [result.run_id]


def test_boundary_is_absent_when_the_runtime_is_not_local(tmp_path):
    settings = offline_settings(
        Path(tmp_path).resolve(),
        runtime_backend="http",
        runtime_base_url="https://runtime.invalid",
    )
    try:
        instance = build_container(settings)
    except RfaError as exc:  # an incomplete real backend is refused, never a mock fallback
        assert exc.code == "configuration_error"
        return
    assert instance.context is None
