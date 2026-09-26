"""NAT1.8 evaluation of one trusted, synthetic WorkService invocation.

Explicit Python API only: await evaluate_synthetic(ready_container, request, principal).
No CLI/enable_nat setting, lifecycle replacement, external exporter or real-provider
claim. The caller owns startup/shutdown and must supply synthetic material. Request
and identity stay in a task-local harness, never NAT messages or config. This is
trusted in-process instrumentation, not an API for granting identity/permissions.

The installed v1.8 APIs are experimental; unknown versions fail closed. NVIDIA's
langgraph_wrapper imports this file under a generated module name, so the graph
factory explicitly uses the canonical module's ContextVar (not a global service).
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Any, Literal, TypedDict

import yaml
from langsmith import tracing_context
from pydantic import BaseModel, ConfigDict, Field

from rfa_mas.contracts import (
    ObservationLedger,
    PublicationStatus,
    RunResult,
    TrustedPrincipal,
    WorkRequest,
    WorkStatus,
    sha256_text,
)
from rfa_mas.errors import ResourceNotFoundError

ROOT = Path(__file__).resolve().parents[3]
CONFIG = ROOT / "configs/nat/rfa_eval.yml"
DATASET = ROOT / "configs/nat/rfa_eval_cases.json"
CASE_ID = "representative-v1"
EVALUATOR = "rfa_contract"
PACKAGES = ("nvidia-nat-core", "nvidia-nat-langchain", "nvidia-nat-eval")


class NatEvaluationError(RuntimeError):
    """Fixed error codes only; upstream/provider exception text is never exposed."""


class SafeCaseOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    case_id: Literal["representative-v1"] = CASE_ID
    status: WorkStatus
    binding_valid: bool
    ledger: ObservationLedger


class NatEvaluationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["passed", "failed", "error"]
    evaluator: Literal["deterministic-contract"] = "deterministic-contract"
    semantic_quality: Literal["not_run"] = "not_run"
    provider_mode: Literal["mock/local"] = "mock/local"
    nat_versions: dict[str, str]
    cases: tuple[SafeCaseOutput, ...]
    duration_ms: float = Field(ge=0)
    input_tokens: None = None
    output_tokens: None = None
    reason: Literal["observed_contract", "contract_failed", "evaluation_incomplete"]


@dataclass(repr=False)
class _Harness:
    container: Any
    request: WorkRequest
    principal: TrustedPrincipal
    expected_status: WorkStatus
    calls: int = 0
    evaluator_calls: int = 0
    safe_output: SafeCaseOutput | None = None
    # Never serialize this protected native result or use its aliases as auth.
    result: RunResult | None = field(default=None, repr=False)

    async def invoke(self, case_id: str) -> SafeCaseOutput:
        if case_id != CASE_ID or self.calls:
            raise NatEvaluationError("NAT_CASE_REJECTED")
        self.calls += 1  # reserve before await; retries must not repeat writes
        try:
            self.result = await self.container.service.run(self.request, self.principal)
            ledger = await self.container.service.observations.ledger(
                self.result.run_id, self.principal
            )
            # Reparse the durable allowlist, not provider dictionaries/trace files.
            ledger = ObservationLedger.model_validate(ledger.model_dump(mode="json"))
            self.safe_output = SafeCaseOutput(
                status=self.result.status,
                binding_valid=_binding_valid(self.result),
                ledger=ledger,
            )
            return self.safe_output
        except asyncio.CancelledError:
            raise
        except Exception:
            # Prevent NAT's exception logger from seeing raw prompt/provider errors.
            raise NatEvaluationError("NAT_WORKFLOW_FAILED") from None


_ACTIVE: ContextVar[_Harness | None] = ContextVar("rfa_nat_evaluation", default=None)
_REGISTERED = False


def _binding_valid(result: RunResult) -> bool:
    draft, review = result.draft, result.review
    if draft is None or review is None:
        return False
    return (
        draft.run_id == result.run_id == review.run_id
        and draft.draft_id == review.draft_id
        and draft.version == review.draft_version
        and draft.content_hash == review.content_hash == sha256_text(draft.content)
        and draft.target == review.target
        and result.publication_status == PublicationStatus.NOT_REQUESTED
    )


def _contract_passed(output: SafeCaseOutput, expected: WorkStatus) -> bool:
    ledger = output.ledger
    records = ledger.observations
    coverage = {item.boundary: item for item in ledger.coverage}
    if not records or output.status != expected or not output.binding_valid:
        return False
    if [r.sequence for r in records] != list(range(1, len(records) + 1)):
        return False
    if len({r.observation_id for r in records}) != len(records):
        return False
    if any(r.event.execution.run_id != ledger.execution.run_id for r in records):
        return False
    if any(c.state == "incomplete" for c in ledger.coverage):
        return False
    for name in ("request", "retrieval", "model", "policy", "runtime", "approval"):
        item = coverage.get(name)
        if item is None or item.state != "collected" or not item.calls:
            return False
    if coverage["request"].calls != 1:
        return False
    # Never promote test sink receipts or missing instrumentation to real writes.
    for name in ("tool", "publish", "internal_nodes"):
        item = coverage.get(name)
        if item is None or item.state != "uncollected" or item.calls is not None:
            return False
    return all(
        r.event.input_tokens is None
        and r.event.output_tokens is None
        and r.event.policy_decision_id is None
        and r.event.approval_id is None
        and r.event.publication_id is None
        and (r.transport is None or r.event.duration_ms is not None)
        for r in records
    )


class _Messages(TypedDict):
    messages: list[Any]


def build_eval_graph(_config):
    """Official wrapper factory; no checkpointer/auth config is taken from NAT."""
    from langchain_core.messages import AIMessage, convert_to_messages
    from langgraph.graph import END, START, StateGraph

    from rfa_mas.adapters import nat_eval as canonical

    harness = canonical._ACTIVE.get()
    if harness is None:
        raise NatEvaluationError("NAT_TRUSTED_CONTEXT_REQUIRED")

    async def execute(state):
        try:
            messages = convert_to_messages(state["messages"])
            if len(messages) != 1 or messages[0].content != CASE_ID:
                raise NatEvaluationError("NAT_CASE_REJECTED")
            output = await harness.invoke(CASE_ID)
            return {"messages": [AIMessage(content=output.model_dump_json())]}
        except asyncio.CancelledError:
            raise
        except Exception:
            raise NatEvaluationError("NAT_WORKFLOW_FAILED") from None

    graph = StateGraph(_Messages)
    graph.add_node("rfa_service", execute)
    graph.add_edge(START, "rfa_service")
    graph.add_edge("rfa_service", END)
    return graph.compile()


def _require_nat() -> dict[str, str]:
    try:
        versions = {name: importlib.metadata.version(name) for name in PACKAGES}
        if set(versions.values()) != {"1.8.0"}:
            raise NatEvaluationError("NAT_VERSION_UNSUPPORTED")
        return versions
    except importlib.metadata.PackageNotFoundError:
        raise NatEvaluationError("NAT_UNAVAILABLE") from None


def _register_evaluator() -> None:
    global _REGISTERED
    if _REGISTERED:
        return
    from nat.builder.evaluator import EvaluatorInfo
    from nat.cli.register_workflow import register_evaluator
    from nat.data_models.evaluator import EvaluatorBaseConfig
    from nat.plugins.eval.data_models.evaluator_io import EvalOutput, EvalOutputItem

    class ContractCheckConfig(EvaluatorBaseConfig, name="rfa_contract_check"):
        model_config = ConfigDict(extra="forbid")

    @register_evaluator(config_type=ContractCheckConfig)
    async def register(config, _builder):
        async def evaluate(eval_input):
            try:
                harness = _ACTIVE.get()
                if harness is None:
                    raise NatEvaluationError("NAT_TRUSTED_CONTEXT_REQUIRED")
                harness.evaluator_calls += 1
                items = eval_input.eval_input_items
                if len(items) != 1 or items[0].id != CASE_ID:
                    raise NatEvaluationError("NAT_EVALUATION_INCOMPLETE")
                output = SafeCaseOutput.model_validate_json(items[0].output_obj)
                passed = (
                    harness.calls == 1
                    and output == harness.safe_output
                    and _contract_passed(output, harness.expected_status)
                )
                return EvalOutput(
                    average_score=float(passed),
                    eval_output_items=[
                        EvalOutputItem(
                            id=CASE_ID,
                            score=float(passed),
                            reasoning={"rule": "observed-contract-v1", "passed": passed},
                        )
                    ],
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # v1.8 logs evaluator exceptions and omits their result; normalize
                # before that logger and check the missing result after the run.
                raise NatEvaluationError("NAT_EVALUATION_INCOMPLETE") from None

        yield EvaluatorInfo(
            config=config, evaluate_fn=evaluate, description="Local observed contract, not Judge"
        )

    _REGISTERED = True


def _load_config():
    # Validate the complete owned configuration before NAT plugin/env expansion.
    # No arbitrary config path, override, callbacks or user-supplied dataset API.
    raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    expected = {
        "general": {"telemetry": {"logging": {}, "tracing": {}}},
        "workflow": {
            "_type": "langgraph_wrapper",
            "graph": "src/rfa_mas/adapters/nat_eval.py:build_eval_graph",
            "env": None,
        },
        "eval": {
            "general": {
                "max_concurrency": 1,
                "per_input_user_id": False,
                "validate_llm_endpoints": False,
                "output": None,
                "profiler": None,
                "dataset": {
                    "_type": "json",
                    "file_path": "configs/nat/rfa_eval_cases.json",
                    "id_key": "id",
                    "structure": {"question_key": "question", "answer_key": "answer"},
                },
            },
            "evaluators": {EVALUATOR: {"_type": "rfa_contract_check"}},
        },
    }
    dataset = json.loads(DATASET.read_text(encoding="utf-8"))
    if raw != expected or dataset != [
        {"id": CASE_ID, "question": CASE_ID, "answer": "observed-contract-v1"}
    ]:
        raise NatEvaluationError("NAT_CONFIGURATION_REJECTED")
    from nat.runtime.loader import load_config

    config = load_config(CONFIG)  # public discovery registers nested discriminator types
    config.workflow.graph = f"{Path(__file__)}:build_eval_graph"
    config.eval.general.dataset.file_path = str(DATASET)
    return config


def _summarize(output, harness: _Harness, versions, elapsed) -> NatEvaluationReport:
    """No upstream UsageStats defaults, raw trajectories, logs or callback artifacts."""
    status, reason = "error", "evaluation_incomplete"
    cases = (harness.safe_output,) if harness.safe_output is not None else ()
    try:
        items = output.eval_input.eval_input_items
        evaluations = output.evaluation_results
        if (
            output.workflow_interrupted
            or len(items) != 1
            or items[0].id != CASE_ID
            or not items[0].output_obj
            or harness.calls != 1
            or harness.evaluator_calls != 1
            or len(evaluations) != 1
            or evaluations[0][0] != EVALUATOR
        ):
            raise ValueError
        evaluated = evaluations[0][1]
        scores = evaluated.eval_output_items
        if (
            len(scores) != 1
            or scores[0].id != CASE_ID
            or scores[0].error is not None
            or type(scores[0].score) not in (int, float)
            or scores[0].score not in (0, 1)
            or evaluated.average_score != scores[0].score
            or SafeCaseOutput.model_validate_json(items[0].output_obj) != harness.safe_output
        ):
            raise ValueError
        passed = bool(scores[0].score) and _contract_passed(
            harness.safe_output, harness.expected_status
        )
        status, reason = (
            ("passed", "observed_contract") if passed else ("failed", "contract_failed")
        )
    except (AttributeError, IndexError, TypeError, ValueError):
        pass
    return NatEvaluationReport(
        status=status, reason=reason, nat_versions=versions, cases=cases, duration_ms=elapsed
    )


async def evaluate_synthetic(
    container,
    request: WorkRequest,
    principal: TrustedPrincipal,
    *,
    expected_status: WorkStatus = WorkStatus.COMPLETED,
) -> NatEvaluationReport:
    """Ready local/mock container, trusted identity, synthetic data only.

    No retry/resume: errors retain the service's durable state for its owner to
    inspect. A new call is a new evaluation, not permission to replay writes.
    """
    versions = _require_nat()
    if not container.ready or _ACTIVE.get() is not None:
        raise NatEvaluationError("NAT_LIFECYCLE_REQUIRED")
    settings = container.settings
    if (
        settings.model_provider != "mock"
        or settings.retriever_backend != "mock"
        or settings.response_backend != "mock"
        or settings.tool_backend != "mock"
        or settings.runtime_backend != "local"
        or settings.policy_backend != "local"
        or settings.trace_backend != "local"
        or settings.allow_external_writes
    ):
        raise NatEvaluationError("NAT_SYNTHETIC_ONLY")
    try:
        # Clone trusted DTOs before awaiting; caller mutations cannot alter a run.
        harness = _Harness(
            container,
            WorkRequest.model_validate(request.model_dump(mode="json")),
            TrustedPrincipal.model_validate(principal.model_dump(), strict=True),
            WorkStatus(expected_status),
        )
    except Exception:
        raise NatEvaluationError("NAT_INPUT_REJECTED") from None
    # Cached/partially executed runs have historical observations, not fresh NAT
    # provider calls. Never replay or award a fresh-execution pass for them.
    try:
        await container.repository.get_owned_run(harness.request.run_id, harness.principal)
    except ResourceNotFoundError:
        pass
    except Exception:
        raise NatEvaluationError("NAT_INPUT_REJECTED") from None
    else:
        raise NatEvaluationError("NAT_EXISTING_RUN_REJECTED")
    token = _ACTIVE.set(harness)
    start = perf_counter()
    try:
        # Covers NAT import/config/build/eval/cleanup, not merely the service call.
        with tracing_context(enabled=False, parent=False):
            _register_evaluator()
            from nat.data_models.evaluate_runtime import EvaluationRunConfig
            from nat.plugins.eval.eval_callbacks import EvalCallbackManager
            from nat.plugins.eval.runtime.evaluate import EvaluationRun

            config = await asyncio.to_thread(_load_config)
            callbacks = EvalCallbackManager()  # owned, empty; never caller/plugin callbacks
            run = EvaluationRun(
                EvaluationRunConfig(config_file=config, write_output=False, endpoint=None, reps=1),
                callback_manager=callbacks,
            )
            output = await run.run_and_evaluate()
            if callbacks._callbacks:
                raise NatEvaluationError("NAT_CALLBACKS_REJECTED")
            return _summarize(output, harness, versions, (perf_counter() - start) * 1000)
    except asyncio.CancelledError:
        raise
    except NatEvaluationError:
        raise
    except ImportError:
        raise NatEvaluationError("NAT_UNAVAILABLE") from None
    except Exception:
        raise NatEvaluationError("NAT_EVALUATION_FAILED") from None
    finally:
        _ACTIVE.reset(token)
