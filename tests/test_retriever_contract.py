"""P1-003 contract: official nemo-retriever Skill CLI behind ToolPort and source_scout.

A fake `retriever` executable exercises the real subprocess boundary (argv, env, timeout,
kill, exit codes, stdout parsing). No network, NVIDIA credential or LanceDB is used; the
live official Skill run is P1-003A (tests/integration/test_retriever_live.py).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from rfa_mas.adapters.nemo_retriever import (
    ENV_ALLOWLIST,
    TOOL_NAME,
    NemoRetrieverTool,
    RetrieverConfig,
    RetrieverIndex,
    RetrieverToolRouter,
)
from rfa_mas.application.workers import ExternalSearchBinding
from rfa_mas.bootstrap import build_container
from rfa_mas.contracts import (
    Audience,
    DirectWorkRequest,
    DomainId,
    DraftTarget,
    KnowledgeWrite,
    ResultStatus,
    TeamExecutionRequest,
    ToolEffect,
    ToolRequest,
    WorkStatus,
    sha256_text,
)
from rfa_mas.errors import RfaError
from rfa_mas.settings import Settings

FIXTURES = Path(__file__).parents[1] / "fixtures" / "contracts" / "retriever"
KEY = "nvapi-SYNTHETIC-TEST-KEY-0000000000000000"
CANARY = "SYNTHETIC_PRIVATE_CANARY_RETRIEVER_U1"
GOAL = "양자화 방법론 자료 조사"
FAKE = r"""#!{python}
import hashlib, json, os, pathlib, sys, time
here = pathlib.Path(__file__).resolve().parent
mode = json.loads((here / "mode.json").read_text())
key = os.environ.get("NVIDIA_API_KEY")
record = {{"argv": sys.argv[1:], "env": sorted(os.environ), "pid": os.getpid(),
          "key_sha256": hashlib.sha256(key.encode()).hexdigest() if key else None}}
with open(here / "calls.jsonl", "a") as log:
    log.write(json.dumps(record, ensure_ascii=False) + "\n")
if mode.get("sleep"):
    time.sleep(mode["sleep"])
if mode.get("stderr"):
    sys.stderr.write(mode["stderr"])
if mode.get("stdout_file"):
    sys.stdout.write(pathlib.Path(mode["stdout_file"]).read_text())
if mode.get("stdout_bytes"):
    sys.stdout.write("x" * mode["stdout_bytes"])
