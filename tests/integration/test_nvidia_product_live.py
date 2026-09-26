"""Opt-in live smoke of the PRODUCT ModelPort path to hosted NVIDIA (P1-002).

WorkService.run -> Supervisor -> domain TaskGraph -> P1-005 share/content/egress screen ->
ObservedPort(model, mode=real) -> NvidiaChatModel behind PublicOnlyEgressGate -> hosted
chat completions. Only synthetic KB notes exist in a temporary store; only the public one may
be sent. Only NVIDIA_BASE_URL/NVIDIA_MODEL/NVIDIA_API_KEY are read from the explicit env file;
every other setting is an explicit offline value (never the user's DB or backends).

Run explicitly (skip is not success):
    RFA_NVIDIA_LIVE=1 RFA_NVIDIA_ENV_FILE=/abs/path/.env.dev \
    RFA_NVIDIA_PRODUCT_EVIDENCE_OUT=/abs/path/new-file.json \
    .venv/bin/python -m pytest -q tests/integration/test_nvidia_product_live.py
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import pytest

from rfa_mas.bootstrap import build_container
from rfa_mas.contracts import (
    Audience,
    DirectWorkRequest,
    DomainId,
    DraftTarget,
    KnowledgeWrite,
    WorkStatus,
)
from rfa_mas.settings import Settings

LIVE_ENABLED = os.environ.get("RFA_NVIDIA_LIVE") == "1"
ENV_FILE = os.environ.get("RFA_NVIDIA_ENV_FILE")
EVIDENCE_OUT = os.environ.get("RFA_NVIDIA_PRODUCT_EVIDENCE_OUT")
ALLOWED_HOST = "integrate.api.nvidia.com"
PUBLIC_FACT = (
    "TRIV3 SDK 공개 FAQ(합성): 공식 출시일은 2026-10-20이고 "
    "설치 명령은 pip install triv3-sdk이다."
)
OWNER_CANARY = "SYNTHETIC_PRIVATE_CANARY_P1002_LIVE_OWNER"

pytestmark = pytest.mark.skipif(
    not (LIVE_ENABLED and ENV_FILE),
    reason="opt-in live NVIDIA product-path smoke; skipped run is not evidence",
)


class RecordingTransport(httpx.AsyncBaseTransport):
    """Real network transport that keeps only request bodies for the privacy assertions."""

    def __init__(self) -> None:
        self.inner = httpx.AsyncHTTPTransport()
        self.bodies: list[bytes] = []
        self.hosts: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.hosts.append(request.url.host)
        self.bodies.append(request.content)
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


def _settings(root: Path) -> Settings:
    env_file = Path(ENV_FILE or "")
    if not env_file.is_absolute() or not env_file.is_file() or env_file.is_symlink():
        pytest.fail("RFA_NVIDIA_ENV_FILE must be an explicit regular file")
    source = Settings(_env_file=env_file)
    if not source.nvidia_model or source.nvidia_api_key is None:
        pytest.fail("NVIDIA_MODEL/NVIDIA_API_KEY are not configured in the env file")
    return Settings(
        _env_file=None,
        app_host="127.0.0.1",
        database_url=f"sqlite:///{root / 'rfa.db'}",
        trace_dir=root / "traces",
        model_provider="nvidia",
        nvidia_base_url=source.nvidia_base_url,
        nvidia_model=source.nvidia_model,
        nvidia_api_key=source.nvidia_api_key,
        # Hosted latency varied from ~20 s to >150 s in P1-002A; bounded, never unbounded.
        http_timeout_seconds=170,
        tool_timeout_seconds=400,
        nvidia_max_output_tokens=1024,
    )


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _root(temporary: str) -> Path:
    return Path(temporary).resolve()


def _write_evidence(serialized: str) -> None:
    out = Path(EVIDENCE_OUT or "")
    assert out.is_absolute() and not out.exists()
    out.write_text(serialized + "\n", encoding="utf-8")


async def test_product_run_reaches_hosted_nvidia_with_public_evidence_only():
    with TemporaryDirectory(prefix="rfa-p1002-live-") as temporary:
        settings = _settings(_root(temporary))
        key = settings.nvidia_api_key.get_secret_value()
        transport = RecordingTransport()
        container = build_container(settings, model_transport=transport)
        await container.startup()
        try:
            owner = await container.repository.local_principal()
            for name, audience, text in (("faq", "public", PUBLIC_FACT),
                                         ("plan", "owner", f"TRIV3 SDK 내부 계획 {OWNER_CANARY}")):
                await container.knowledge.write(KnowledgeWrite.model_validate({
                    "domain_id": "triv3",
                    "provenance": {"provider": "note", "namespace": "p1002-live",
                                   "external_id": name},
                    "provider_revision": "r1", "title": f"TRIV3 SDK {name}", "content": text,
                    "synthetic": True, "acl": {"audience": audience}}), owner)
            started = time.perf_counter()
            result = await container.service.run(DirectWorkRequest(
                query="TRIV3 SDK 공식 출시일 알려줘", domain_id=DomainId.TRIV3,
                target=DraftTarget(audience=Audience.OWNER)), owner)
            wall_ms = (time.perf_counter() - started) * 1000
            ledger = await container.service.observations.ledger(result.run_id, owner)
        finally:
            await container.shutdown()
            await transport.aclose()
        model_rows = [r for r in ledger.observations if r.event.event == "model"]
        finished = [r for r in model_rows if r.event.status != "started"]
        bodies = [body.decode("utf-8", "replace") for body in transport.bodies]
        evidence = {
            "kind": "p1002_product_live_smoke",
            "simulated": False,
            "run_status": result.status.value,
            "stop_reason": result.stop_reason,
            "wall_ms": round(wall_ms, 1),
            "model_duration_ms": [r.event.duration_ms for r in finished],
            "model_mode": sorted({r.event.mode.value for r in model_rows}),
            "model_status": [r.event.status for r in finished],
            "http_attempts": len(bodies),
            "hosts": sorted(set(transport.hosts)),
            "public_fact_sent": [PUBLIC_FACT[:20] in b for b in bodies],
            "owner_canary_sent": any(OWNER_CANARY in b or "내부 계획" in b for b in bodies),
            "draft_audiences": sorted({e.audience.value for e in result.draft.allowed_evidence})
            if result.draft else [],
            "draft_adapter": result.draft.adapter if result.draft else None,
            "draft_chars": len(result.draft.content) if result.draft else 0,
            "draft_sha256": _sha(result.draft.content) if result.draft else None,
            "mentions_release_date": bool(result.draft and "10-20" in result.draft.content
                                          or result.draft and "10월 20일" in result.draft.content),
        }
        serialized = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
        assert key not in serialized + result.model_dump_json() + ledger.model_dump_json()
        if EVIDENCE_OUT:
            _write_evidence(serialized)
        assert set(transport.hosts) == {ALLOWED_HOST}
        assert evidence["owner_canary_sent"] is False
        assert all(evidence["public_fact_sent"])
        assert result.status == WorkStatus.COMPLETED, evidence
        assert evidence["model_mode"] == ["real"] and evidence["draft_audiences"] == ["public"]
        assert "nvidia-chat-completions" in evidence["draft_adapter"]
