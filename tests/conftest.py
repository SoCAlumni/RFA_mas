from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from rfa_mas.bootstrap import Container, build_container
from rfa_mas.contracts import TrustedPrincipal
from rfa_mas.settings import Settings


@pytest.fixture
def principal() -> TrustedPrincipal:
    return TrustedPrincipal(
        user_id="fixture-owner-001",
        authenticated=True,
        company_id="local-company",
        business_units=frozenset({"triv3-team", "quantization-research-team"}),
        roles=frozenset({"company", "local_development_identity"}),
    )


@pytest_asyncio.fixture
async def container(tmp_path) -> AsyncIterator[Container]:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'rfa.db'}",
        trace_dir=tmp_path / "traces",
    )
    instance = build_container(settings)
    await instance.startup()
    try:
        yield instance
    finally:
        await instance.shutdown()
