"""Opt-in live smoke: product Research worker -> ToolPort -> official nemo-retriever Skill.

P1-003 product path. The pinned NVIDIA nemo-retriever CLI ingests the fictional Korean PDFs
from fixtures/retriever into a temporary LanceDB (hosted embedding on a CPU host), then one
product Research Run is executed with RETRIEVER_BACKEND=nemo_cli. source_scout must reach
the Skill through TeamRunner -> ObservedPort(mode=real) -> NemoRetrieverTool and keep
source/page/revision provenance. No model answer is requested here; DRAFT text stays local.

Run explicitly (skip is not success):
    RFA_RETRIEVER_PRODUCT_LIVE=1 RFA_NVIDIA_ENV_FILE=/abs/.env.dev \
    RFA_RETRIEVER_BIN=/abs/tool-venv/bin/retriever \
    RFA_RETRIEVER_PRODUCT_EVIDENCE_OUT=/abs/evidence.json \
    .venv/bin/python -m pytest -q tests/integration/test_retriever_product_live.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from rfa_mas.adapters.nemo_retriever import ENV_ALLOWLIST, SKILL_CLI_VERSION, TOOL_NAME
from rfa_mas.bootstrap import build_container
from rfa_mas.contracts import (
    Audience,
    DirectWorkRequest,
    DomainId,
    DraftTarget,
    TeamExecutionRequest,
    WorkStatus,
)
from rfa_mas.settings import Settings

LIVE_ENABLED = os.environ.get("RFA_RETRIEVER_PRODUCT_LIVE") == "1"
ENV_FILE = os.environ.get("RFA_NVIDIA_ENV_FILE")
RETRIEVER_BIN = os.environ.get("RFA_RETRIEVER_BIN")
EVIDENCE_OUT = os.environ.get("RFA_RETRIEVER_PRODUCT_EVIDENCE_OUT")

FIXTURES = Path(__file__).parent / "fixtures" / "retriever"
DOCUMENTS = ("synthetic_alpha_minutes.pdf", "synthetic_security_policy.pdf")
TABLE = "rfa-product-synthetic"
INDEX_ID = "rfa-synthetic-public-pdfs"
# TeamSelector admits research-shaped goals; a bare question is denied before any role.
QUESTION = "베타 출시일과 담당자 자료 조사"
EXPECTED = ("synthetic_alpha_minutes p.2", 2, ("11월 14일", "김하늘"))
INGEST_TIMEOUT_SECONDS = 300
_FAILURE: dict[str, str] = {}

pytestmark = pytest.mark.skipif(
    not (LIVE_ENABLED and ENV_FILE and RETRIEVER_BIN),
    reason="opt-in live product Skill path; skipped run is not evidence",
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ProductRun:
    def __init__(self, env_file: Path, binary: Path, workdir: Path) -> None:
        secret = Settings(_env_file=env_file).nvidia_api_key
        if secret is None or not secret.get_secret_value():
            pytest.fail("NVIDIA_API_KEY missing in the explicit env file")
        self._secret = secret
        self.binary, self.workdir = binary, workdir
        self.index_dir = workdir / "index"
        self.results: dict[str, Any] = {}

    def leaks(self, text: str) -> bool:
        return self._secret.get_secret_value() in text

    def ingest(self) -> None:
        docs = self.workdir / "docs"
        docs.mkdir()
        sources = {}
        for name in DOCUMENTS:
            target = docs / name
            shutil.copyfile(FIXTURES / name, target)
            digest = _sha256(target.read_bytes())
            sources[target.stem] = {
                "source_id": f"retriever:synthetic/{target.stem}",
                "source_revision": f"pdf-sha256-{digest[:16]}",
            }
        self.index_dir.mkdir()
        env = {name: os.environ[name] for name in ENV_ALLOWLIST if name in os.environ}
        env["NVIDIA_API_KEY"] = self._secret.get_secret_value()
        argv = [
            str(self.binary),
            "ingest",
            *(str(docs / n) for n in DOCUMENTS),
            "--method",
            "pdfium",
            "--lancedb-uri",
            str(self.index_dir / "lancedb"),
            "--table-name",
            TABLE,
        ]
        started = time.monotonic()
        done = subprocess.run(
            argv,
            cwd=self.workdir,
            env=env,
            capture_output=True,
            text=True,
            timeout=INGEST_TIMEOUT_SECONDS,
            check=False,
        )
        self.results["ingest"] = {
            "argv": ["retriever", *argv[1:]],
            "exit_code": done.returncode,
            "seconds": round(time.monotonic() - started, 2),
        }
        if done.returncode != 0:
            pytest.fail("retriever ingest failed; output withheld (may echo environment)")
        manifest = {
            "index_id": INDEX_ID,
            "lancedb_dir": "lancedb",
            "table_name": TABLE,
            "domain_id": "triv3",
            "audience": "public",
            "synthetic": True,
            "policy_version": "local-v1",
            "sources": sources,
        }
        (self.index_dir / "index_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.results["manifest_sources"] = sources

    async def research(self) -> None:
        settings = Settings(
            _env_file=None,
            database_url=f"sqlite:///{self.workdir / 'product.db'}",
            trace_dir=(self.workdir / "traces").resolve(),
            retriever_backend="nemo_cli",
            retriever_index_dir=self.index_dir,
            retriever_cli_path=self.binary,
            nvidia_api_key=self._secret,
        )
        container = build_container(settings)
        await container.startup()
        try:
            owner = await container.repository.local_principal()
            started = time.monotonic()
            result = await container.service.run(
                DirectWorkRequest(
                    query=QUESTION,
                    domain_id=DomainId.TRIV3,
                    target=DraftTarget(audience=Audience.OWNER),
                    team=TeamExecutionRequest(
                        goal=QUESTION, outputs=("research_report",), requested_pattern="research"
                    ),
                ),
                owner,
            )
            self.results["run_seconds"] = round(time.monotonic() - started, 2)
            self.results["status"] = result.status.value
            self.results["errors"] = [e.code for e in result.errors]
            if result.status != WorkStatus.COMPLETED:
                pytest.fail(f"research run {result.status.value}: {self.results['errors']}")
            team = await container.service.team_result(result.run_id, owner)
            ledger = await container.service.observations.ledger(result.run_id, owner)
            self.results.update(
                status=result.status.value,
                errors=[e.code for e in result.errors],
                draft_evidence=[e.source_id for e in result.draft.allowed_evidence]
                if result.draft
                else [],
                team_status=team.status,
                roles=[(r.role, r.status, r.tool_calls) for r in team.roles],
                scout=team.findings.get("source_scout", {}),
                tool_modes=sorted(
                    {o.event.mode.value for o in ledger.observations if o.event.event == "tool"}
                ),
                tool_calls_observed={c.boundary: c.calls for c in ledger.coverage}.get("tool"),
            )
            self.results["dumps"] = result.model_dump_json() + team.model_dump_json()
            self.results["dumps"] += ledger.model_dump_json()
        finally:
            await container.shutdown()
        self.results["traces"] = "".join(
            p.read_text(errors="replace")
            for p in (self.workdir / "traces").rglob("*")
            if p.is_file()
        )


@pytest.fixture(scope="module")
def product_run(tmp_path_factory: pytest.TempPathFactory) -> ProductRun:
    # A module fixture error is re-raised per test; never repeat hosted calls after one.
    if "reason" in _FAILURE:
        pytest.fail(_FAILURE["reason"])
    env_file = Path(ENV_FILE or "")
    if env_file.is_symlink() or not env_file.is_file():
        pytest.fail("RFA_NVIDIA_ENV_FILE must be an explicit regular file")
    binary = Path(RETRIEVER_BIN or "")
    if not binary.is_file():
        pytest.fail("RFA_RETRIEVER_BIN must point to the pinned retriever executable")
    run = ProductRun(env_file, binary.resolve(), tmp_path_factory.mktemp("retriever-product"))
    started = time.monotonic()
    try:
        run.ingest()
        asyncio.run(run.research())
    except BaseException as exc:
        _FAILURE["reason"] = f"product live run failed once ({type(exc).__name__}); not retried"
        raise
    run.results["wall_seconds"] = round(time.monotonic() - started, 2)
    _write_evidence(run)
    return run


def test_research_run_completes_and_source_scout_used_the_skill(product_run: ProductRun) -> None:
    r = product_run.results
    assert r["status"] == WorkStatus.COMPLETED.value, r["errors"]
    assert r["roles"][0][0] == "source_scout" and r["roles"][0][1] == "succeeded"
    search = r["scout"]["external_search"]
    assert search["tool"] == TOOL_NAME and search["index_id"] == INDEX_ID
    assert search["status"] == "succeeded" and search["simulated"] is False
    assert search["used"] >= 1 and search["unmapped"] == 0


def test_top_evidence_keeps_source_page_revision_and_facts(product_run: ProductRun) -> None:
    r = product_run.results
    external = [s for s in r["scout"]["sources"] if s.get("origin") == "nemo-retriever-cli"]
    citation, page, facts = EXPECTED
    matches = [s for s in external if s["title"] == citation]
    assert matches, [s["title"] for s in external]
    top = matches[0]
    stem = citation.split(" p.")[0]
    assert top["page"] == page
    assert top["source_id"] == f"retriever:synthetic/{stem}"
    assert top["source_revision"] == r["manifest_sources"][stem]["source_revision"]
    assert top["audience"] == "public" and top["fidelity"] == "verbatim"
    compact = "".join(top["excerpt"].split())
    assert all("".join(f.split()) in compact for f in facts)


def test_skill_call_is_observed_as_real_and_not_bound_to_draft(product_run: ProductRun) -> None:
    r = product_run.results
    assert "real" in r["tool_modes"] and r["tool_calls_observed"] >= 1
    assert not any(sid.startswith("retriever:") for sid in r["draft_evidence"])


def test_credential_absent_from_results_traces_and_evidence(product_run: ProductRun) -> None:
    r = product_run.results
    for name in ("dumps", "traces"):
        if product_run.leaks(r[name]):
            pytest.fail(f"credential found in {name}")
    if EVIDENCE_OUT and product_run.leaks(Path(EVIDENCE_OUT).read_text(encoding="utf-8")):
        pytest.fail("credential found in evidence output")


def _write_evidence(run: ProductRun) -> None:
    if not EVIDENCE_OUT:
        return
    r = run.results
    external = [s for s in r.get("scout", {}).get("sources", []) if s.get("origin")]
    document = {
        "task": "P1-003",
        "kind": "product_research_worker_live_smoke",
        "simulated": False,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "skill": {"name": "nemo-retriever", "cli_version": SKILL_CLI_VERSION},
        "path": "WorkService.run -> TeamRunner(research) -> source_scout -> "
        "ObservedPort(tool, mode=real) -> NemoRetrieverTool -> retriever query --format evidence",
        "embedding": "hosted integrate.api.nvidia.com (CPU host default)",
        "question": QUESTION,
        "ingest": r.get("ingest"),
        "manifest_sources": r.get("manifest_sources"),
        "run_status": r.get("status"),
        "run_seconds": r.get("run_seconds"),
        "wall_seconds": r.get("wall_seconds"),
        "roles": r.get("roles"),
        "external_search": r.get("scout", {}).get("external_search"),
        "external_sources": [
            {
                k: s.get(k)
                for k in (
                    "source_id",
                    "source_revision",
                    "page",
                    "title",
                    "fidelity",
                    "content_hash",
                )
            }
            for s in external
        ],
        "tool_modes": r.get("tool_modes"),
        "draft_evidence_includes_external": any(
            sid.startswith("retriever:") for sid in r.get("draft_evidence", [])
        ),
        "not_run": [
            "model answer over Skill evidence (P1-002 ModelPort not wired)",
            "nemo_service path",
            "local embedding NIM",
        ],
    }
    text = json.dumps(document, ensure_ascii=False, indent=2)
    if run.leaks(text):
        pytest.fail("evidence would contain the credential; not written")
    Path(EVIDENCE_OUT).write_text(text + "\n", encoding="utf-8")