sys.exit(mode.get("exit", 0))
"""


class Fake:
    def __init__(self, root: Path) -> None:
        root.mkdir()
        self.binary = root / "retriever"
        self.binary.write_text(FAKE.format(python=sys.executable))
        self.binary.chmod(0o755)
        self.root = root
        self.mode()

    def mode(self, **values) -> None:
        if "stdout" in values:
            values["stdout_file"] = str(FIXTURES / values.pop("stdout"))
        (self.root / "mode.json").write_text(json.dumps(values))

    def calls(self) -> list[dict]:
        path = self.root / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture
def fake(tmp_path) -> Fake:
    return Fake(tmp_path / "bin")


def index(**changes) -> RetrieverIndex:
    manifest = FIXTURES / "index_manifest.json"
    base = RetrieverIndex.from_manifest(manifest)
    return base.model_copy(update=changes) if changes else base


def hosted(fake: Fake, **values) -> RetrieverConfig:
    return RetrieverConfig(binary=fake.binary, embedding="hosted", api_key=SecretStr(KEY), **values)


def request(key="k1", **arguments) -> ToolRequest:
    effect = arguments.pop("effect", ToolEffect.READ)
    name = arguments.pop("tool_name", TOOL_NAME)
    args = {"query": GOAL, "index_id": "triv-demo-public-papers", "top_k": 3} | arguments
    return ToolRequest(
        request_id="req-1",
        trace_id="trace-1",
        run_id="run-1",
        agent_id="source-scout",
        domain_id=DomainId.TRIV3,
        idempotency_key=key,
        tool_name=name,
        effect=effect,
        arguments={k: v for k, v in args.items() if v is not None},
    )


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_manifest_fixture_and_config_rules():
    loaded = index()
    assert Path(loaded.lancedb_uri).is_absolute() and loaded.lancedb_uri.endswith("lancedb")
    assert loaded.audience == Audience.PUBLIC and loaded.synthetic
    with pytest.raises(ValidationError):
        RetrieverIndex.model_validate(loaded.model_dump() | {"lancedb_uri": "s3://bucket/db"})
    binary = Path("/opt/retriever/bin/retriever")
    with pytest.raises(ValidationError):  # hosted path needs the key
        RetrieverConfig(binary=binary, embedding="hosted")
    with pytest.raises(ValidationError):  # local path must stay on loopback
        RetrieverConfig(
            binary=binary, embedding="local", embed_invoke_url="https://embed.example.com/v1"
        )
    with pytest.raises(ValidationError):  # local path never receives the hosted key
        RetrieverConfig(
            binary=binary,
            embedding="local",
            embed_invoke_url="http://127.0.0.1:8000/v1",
            api_key=SecretStr(KEY),
        )
    with pytest.raises(ValidationError):
        RetrieverConfig(binary=Path("retriever"), embedding="hosted", api_key=SecretStr(KEY))
    RetrieverConfig(binary=binary, embedding="local", embed_invoke_url="http://localhost:8000/v1")


async def test_cli_evidence_maps_to_items_with_source_page_revision(fake, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("ALL_PROXY", "socks5://proxy.invalid:1080")
    fake.mode(stdout="query_evidence_ok.txt")
    tool = NemoRetrieverTool(hosted(fake), (index(),))
    result = await tool.execute(request())
    assert result.status == ResultStatus.SUCCEEDED and result.error is None
    assert result.adapter == "nemo-retriever-cli" and result.simulated is False
    out = result.output
    assert out["skill"] == {"name": "nemo-retriever", "cli_version": "26.8.1"}
    assert out["unmapped"] == 1 and out["insufficient"] is False
    assert [
        (e["source_id"], e["source_revision"], e["location"]["page"]) for e in out["evidence"]
    ] == [
        ("retriever:triv-demo/paper-pa", "pdf-sha256-5f1c0a7e9b3d2468", 1),
        ("retriever:triv-demo/paper-pb", "pdf-sha256-a93e61d04c7b8f25", 2),
    ]
    raw = json.loads((FIXTURES / "query_evidence_ok.txt").read_text().split("\n", 1)[1])
    for item, source in zip(out["evidence"], raw["evidence"], strict=False):
        assert item["audience"] == "public" and item["policy_version"] == "local-v1"
        assert item["content_hash"] == sha256_text(source["text"])
        assert (
            item["location"]["uri"]
            == f"nemo-retriever://triv-demo-public-papers/{source['source']}"
        )
    assert [c["citation"] for c in out["citations"]] == [
        "triv_demo_paper_pa p.1",
        "triv_demo_paper_pb p.2",
    ]
    assert CANARY not in result.model_dump_json()
    (call,) = fake.calls()
    assert call["argv"] == [
        "query",
        GOAL,
        "--lancedb-uri",
        index().lancedb_uri,
        "--table-name",
        "triv-demo-papers",
        "--top-k",
        "3",
        "--format",
        "evidence",
    ]
    assert call["key_sha256"] == hashlib.sha256(KEY.encode()).hexdigest()
    forwarded = set(call["env"]) - {"NVIDIA_API_KEY", "LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
    assert forwarded <= set(ENV_ALLOWLIST)
    assert not any("PROXY" in name.upper() for name in call["env"])
    assert KEY not in result.model_dump_json()


async def test_hosted_embedding_queries_only_public_synthetic_indexes(fake):
    fake.mode(stdout="query_evidence_ok.txt")
    team_only = index(index_id="triv-demo-team-notes", audience=Audience.BUSINESS_UNIT)
    real_public = index(index_id="triv-demo-real-public", synthetic=False)
    tool = NemoRetrieverTool(hosted(fake), (team_only, real_public))
    for index_id in ("triv-demo-team-notes", "triv-demo-real-public"):
        result = await tool.execute(request(key=index_id, index_id=index_id))
        assert result.status == ResultStatus.DENIED
        assert result.error.code == "egress_not_permitted"
    assert fake.calls() == []  # decided before the query could leave the PC
    local = NemoRetrieverTool(
        RetrieverConfig(
            binary=fake.binary, embedding="local", embed_invoke_url="http://127.0.0.1:8000/v1"
        ),
        (team_only,),
    )
    result = await local.execute(request(key="local", index_id="triv-demo-team-notes"))
    assert result.status == ResultStatus.SUCCEEDED
    assert result.output["evidence"][0]["audience"] == "business_unit"
    (call,) = fake.calls()
    assert call["key_sha256"] is None and "NVIDIA_API_KEY" not in call["env"]
    assert call["argv"][-2:] == ["--embed-invoke-url", "http://127.0.0.1:8000/v1"]


@pytest.mark.parametrize(
    ("changes", "status", "code"),
    [
        ({"effect": ToolEffect.WRITE}, ResultStatus.DENIED, "tool_not_allowed"),
        ({"tool_name": "shell"}, ResultStatus.DENIED, "tool_not_allowed"),
        ({"index_id": "unknown-index"}, ResultStatus.DENIED, "unknown_index"),
        ({"query": ""}, ResultStatus.FAILED, "invalid_tool_arguments"),
        ({"query": "x" * 501}, ResultStatus.FAILED, "invalid_tool_arguments"),
        ({"top_k": 6}, ResultStatus.FAILED, "invalid_tool_arguments"),
        ({"top_k": "3"}, ResultStatus.FAILED, "invalid_tool_arguments"),
        ({"lancedb_uri": "/tmp/other"}, ResultStatus.FAILED, "invalid_tool_arguments"),
    ],
)
async def test_requests_are_validated_before_any_subprocess(fake, changes, status, code):
    tool = NemoRetrieverTool(hosted(fake), (index(),))
    result = await tool.execute(request(**changes))
    assert (result.status, result.error.code) == (status, code)
    assert fake.calls() == []


async def test_timeout_kills_the_cli_process(fake):
    fake.mode(sleep=30, stdout="query_evidence_ok.txt")
    tool = NemoRetrieverTool(hosted(fake, timeout_seconds=0.5), (index(),))
    started = time.monotonic()
    result = await tool.execute(request())
    assert time.monotonic() - started < 10
    assert result.status == ResultStatus.TIMED_OUT and result.error.code == "retriever_timeout"
    assert result.error.retryable is True and result.output == {}
    assert not pid_alive(fake.calls()[0]["pid"])


async def test_cancellation_kills_the_cli_process(fake):
    fake.mode(sleep=30)
    tool = NemoRetrieverTool(hosted(fake, timeout_seconds=60), (index(),))
    task = asyncio.create_task(tool.execute(request()))
    for _ in range(200):
        if fake.calls():
            break
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not pid_alive(fake.calls()[0]["pid"])


@pytest.mark.parametrize(
    ("mode", "status", "code"),
    [
        ({"stdout": "query_evidence_malformed.txt"}, ResultStatus.FAILED, "malformed_evidence"),
        ({"stdout": "query_evidence_trailing.txt"}, ResultStatus.FAILED, "malformed_evidence"),
        ({"stdout_bytes": 2_000_000}, ResultStatus.FAILED, "output_too_large"),
        (
            {"exit": 1, "stderr": f"error key={KEY} query={GOAL}"},
            ResultStatus.FAILED,
            "retriever_failed",
        ),
        ({"exit": 0}, ResultStatus.FAILED, "malformed_evidence"),
    ],
)
async def test_bad_cli_outputs_are_never_success_and_leak_nothing(fake, mode, status, code):
    fake.mode(**mode)
    result = await NemoRetrieverTool(hosted(fake), (index(),)).execute(request())
    assert (result.status, result.error.code, result.output) == (status, code, {})
    dumped = result.model_dump_json()
    assert KEY not in dumped and GOAL not in dumped and "stderr" not in dumped


async def test_empty_evidence_is_explicitly_insufficient(fake):
    fake.mode(stdout="query_evidence_empty.txt")
    result = await NemoRetrieverTool(hosted(fake), (index(),)).execute(request())
    assert result.status == ResultStatus.SUCCEEDED
    assert result.output["evidence"] == [] and result.output["insufficient"] is True
    assert result.output["coverage"]["thin_spots"] == ["no matches — likely out of corpus"]


async def test_missing_binary_and_idempotent_replay(fake, tmp_path):
    missing = RetrieverConfig(
        binary=tmp_path / "absent", embedding="hosted", api_key=SecretStr(KEY)
    )
    result = await NemoRetrieverTool(missing, (index(),)).execute(request())
    assert (result.status, result.error.code) == (ResultStatus.FAILED, "retriever_unavailable")
    fake.mode(stdout="query_evidence_ok.txt")
    tool = NemoRetrieverTool(hosted(fake), (index(),))
    first = await tool.execute(request(key="same"))
    assert await tool.execute(request(key="same")) == first
    assert len(fake.calls()) == 1
    with pytest.raises(RfaError) as error:
        await tool.execute(request(key="same", query="다른 질문"))
    assert error.value.code == "idempotency_conflict"


async def test_router_sends_only_the_retriever_tool_to_the_cli(fake):
    class Base:
        adapter_name, simulated, seen = "base-tools", False, []

        async def execute(self, req):
            self.seen.append(req.tool_name)
            return "base"

    fake.mode(stdout="query_evidence_ok.txt")
    router = RetrieverToolRouter(Base(), NemoRetrieverTool(hosted(fake), (index(),)))
    assert router.adapter_name == "base-tools+nemo-retriever-cli" and router.simulated is False
    assert await router.execute(request(tool_name="metric_compare")) == "base"
    assert (await router.execute(request())).status == ResultStatus.SUCCEEDED
    assert Base.seen == ["metric_compare"] and len(fake.calls()) == 1


async def _research_container(tmp_path, fake, **tool_config):
    container = build_container(
        Settings(
            _env_file=None,
            database_url=f"sqlite:///{tmp_path / 'p1003.db'}",
            trace_dir=(tmp_path / "traces").resolve(),
        )
    )
    await container.startup()
    owner = await container.repository.local_principal()
    await container.knowledge.write(
        KnowledgeWrite.model_validate(
            {
                "domain_id": "triv3",
                "provenance": {"provider": "note", "namespace": "p1003", "external_id": "memo"},
                "provider_revision": "r1",
                "title": "양자화 방법론 메모",
                "content": "양자화 방법론 자료 조사 메모: 보정 데이터와 지연을 함께 본다.",
                "acl": {"audience": "public"},
                "synthetic": True,
            }
        ),
        owner,
    )
    runner = container.team_runner
    runner.tools.inner = RetrieverToolRouter(
        runner.tools.inner, NemoRetrieverTool(hosted(fake, **tool_config), (index(),))
    )
    runner.external_search = ExternalSearchBinding(
        TOOL_NAME, "triv-demo-public-papers", Audience.PUBLIC
    )
    return container, owner


def _research_request() -> DirectWorkRequest:
    return DirectWorkRequest(
        query=GOAL,
        domain_id=DomainId.TRIV3,
        target=DraftTarget(audience=Audience.OWNER),
        team=TeamExecutionRequest(
            goal=GOAL, outputs=("research_report",), requested_pattern="research"
        ),
    )


async def test_source_scout_calls_the_skill_tool_and_keeps_provenance(tmp_path, fake):
    fake.mode(stdout="query_evidence_ok.txt")
    container, owner = await _research_container(tmp_path, fake)
    try:
        result = await container.service.run(_research_request(), owner)
        assert result.status == WorkStatus.COMPLETED, result.errors
        team = await container.service.team_result(result.run_id, owner)
        scout = team.findings["source_scout"]
        assert scout["external_search"] == {
            "tool": TOOL_NAME,
            "index_id": "triv-demo-public-papers",
            "status": "succeeded",
            "simulated": False,
            "returned": 2,
            "used": 2,
            "unmapped": 1,
        }
        external = [s for s in scout["sources"] if s.get("origin") == "nemo-retriever-cli"]
        assert [
            (s["source_id"], s["source_revision"], s["page"], s["title"]) for s in external
        ] == [
            (
                "retriever:triv-demo/paper-pa",
                "pdf-sha256-5f1c0a7e9b3d2468",
                1,
                "triv_demo_paper_pa p.1",
            ),
            (
                "retriever:triv-demo/paper-pb",
                "pdf-sha256-a93e61d04c7b8f25",
                2,
                "triv_demo_paper_pb p.2",
            ),
        ]
        assert any(s.get("origin") is None for s in scout["sources"])  # local KB kept
        reviewed = {r["source_id"] for r in team.findings["evidence_reviewer"]["reviewed"]}
        assert {s["source_id"] for s in external} <= reviewed
        (call,) = fake.calls()
        assert call["argv"][1] == GOAL and call["argv"][-2:] == ["--format", "evidence"]
        scout_role = next(r for r in team.roles if r.role == "source_scout")
        assert scout_role.tool_calls == 2  # one KB search + one Skill tool call, both budgeted
        # External evidence is not bound to the DRAFT until P1-005 defines that policy.
        assert not any(e.source_id.startswith("retriever:") for e in result.draft.allowed_evidence)
        dumped = result.model_dump_json() + team.model_dump_json()
        assert CANARY not in dumped and KEY not in dumped
        ledger = await container.service.observations.ledger(result.run_id, owner)
        coverage = {c.boundary: c for c in ledger.coverage}
        assert coverage["tool"].calls >= 1
    finally:
        await container.shutdown()


async def test_skill_tool_failure_is_explicit_and_local_research_continues(tmp_path, fake):
    fake.mode(exit=1, stderr=f"boom {KEY}")
    container, owner = await _research_container(tmp_path, fake)
    try:
        result = await container.service.run(_research_request(), owner)
        assert result.status == WorkStatus.COMPLETED, result.errors
        team = await container.service.team_result(result.run_id, owner)
        scout = team.findings["source_scout"]
        assert scout["external_search"]["status"] == "failed"
        assert scout["external_search"]["error_code"] == "retriever_failed"
        assert scout["sources"] and all(s.get("origin") is None for s in scout["sources"])
        assert KEY not in result.model_dump_json() + team.model_dump_json()
    finally:
        await container.shutdown()


async def test_default_team_runner_has_no_external_search(tmp_path):
    container = build_container(
        Settings(
            _env_file=None,
            database_url=f"sqlite:///{tmp_path / 'd.db'}",
            trace_dir=(tmp_path / "traces").resolve(),
        )
    )
    await container.startup()
    try:
        assert container.team_runner.external_search is None
        assert "nemo-retriever" not in container.team_runner.tools.adapter_name
    finally:
        await container.shutdown()


async def test_bootstrap_nemo_cli_wires_the_skill_tool_and_keeps_kb_local(tmp_path, fake):
    """Proposal wiring (shared bootstrap/settings; coordinator applies)."""
    from rfa_mas.errors import ConfigurationError

    index_dir = tmp_path / "index"
    index_dir.mkdir()
    (index_dir / "index_manifest.json").write_text(
        (FIXTURES / "index_manifest.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    base = {
        "_env_file": None,
        "database_url": f"sqlite:///{tmp_path / 'b.db'}",
        "trace_dir": (tmp_path / "traces").resolve(),
        "retriever_backend": "nemo_cli",
        "retriever_index_dir": index_dir,
    }
    missing = Settings(**base)
    assert {"RETRIEVER_CLI_PATH", "NVIDIA_API_KEY"} <= set(missing.missing_for_selected_modes())
    with pytest.raises(ConfigurationError):
        build_container(missing)
    configured = Settings(**base, retriever_cli_path=fake.binary, nvidia_api_key=SecretStr(KEY))
    container = build_container(configured)
    await container.startup()
    try:
        runner = container.team_runner
        assert runner.external_search == ExternalSearchBinding(
            TOOL_NAME, "triv-demo-public-papers", Audience.PUBLIC
        )
        assert runner.tools.adapter_name == "local-analysis-tools+nemo-retriever-cli"
        assert runner.retrieval.adapter_name == container.retrieval.adapter_name
        assert "nemo" not in container.retrieval.adapter_name
    finally:
        await container.shutdown()
    broken = Settings(
        **(base | {"retriever_index_dir": tmp_path / "absent"}),
        retriever_cli_path=fake.binary,
        nvidia_api_key=SecretStr(KEY),
    )
    with pytest.raises(ConfigurationError) as error:
        build_container(broken)
    assert KEY not in str(error.value)



async def test_hosted_query_with_a_private_marker_never_leaves_the_pc(fake):
    """The hosted embedding is a cloud model: the P1-005 content markers apply to the query."""
    fake.mode(stdout="query_evidence_ok.txt")
    tool = NemoRetrieverTool(hosted(fake), (index(),))
    denied = await tool.execute(request(key="private", query=f"{GOAL} {CANARY}"))
    assert (denied.status, denied.error.code) == (ResultStatus.DENIED, "egress_not_permitted")
    assert fake.calls() == []
    allowed = await tool.execute(request(key="public"))
    assert allowed.status == ResultStatus.SUCCEEDED and len(fake.calls()) == 1
    local = NemoRetrieverTool(
        RetrieverConfig(
            binary=fake.binary, embedding="local", embed_invoke_url="http://127.0.0.1:8000/v1"
        ),
        (index(),),
    )
    kept_local = await local.execute(request(key="local-private", query=f"{GOAL} {CANARY}"))
    assert kept_local.status == ResultStatus.SUCCEEDED  # loopback embedding: stays on this PC


def test_nemo_cli_readiness_names_invalid_settings_without_values(tmp_path, fake):
    from rfa_mas.bootstrap import inspect_configuration

    index_dir = tmp_path / "idx"
    index_dir.mkdir()
    (index_dir / "index_manifest.json").write_text(
        (FIXTURES / "index_manifest.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    base = {"_env_file": None, "database_url": f"sqlite:///{tmp_path / 'r.db'}",
            "retriever_backend": "nemo_cli", "nvidia_api_key": SecretStr(KEY)}
    ok = inspect_configuration(Settings(**base, retriever_index_dir=index_dir,
                                        retriever_cli_path=fake.binary))
    assert ok.ready and "retriever:nemo_cli" not in ok.reserved
    for changes, name in (
        ({"retriever_index_dir": index_dir, "retriever_cli_path": Path("relative/retriever")},
         "RETRIEVER_CLI_PATH"),
        ({"retriever_index_dir": tmp_path / "absent", "retriever_cli_path": fake.binary},
         "RETRIEVER_INDEX_DIR"),
    ):
        report = inspect_configuration(Settings(**base, **changes))
        assert not report.ready and report.invalid == (name,)
        assert KEY not in repr(report)
