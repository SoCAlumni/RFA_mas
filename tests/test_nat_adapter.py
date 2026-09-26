"""Synthetic native/NAT consumer boundaries; conditional collection is not live proof."""

from __future__ import annotations

import importlib.abc
import importlib.metadata
import socket
import sys

import pytest
from langsmith import utils as langsmith_utils

from rfa_mas.adapters import nat_eval
from rfa_mas.bootstrap import build_container
from rfa_mas.contracts import (
    Audience,
    DomainId,
    DraftTarget,
    PublicationStatus,
    ResumeRequest,
    ReviewDecision,
    ReviewStatus,
    SimulationScenario,
    WorkRequest,
    WorkStatus,
)
from rfa_mas.settings import Settings

CANARY = "PRIVATE_NAT_CANARY_028"
try:
    importlib.metadata.version("nvidia-nat-langchain")
    NAT_INSTALLED = True
except importlib.metadata.PackageNotFoundError:
    NAT_INSTALLED = False


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    connections = []
    original = socket.socket.connect

    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            connections.append("blocked")
            raise AssertionError("NAT tests prohibit network")
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    yield connections
    assert connections == []


def work(**updates):
    return WorkRequest(
        **{
            "query": f"TRIV3 공개 트랙 {CANARY}",
            "domain_id": DomainId.TRIV3,
            "target": DraftTarget(audience=Audience.PUBLIC),
            **updates,
        }
    )


def trace_text(container):
    return "".join(
        path.read_text()
        for path in (container.settings.trace_dir / "rfa-observations-v1").glob("events-*.jsonl")
    )


def counts(ledger):
    return {c.boundary: (c.state, c.calls) for c in ledger.coverage}


