"""Opt-in live smoke for the official NVIDIA nemo-retriever Agent Skill (P1-003A).

The skill (listed in the build.nvidia.com Skills catalog, source NVIDIA/skills) tells an
agent to use the pinned retriever CLI: "retriever ingest" into a local LanceDB index and
"retriever query --format evidence" for cited evidence. On a CPU-only host the CLI embeds
with NVIDIA's hosted endpoint, so this run needs the user-supplied NVIDIA_API_KEY.

Only fictional Korean PDFs from fixtures/retriever are ingested. The retrieved, cited
evidence is then the only context for one hosted Nemotron answer. This is not the product
Research worker/ToolPort/EvidenceBundle path (P1-003).

Run explicitly (skip is not success):
    RFA_RETRIEVER_LIVE=1 RFA_NVIDIA_ENV_FILE=/abs/.env.dev \
    RFA_RETRIEVER_BIN=/abs/tool-venv/bin/retriever \
    RFA_RETRIEVER_EVIDENCE_OUT=/abs/evidence.json \
    .venv/bin/python -m pytest -q tests/integration/test_retriever_live.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from rfa_mas.settings import Settings

LIVE_ENABLED = os.environ.get("RFA_RETRIEVER_LIVE") == "1"
ENV_FILE = os.environ.get("RFA_NVIDIA_ENV_FILE")
RETRIEVER_BIN = os.environ.get("RFA_RETRIEVER_BIN")
EVIDENCE_OUT = os.environ.get("RFA_RETRIEVER_EVIDENCE_OUT")

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures" / "retriever"
DOCUMENTS = ("synthetic_alpha_minutes.pdf", "synthetic_security_policy.pdf")
EXPECTED_ROWS = 5
EXPECTED_VERSION = "26.8.1"
TABLE = "rfa-synthetic"
CLI_TIMEOUT_SECONDS = 300
LLM_TIMEOUT_SECONDS = 170.0
LLM_MAX_ATTEMPTS = 2
RETRYABLE_STATUS = frozenset({429, 502, 503, 504})
ALLOWED_LLM_HOST = "integrate.api.nvidia.com"
SKILL_SOURCE = {
    "skill": "nemo-retriever",
    "catalog": "https://build.nvidia.com/skills",
    "skill_md": "https://github.com/NVIDIA/skills/blob/main/skills/nemo-retriever/SKILL.md",
    "pinned_package": f"nemo-retriever=={EXPECTED_VERSION}",
}

pytestmark = pytest.mark.skipif(
    not (LIVE_ENABLED and ENV_FILE and RETRIEVER_BIN),
    reason="opt-in live NeMo Retriever Skill smoke; skipped run is not evidence",
)

# name -> (question, expected top-1 citation, expected page, verbatim facts)
GOLDEN: dict[str, tuple[str, str, int, tuple[str, ...]]] = {
    "release": (
        "베타 출시일과 담당자는 누구인가?",
        "synthetic_alpha_minutes p.2",
        2,
        ("11월 14일", "김하늘"),
    ),
    "budget": (
        "3분기 GPU 예산은 얼마인가?",
        "synthetic_alpha_minutes p.3",
        3,
        ("1200만 원",),
    ),
    "retention": (
        "회의 녹음 파일은 며칠 동안 보관하나?",
        "synthetic_security_policy p.2",
        2,
        ("90일",),
    ),
}
RESEARCH_QUESTION = "베타 출시일과 담당자, 그리고 회의 녹음 파일 보관 기간을 알려줘."
RESEARCH_SOURCES = ("release", "retention")
RESEARCH_REQUIRED_CITATIONS = {"synthetic_alpha_minutes p.2", "synthetic_security_policy p.2"}
RESEARCH_SYSTEM = (
    "너는 Research 담당 비서다. 아래 검색 근거만 사용해 한국어로 답한다. "
    "근거에 없는 내용은 쓰지 않는다. 출력은 JSON 객체 하나이며 키는 "
    "answer(문자열)와 citations(사용한 근거의 대괄호 안 citation 문자열 배열)뿐이다."
    "\n\n검색 근거:\n"
)


class ResearchAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    answer: str
    citations: list[str]


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _compact(text: str) -> str:
    return "".join(text.split())


class SkillRun:
    """Runs the official skill workflow once; the credential never leaves the child env."""

    def __init__(self, settings: Settings, binary: Path, workdir: Path) -> None:
        if settings.nvidia_api_key is None or not settings.nvidia_model:
            pytest.fail("NVIDIA_API_KEY/NVIDIA_MODEL missing in the explicit env file")
        base = str(settings.nvidia_base_url or "").rstrip("/")
        parts = urlsplit(base)
        if parts.scheme != "https" or parts.hostname != ALLOWED_LLM_HOST:
            pytest.fail("NVIDIA_BASE_URL must be the https hosted endpoint for this smoke")
        self._secret = settings.nvidia_api_key
        self.llm_endpoint = f"{parts.scheme}://{parts.hostname}{parts.path}/chat/completions"
        self.llm_model = settings.nvidia_model
        self.binary = binary
        self.workdir = workdir
        self.db = workdir / "lancedb"
        base_env = {
            name: os.environ[name]
            for name in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")
            if name in os.environ
        }
        self._env_without_key = base_env
        self._env_with_key = {**base_env, "NVIDIA_API_KEY": self._secret.get_secret_value()}
        self.results: dict[str, Any] = {}

    def secret_in(self, text: str) -> bool:
        return self._secret.get_secret_value() in text

    def _redact(self, text: str) -> str:
        return text.replace(self._secret.get_secret_value(), "[REDACTED]")

    def cli(self, args: list[str], *, with_key: bool = True) -> dict[str, Any]:
        argv = [str(self.binary), *args]
        started = time.monotonic()
        try:
            completed = subprocess.run(
                argv,
                cwd=self.workdir,
                env=self._env_with_key if with_key else self._env_without_key,
                capture_output=True,
                text=True,
                timeout=CLI_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {
                "argv": ["retriever", *args],
                "exit_code": None,
                "error": "timeout",
                "seconds": round(time.monotonic() - started, 2),
                "stdout": "",
                "stderr": "",
            }
        return {
            "argv": ["retriever", *args],
            "exit_code": completed.returncode,
            "seconds": round(time.monotonic() - started, 2),
            "stdout": self._redact(completed.stdout),
            "stderr": self._redact(completed.stderr),
        }

    def run(self) -> None:
        version = self.cli(["--version"], with_key=False)
        self.results["version"] = version
        documents = [str(self.workdir / name) for name in DOCUMENTS]
        ingest = self.cli(
            [
                "ingest",
                *documents,
                "--method",
                "pdfium",
                "--lancedb-uri",
                str(self.db),
                "--table-name",
                TABLE,
            ]
        )
        match = re.search(r"(\d+) row", ingest["stdout"])
        ingest["rows"] = int(match.group(1)) if match else None
        self.results["ingest"] = ingest
        self.results["keyless_control"] = self.cli(
            self._query_args(GOLDEN["release"][0], top_k=1), with_key=False
        )
        queries: dict[str, Any] = {}
        for name, (question, *_rest) in GOLDEN.items():
            result = self.cli(self._query_args(question, top_k=3))
            result["evidence"] = _parse_evidence(result["stdout"])
            queries[name] = result
        self.results["queries"] = queries
        self.results["research"] = self._research(queries)

    def _query_args(self, question: str, *, top_k: int) -> list[str]:
        return [
            "query",
            question,
            "--lancedb-uri",
            str(self.db),
            "--table-name",
            TABLE,
            "--top-k",
            str(top_k),
            "--format",
            "evidence",
        ]

    def _research(self, queries: dict[str, Any]) -> dict[str, Any]:
        items: dict[str, str] = {}
        for name in RESEARCH_SOURCES:
            for item in (queries[name].get("evidence") or {}).get("evidence", [])[:2]:
                items.setdefault(item["citation"], item["text"])
        record: dict[str, Any] = {"retrieved_citations": sorted(items), "attempts": []}
        if not items:
            record["outcome"] = "no_evidence"
            return record
        context = "\n\n".join(f"[{citation}]\n{text}" for citation, text in items.items())
        body = {
            "model": self.llm_model,
            "stream": False,
            "temperature": 0,
            "max_tokens": 512,
            "response_format": {"type": "json_object"},
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [
                {
                    "role": "system",
                    "content": RESEARCH_SYSTEM + context,
                },
                {"role": "user", "content": RESEARCH_QUESTION},
            ],
        }
        raw = json.dumps(body, ensure_ascii=False, sort_keys=True).encode()
        record["request_sha256"] = hashlib.sha256(raw).hexdigest()
        with httpx.Client(
            timeout=LLM_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False
        ) as http:
            for _ in range(LLM_MAX_ATTEMPTS):
                started = time.monotonic()
                try:
                    response = http.post(
                        self.llm_endpoint,
                        content=raw,
                        headers={
                            "Authorization": "Bearer " + self._secret.get_secret_value(),
                            "Content-Type": "application/json",
                            "Accept": "application/json",
                        },
                    )
                except httpx.HTTPError as error:
                    record["attempts"].append(
                        {
                            "error": type(error).__name__,
                            "latency_seconds": round(time.monotonic() - started, 2),
                        }
                    )
                    continue
                record["attempts"].append(
                    {
                        "http_status": response.status_code,
                        "latency_seconds": round(time.monotonic() - started, 2),
                    }
                )
                if response.status_code == 200:
                    return _llm_success(record, response)
                if response.status_code not in RETRYABLE_STATUS:
                    break
        record["outcome"] = "failed"
        return record


def _parse_evidence(stdout: str) -> dict[str, Any] | None:
    start = stdout.find("{")
    if start < 0:
        return None
    try:
        return json.loads(stdout[start:])
    except ValueError:
        return None


def _llm_success(record: dict[str, Any], response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
        choice = data["choices"][0]
        content = choice["message"].get("content") or ""
    except (ValueError, KeyError, IndexError, TypeError):
        record["outcome"] = "unexpected_shape"
        return record
    record.update(
        {
            "outcome": "http_200",
            "model_echo": data.get("model"),
            "finish_reason": choice.get("finish_reason"),
            "usage": data.get("usage"),
            "content": content,
        }
    )
    return record


@pytest.fixture(scope="module")
def skill_run(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SkillRun]:
    env_file = Path(ENV_FILE or "")
    if env_file.is_symlink() or not env_file.is_file():
        pytest.fail("RFA_NVIDIA_ENV_FILE must be an explicit regular file")
    binary = Path(RETRIEVER_BIN or "")
    if not binary.is_file():
        pytest.fail("RFA_RETRIEVER_BIN must point to the pinned retriever executable")
    workdir = tmp_path_factory.mktemp("retriever-skill")
    for name in DOCUMENTS:
        shutil.copyfile(FIXTURES / name, workdir / name)
    run = SkillRun(Settings(_env_file=env_file), binary, workdir)
    started = time.monotonic()
    run.run()
    run.results["wall_seconds"] = round(time.monotonic() - started, 2)
    yield run
    _write_evidence(run)


def _cli_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: result.get(key)
        for key in ("argv", "exit_code", "error", "seconds", "rows", "checks")
        if key in result
    }


def _write_evidence(run: SkillRun) -> None:
    if not EVIDENCE_OUT:
        return
    results = run.results
    queries = {}
    for name, result in results.get("queries", {}).items():
        items = (result.get("evidence") or {}).get("evidence", [])
        queries[name] = {
            **_cli_summary(result),
            "question": GOLDEN[name][0],
            "top_citations": [item.get("citation") for item in items],
            "top1_score": items[0].get("score") if items else None,
            "top1_text_sha256": _sha256(items[0].get("text", "")) if items else None,
            "coverage": (result.get("evidence") or {}).get("coverage"),
        }
    research = dict(results.get("research", {}))
    content = research.pop("content", "")
    research["answer_excerpt"] = content[:240]
    research["answer_sha256"] = _sha256(content)
    control = results.get("keyless_control", {})
    document = {
        "task": "P1-003A",
        "kind": "direct_official_skill_smoke",
        "simulated": False,
        "product_research_worker_path": "not_run",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "skill_source": SKILL_SOURCE,
        "retriever_version": results.get("version", {}).get("stdout", "").strip()[:80],
        "embedding": "hosted integrate.api.nvidia.com (CPU host default)",
        "wall_seconds": results.get("wall_seconds"),
        "ingest": _cli_summary(results.get("ingest", {})),
        "keyless_control": {
            **_cli_summary(control),
            "mentions_key_requirement": "NVIDIA_API_KEY"
            in control.get("stdout", "") + control.get("stderr", ""),
        },
        "queries": queries,
        "research": research,
        "llm_model": run.llm_model,
        "stored": "commands without credential, counts, citations, hashes and synthetic excerpts",
    }
    text = json.dumps(document, ensure_ascii=False, indent=2)
    if run.secret_in(text):
        pytest.fail("evidence would contain the credential; not written")
    Path(EVIDENCE_OUT).write_text(text + "\n", encoding="utf-8")


def test_skill_cli_version_and_hosted_ingest(skill_run: SkillRun) -> None:
    version = skill_run.results["version"]
    ingest = skill_run.results["ingest"]
    checks = {
        "version": version["exit_code"] == 0 and EXPECTED_VERSION in version["stdout"],
        "ingest_exit_zero": ingest["exit_code"] == 0,
        "ingest_rows": ingest.get("rows") == EXPECTED_ROWS,
    }
    ingest["checks"] = checks
    assert all(checks.values()), checks


def test_keyless_query_fails_because_embedding_is_hosted(skill_run: SkillRun) -> None:
    control = skill_run.results["keyless_control"]
    output = control["stdout"] + control["stderr"]
    checks = {
        "nonzero_exit": control["exit_code"] not in (0, None),
        "mentions_key_requirement": "NVIDIA_API_KEY" in output,
    }
    control["checks"] = checks
    assert all(checks.values()), checks


@pytest.mark.parametrize("name", list(GOLDEN))
def test_golden_query_returns_cited_page_evidence(skill_run: SkillRun, name: str) -> None:
    _question, citation, page, facts = GOLDEN[name]
    result = skill_run.results["queries"][name]
    items = (result.get("evidence") or {}).get("evidence", [])
    top = items[0] if items else {}
    checks = {
        "exit_zero": result["exit_code"] == 0,
        "top1_citation": top.get("citation") == citation,
        "top1_page_locator": top.get("locator") == {"kind": "page", "value": page},
        "verbatim": top.get("fidelity") == "verbatim",
        "facts_in_text": all(_compact(fact) in _compact(top.get("text", "")) for fact in facts),
    }
    result["checks"] = checks
    assert all(checks.values()), checks


def test_research_answer_uses_only_retrieved_citations(skill_run: SkillRun) -> None:
    research = skill_run.results["research"]
    if research.get("outcome") != "http_200":
        pytest.fail(f"research: {research.get('outcome')} after {len(research['attempts'])}")
    assert research["finish_reason"] == "stop"
    assert research["model_echo"] == skill_run.llm_model
    try:
        parsed = ResearchAnswer.model_validate_json(research["content"])
    except ValidationError as error:
        research["checks"] = {"pydantic_valid": False}
        pytest.fail(f"research answer failed schema: {error.error_count()} errors")
    cited = {item.strip("[] ") for item in parsed.citations}
    answer = _compact(parsed.answer)
    checks = {
        "pydantic_valid": True,
        "facts": all(fact in answer for fact in ("11월14일", "김하늘", "90일")),
        "citations_within_retrieved": bool(cited) and cited <= set(research["retrieved_citations"]),
        "required_citations": cited >= RESEARCH_REQUIRED_CITATIONS,
    }
    research["checks"] = checks
    assert all(checks.values()), checks


def test_skill_provenance_is_recorded() -> None:
    document = (REPO_ROOT / "docs" / "evidence" / "nvidia-skill.md").read_text(encoding="utf-8")
    required = (
        SKILL_SOURCE["catalog"],
        SKILL_SOURCE["skill_md"],
        SKILL_SOURCE["pinned_package"],
        "not_run",
    )
    missing = [item for item in required if item not in document]
    assert not missing, missing
