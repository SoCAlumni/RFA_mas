"""Installed EvaluationRun smoke, never a wrapper-only or live Judge success."""

import importlib.metadata
import socket
import sys

import pytest
import pytest_asyncio

from rfa_mas.adapters import nat_eval
from rfa_mas.bootstrap import build_container
from rfa_mas.contracts import Audience, DomainId, DraftTarget, WorkRequest
from rfa_mas.settings import Settings

try:
    importlib.metadata.version("nvidia-nat-langchain")
    NAT_INSTALLED = True
except importlib.metadata.PackageNotFoundError:
    NAT_INSTALLED = False


@pytest_asyncio.fixture
async def container(tmp_path):
    # NAT synthetic evaluation accepts only the explicit mock retriever fixture;
    # the product default is the local lexical reader (P1-001A).
    instance = build_container(
        Settings(
            _env_file=None,
            database_url=f"sqlite:///{tmp_path / 'smoke.db'}",
            trace_dir=tmp_path / "traces",
            retriever_backend="mock",
        )
    )
    await instance.startup()
    try:
        yield instance
    finally:
        await instance.shutdown()


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    attempts = []
    original = socket.socket.connect

    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            attempts.append("outbound")
            raise AssertionError("NAT smoke forbids network")
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    yield
    assert not attempts


def request():
    return WorkRequest(
        query="TRIV3 공개 트랙 PRIVATE_NAT_SMOKE_CANARY",
        domain_id=DomainId.TRIV3,
        target=DraftTarget(audience=Audience.PUBLIC),
    )


if NAT_INSTALLED:

    async def test_real_evaluation_run_registered_evaluator_no_raw_files(
        container, principal, monkeypatch, tmp_path, caplog
    ):
        from nat.plugins.eval.runtime.evaluate import EvaluationRun

        calls, observed = [], []
        original = EvaluationRun.run_and_evaluate

        async def spy(runner, *args, **kwargs):
            calls.append(1)
            assert runner.callback_manager._callbacks == []
            assert runner.config.write_output is False
            assert runner.config.endpoint is None
            config = runner.config.config_file
            assert config.workflow.env is None
            assert config.eval.general.output is config.eval.general.profiler is None
            assert config.general.telemetry.logging == config.general.telemetry.tracing == {}
            result = await original(runner, *args, **kwargs)
            observed.append(result)
            return result

        monkeypatch.setattr(EvaluationRun, "run_and_evaluate", spy)
        # CWD independence and no NAT default output directory creation.
        monkeypatch.chdir(tmp_path)
        work = request()
        report = await nat_eval.evaluate_synthetic(container, work, principal)
        assert report.status == "passed"
        assert calls == [1]
        assert report.nat_versions == {name: "1.8.0" for name in nat_eval.PACKAGES}
        assert report.semantic_quality == "not_run"
        assert report.duration_ms > 0
        assert report.input_tokens is report.output_tokens is None
        result = observed[0]
        assert not result.workflow_interrupted
        assert len(result.eval_input.eval_input_items) == 1
        assert len(result.evaluation_results) == 1
        assert result.evaluation_results[0][0] == nat_eval.EVALUATOR
        assert result.evaluation_results[0][1].eval_output_items[0].score == 1
        assert result.workflow_output_file is None and result.evaluator_output_files == []
        assert result.config_original_file is result.config_effective_file is None
        assert not (tmp_path / ".tmp").exists()
        assert "nat.cli.entrypoint" not in sys.modules
        safe = report.model_dump_json() + caplog.text
        assert "PRIVATE_NAT_SMOKE_CANARY" not in safe and work.run_id not in safe
        files = list(container.settings.trace_dir.rglob("events-*.jsonl"))
        assert files
        for path in files:
            text = path.read_text()
            assert "PRIVATE_NAT_SMOKE_CANARY" not in text
            assert work.query not in text
            assert "observation_id" in text

    async def test_actual_swallowed_evaluator_exception_is_error(
        container, principal, monkeypatch, caplog
    ):
        def broken(*_args):
            raise RuntimeError("PRIVATE_EVALUATOR_CANARY")

        monkeypatch.setattr(nat_eval, "_contract_passed", broken)
        report = await nat_eval.evaluate_synthetic(container, request(), principal)
        assert report.status == "error"
        assert "PRIVATE_EVALUATOR_CANARY" not in caplog.text + report.model_dump_json()
        assert "NAT_EVALUATION_INCOMPLETE" in caplog.text

else:

    def test_without_extra_is_unavailable_not_installed_smoke():
        with pytest.raises(nat_eval.NatEvaluationError, match="NAT_UNAVAILABLE"):
            nat_eval._require_nat()