class DenyNatImport(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "nat" or fullname.startswith("nat."):
            raise ModuleNotFoundError("synthetic missing optional dependency")
        return None


def test_missing_extra_explicit_not_fallback(monkeypatch):
    def missing(_name):
        raise importlib.metadata.PackageNotFoundError("synthetic missing")

    monkeypatch.setattr(importlib.metadata, "version", missing)
    with pytest.raises(nat_eval.NatEvaluationError, match="^NAT_UNAVAILABLE$"):
        nat_eval._require_nat()


async def test_default_native_run_without_nat_imports(container, principal, monkeypatch):
    monkeypatch.setattr(sys, "meta_path", [DenyNatImport(), *sys.meta_path])
    result = await container.service.run(work(), principal)
    assert result.status == WorkStatus.COMPLETED
    assert result.publication_status == PublicationStatus.NOT_REQUESTED
    assert "nat.cli.entrypoint" not in sys.modules


if NAT_INSTALLED:

    class TestInstalledNatAdapter:
        async def test_native_equivalence_one_call_durable_ledger(
            self, container, principal, monkeypatch, tmp_path
        ):
            native = build_container(
                Settings(
                    _env_file=None,
                    database_url=f"sqlite:///{tmp_path / 'native.db'}",
                    trace_dir=tmp_path / "native-traces",
                )
            )
            await native.startup()
            captured = []
            actual = container.service.run

            async def spy(request, caller):
                result = await actual(request, caller)
                captured.append(result)
                return result

            monkeypatch.setattr(container.service, "run", spy)
            native_request, nat_request = work(), work()
            try:
                expected = await native.service.run(native_request, principal)
                report = await nat_eval.evaluate_synthetic(container, nat_request, principal)
                assert report.status == "passed", report
                assert len(captured) == 1
                actual_result = captured[0]
                assert expected.status == actual_result.status == WorkStatus.COMPLETED
                assert expected.draft.content == actual_result.draft.content
                assert expected.draft.content_hash == actual_result.draft.content_hash
                assert expected.draft.target == actual_result.draft.target
                assert expected.review.decision == actual_result.review.decision
                assert nat_eval._binding_valid(expected) and nat_eval._binding_valid(actual_result)
                native_ledger = await native.service.observations.ledger(expected.run_id, principal)
                nat_ledger = await container.service.observations.ledger(
                    actual_result.run_id, principal
                )
                assert counts(native_ledger) == counts(nat_ledger)
                assert counts(nat_ledger)["retrieval"][1] > 0
                assert counts(nat_ledger)["model"][1] > 0
                assert native_ledger.execution.run_id != nat_ledger.execution.run_id
                assert report.cases[0].ledger == nat_ledger
                assert [r.sequence for r in nat_ledger.observations] == list(
                    range(1, len(nat_ledger.observations) + 1)
                )
                assert all(
                    r.event.execution.run_id == nat_ledger.execution.run_id
                    for r in nat_ledger.observations
                )
                safe = report.model_dump_json() + trace_text(container)
                for secret in (CANARY, principal.user_id, nat_request.run_id, nat_request.query):
                    assert secret not in safe
                assert actual_result.draft.content not in safe
                assert report.input_tokens is report.output_tokens is None
                for record in nat_ledger.observations:
                    assert record.event.input_tokens is record.event.output_tokens is None
                    assert record.event.approval_id is None
                    assert record.event.policy_decision_id is None
                    assert record.event.publication_id is None
                    if record.transport:
                        assert record.event.duration_ms >= 0
                assert counts(nat_ledger)["tool"] == ("uncollected", None)
                assert counts(nat_ledger)["publish"] == ("uncollected", None)
                assert counts(nat_ledger)["internal_nodes"] == ("uncollected", None)
            finally:
                await native.shutdown()

        async def test_pending_approval_checkpoint_not_bypassed(
            self, container, principal, monkeypatch
        ):
            submitted = []

            async def pending(draft, **_kwargs):
                submitted.append(draft)
                return ReviewDecision(
                    **{
                        key: getattr(draft, key)
                        for key in ("request_id", "trace_id", "run_id", "agent_id", "domain_id")
                    },
                    draft_id=draft.draft_id,
                    draft_version=draft.version,
                    content_hash=draft.content_hash,
                    target=draft.target,
                    decision=ReviewStatus.PENDING,
                    publication_status=PublicationStatus.NOT_REQUESTED,
                    safe_reason=CANARY,
                    simulated=True,
                    adapter="mock-response",
                )

            monkeypatch.setattr(container.response, "submit_draft", pending)
            request = work()
            report = await nat_eval.evaluate_synthetic(
                container, request, principal, expected_status=WorkStatus.WAITING_APPROVAL
            )
            assert report.status == "passed"
            assert len(submitted) == 1
            stored = await container.repository.get_owned_run(request.run_id, principal)
            assert stored.result.status == WorkStatus.WAITING_APPROVAL
            config = container.service._config(stored.thread_id)
            snapshot = await container.service._graph.aget_state(config)
            assert snapshot.next == ("await_review",)
            # Native service remains the only resume entrypoint/approval authority.
            assert ResumeRequest(event_id="synthetic-wakeup").action == "refresh_review"
            assert report.cases[0].ledger.execution.session_id is not None
            assert CANARY not in report.model_dump_json() + trace_text(container)

        @pytest.mark.parametrize(
            "field,value",
            [
                ("model_provider", "nvidia"),
                ("response_backend", "http"),
                ("runtime_backend", "http"),
                ("allow_external_writes", True),
            ],
        )
        async def test_real_modes_fail_before_service(self, container, principal, field, value):
            container.settings = container.settings.model_copy(update={field: value})
            with pytest.raises(nat_eval.NatEvaluationError, match="^NAT_SYNTHETIC_ONLY$"):
                await nat_eval.evaluate_synthetic(container, work(), principal)

        async def test_lifecycle_and_anonymous_not_auth_by_alias(self, container, principal):
            container.ready = False
            with pytest.raises(nat_eval.NatEvaluationError, match="NAT_LIFECYCLE_REQUIRED"):
                await nat_eval.evaluate_synthetic(container, work(), principal)
            container.ready = True
            anonymous = principal.model_copy(update={"authenticated": False})
            with pytest.raises(nat_eval.NatEvaluationError, match="NAT_INPUT_REJECTED"):
                await nat_eval.evaluate_synthetic(container, work(), anonymous)

        async def test_completed_run_not_fresh_nat_proof(self, container, principal, monkeypatch):
            request = work()
            await container.service.run(request, principal)
            original = await container.service.observations.ledger(request.run_id, principal)

            async def must_not_run(*_args):
                raise AssertionError("completed run must not be replayed")

            monkeypatch.setattr(container.service, "run", must_not_run)
            with pytest.raises(nat_eval.NatEvaluationError, match="NAT_EXISTING_RUN_REJECTED"):
                await nat_eval.evaluate_synthetic(container, request, principal)
            assert (
                await container.service.observations.ledger(request.run_id, principal) == original
            )

        async def test_failure_safe_and_not_retried(
            self, container, principal, monkeypatch, caplog
        ):
            calls = []

            async def fail(*_args):
                calls.append(1)
                raise RuntimeError(CANARY)

            monkeypatch.setattr(container.service, "run", fail)
            report = await nat_eval.evaluate_synthetic(container, work(), principal)
            assert report.status == "error"
            assert calls == [1]
            assert CANARY not in caplog.text + report.model_dump_json()
            assert nat_eval._ACTIVE.get() is None

        async def test_intent_incomplete_and_sink_not_promoted(self, container, principal):
            request = work()
            result = await container.service.run(request, principal)
            observer = container.service.observations
            async with observer.scope(request.run_id, principal):
                # Actual local sink receipt first, independent of production tool coverage.
                sink = ["synthetic receipt"]
                await observer.record_test_sink(received=bool(sink))
                await observer.record("retrieval", "started", mode="mock")
            ledger = await observer.ledger(request.run_id, principal)
            output = nat_eval.SafeCaseOutput(
                status=result.status, binding_valid=nat_eval._binding_valid(result), ledger=ledger
            )
            assert counts(ledger)["retrieval"] == ("incomplete", None)
            assert counts(ledger)["test_sink"] == ("collected", 1)
            assert counts(ledger)["tool"] == ("uncollected", None)
            assert not nat_eval._contract_passed(output, WorkStatus.COMPLETED)

        async def test_outer_ambient_trace_guard_positive_control(
            self, container, principal, monkeypatch
        ):
            from langchain_core.callbacks import BaseCallbackHandler
            from langchain_core.callbacks.manager import CallbackManager
            from langchain_core.tracers import langchain as tracer_module

            langsmith_utils.get_env_var.cache_clear()
            monkeypatch.setenv("LANGSMITH_TRACING", "true")
            monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
            # Actual enablement positive control, no mock of tracing_is_enabled.
            hits = []

            class TracerSpy(BaseCallbackHandler):
                def __init__(self, **_kwargs):
                    hits.append("tracer constructed")

            monkeypatch.setattr(tracer_module, "LangChainTracer", TracerSpy)
            try:
                positive = CallbackManager.configure()
                assert any(isinstance(h, TracerSpy) for h in positive.handlers)
                assert hits == ["tracer constructed"]
                hits.clear()
                report = await nat_eval.evaluate_synthetic(container, work(), principal)
                assert report.status == "passed"
                assert hits == []
                assert "nat.cli.entrypoint" not in sys.modules
            finally:
                langsmith_utils.get_env_var.cache_clear()

        async def test_config_and_dataset_reject_egress_or_payload(self, monkeypatch, tmp_path):
            config = tmp_path / "bad.yml"
            config.write_text("workflow: {env: PRIVATE_NAT_CANARY_028}\n")
            monkeypatch.setattr(nat_eval, "CONFIG", config)
            with pytest.raises(nat_eval.NatEvaluationError, match="NAT_CONFIGURATION_REJECTED"):
                nat_eval._load_config()

        @pytest.mark.parametrize("mode", ["missing", "interrupt", "empty", "error", "nan"])
        async def test_incomplete_results_are_never_passed(
            self, container, principal, monkeypatch, mode
        ):
            from nat.plugins.eval.runtime.evaluate import EvaluationRun

            original = EvaluationRun.run_and_evaluate

            async def damage(runner, *args, **kwargs):
                output = await original(runner, *args, **kwargs)
                if mode == "missing":
                    output.evaluation_results.clear()
                elif mode == "interrupt":
                    output.workflow_interrupted = True
                elif mode == "empty":
                    output.eval_input.eval_input_items[0].output_obj = None
                elif mode == "error":
                    output.evaluation_results[0][1].eval_output_items[0].error = CANARY
                else:
                    output.evaluation_results[0][1].eval_output_items[0].score = float("nan")
                return output

            monkeypatch.setattr(EvaluationRun, "run_and_evaluate", damage)
            report = await nat_eval.evaluate_synthetic(container, work(), principal)
            assert report.status == "error"
            assert CANARY not in report.model_dump_json()

        async def test_timeout_not_a_success(self, container, principal):
            report = await nat_eval.evaluate_synthetic(
                container, work(simulation_scenario=SimulationScenario.TIMEOUT), principal
            )
            assert report.status == "failed"
            assert report.cases[0].status in {WorkStatus.FAILED, WorkStatus.OUTCOME_UNKNOWN}
            assert any(r.transport == "raised" for r in report.cases[0].ledger.observations)

        async def test_duplicate_or_wrong_case_cannot_reinvoke(self, container, principal):
            harness = nat_eval._Harness(container, work(), principal, WorkStatus.COMPLETED)
            with pytest.raises(nat_eval.NatEvaluationError, match="NAT_CASE_REJECTED"):
                await harness.invoke("claim-admin-identity")
            await harness.invoke(nat_eval.CASE_ID)
            with pytest.raises(nat_eval.NatEvaluationError, match="NAT_CASE_REJECTED"):
                await harness.invoke(nat_eval.CASE_ID)
            assert harness.calls == 1

        def test_wrapper_without_trusted_context_rejected(self):
            with pytest.raises(nat_eval.NatEvaluationError, match="NAT_TRUSTED_CONTEXT_REQUIRED"):
                nat_eval.build_eval_graph({"user_id": "owner", "trace_id": CANARY})
