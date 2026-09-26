from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from rfa_mas.contracts import (
    AgentSpec,
    Audience,
    DraftBundle,
    EvidenceBundle,
    EvidenceRef,
    ModelRequest,
    PolicyDecision,
    PolicyRequest,
    ResultStatus,
    RetrievalRequest,
    SimulationScenario,
    StructuredError,
    TaskRequest,
    TaskResult,
    TrustedPrincipal,
    WorkRequest,
    sha256_text,
)
from rfa_mas.ports import ModelPort, PolicyPort, RetrievalPort

PRIVATE_CANARY_PATTERN = re.compile(r"SYNTHETIC_PRIVATE_CANARY_[A-Z0-9_]+")


def _evidence_refs(bundle: EvidenceBundle) -> tuple[EvidenceRef, ...]:
    return tuple(
        EvidenceRef(
            source_id=item.source_id,
            source_revision=item.source_revision,
            location=item.location,
            audience=item.audience,
            content_hash=item.content_hash,
        )
        for item in bundle.items
    )


@dataclass(frozen=True)
class DomainGraphDependencies:
    model: ModelPort
    retrieval: RetrievalPort
    policy: PolicyPort


@dataclass(frozen=True)
class InvocationContext:
    """Fresh authenticated identity, not persisted conversation state."""

    principal: TrustedPrincipal


class DomainState(TypedDict, total=False):
    spec: AgentSpec
    task: TaskRequest
    work: WorkRequest
    policy_decision: PolicyDecision
    evidence: Any
    draft: DraftBundle
    error: StructuredError
    steps: int


def _error(
    task: TaskRequest,
    code: str,
    message: str,
    *,
    retryable: bool = False,
) -> StructuredError:
    return StructuredError(
        code=code,
        retryable=retryable,
        message=message,
        request_id=task.request_id,
        trace_id=task.trace_id,
        run_id=task.run_id,
    )


def build_domain_graph(deps: DomainGraphDependencies) -> Any:
    async def authorize(state: DomainState, runtime: Runtime[InvocationContext]) -> dict[str, Any]:
        task = state["task"]
        work = state["work"]
        spec = state["spec"]
        if state.get("steps", 0) >= spec.max_steps:
            return {
                "error": _error(task, "budget_exceeded", "domain graph step 예산을 초과했습니다."),
                "steps": state.get("steps", 0),
            }
        action = (
            "force_deny_for_simulation"
            if work.simulation_scenario == SimulationScenario.POLICY_DENIED
            else "retrieve"
        )
        decision = await deps.policy.evaluate(
            PolicyRequest(
                request_id=task.request_id,
                trace_id=task.trace_id,
                run_id=task.run_id,
                agent_id=task.agent_id,
                domain_id=task.domain_id,
                action=action,
                resource_audience=Audience.PUBLIC,
                target_audience=work.target.audience,
                principal=runtime.context.principal,
            )
        )
        update: dict[str, Any] = {
            "policy_decision": decision,
            "steps": state.get("steps", 0) + 1,
        }
        if not decision.allowed:
            update["error"] = _error(task, "policy_denied", decision.safe_reason)
        return update

    def after_authorize(state: DomainState) -> str:
        return "finish" if "error" in state else "retrieve"

    async def retrieve(state: DomainState, runtime: Runtime[InvocationContext]) -> dict[str, Any]:
        task = state["task"]
        work = state["work"]
        if state["steps"] >= state["spec"].max_steps:
            return {
                "error": _error(
                    task,
                    "budget_exceeded",
                    "domain graph step 예산을 초과했습니다.",
                ),
                "steps": state["steps"],
            }
        try:
            evidence = await deps.retrieval.search(
                RetrievalRequest(
                    request_id=task.request_id,
                    trace_id=task.trace_id,
                    run_id=task.run_id,
                    agent_id=task.agent_id,
                    domain_id=task.domain_id,
                    query=work.query,
                    allowed_audiences=state["policy_decision"].allowed_audiences,
                    principal=runtime.context.principal,
                    simulation_scenario=work.simulation_scenario,
                )
            )
            return {"evidence": evidence, "steps": state["steps"] + 1}
        except TimeoutError:
            return {
                "error": _error(
                    task,
                    "retrieval_timeout",
                    "근거 검색 시간이 초과되었습니다.",
                    retryable=True,
                ),
                "steps": state["steps"] + 1,
            }

    def after_retrieve(state: DomainState) -> str:
        return "finish" if "error" in state else "generate"

    async def generate(state: DomainState) -> dict[str, Any]:
        task = state["task"]
        work = state["work"]
        if state["steps"] >= state["spec"].max_steps:
            return {
                "error": _error(
                    task,
                    "budget_exceeded",
                    "domain graph step 예산을 초과했습니다.",
                ),
                "steps": state["steps"],
            }
        model_result = await deps.model.generate(
            ModelRequest(
                request_id=task.request_id,
                trace_id=task.trace_id,
                run_id=task.run_id,
                agent_id=task.agent_id,
                domain_id=task.domain_id,
                query=work.query,
                evidence=state["evidence"],
                target=work.target,
                simulation_scenario=work.simulation_scenario,
            )
        )
        content = model_result.content
        if work.target.audience == Audience.PUBLIC:
            content = PRIVATE_CANARY_PATTERN.sub("[REDACTED_PRIVATE_CANARY]", content)
        draft = DraftBundle(
            request_id=task.request_id,
            trace_id=task.trace_id,
            run_id=task.run_id,
            agent_id=task.agent_id,
            domain_id=task.domain_id,
            content_hash=sha256_text(content),
            target=work.target,
            audience=work.target.audience,
            policy_version=state["policy_decision"].policy_version,
            allowed_evidence=_evidence_refs(state["evidence"]),
            content=content,
            simulated=model_result.simulated or state["evidence"].simulated,
            adapter=f"domain-taskgraph:{model_result.adapter}+{state['evidence'].adapter}",
        )
        return {"draft": draft, "steps": state["steps"] + 1}

    builder = StateGraph(DomainState, context_schema=InvocationContext)
    builder.add_node("authorize", authorize)
    builder.add_node("retrieve", retrieve)
    builder.add_node("generate", generate)
    builder.add_edge(START, "authorize")
    builder.add_conditional_edges(
        "authorize", after_authorize, {"retrieve": "retrieve", "finish": END}
    )
    builder.add_conditional_edges(
        "retrieve", after_retrieve, {"generate": "generate", "finish": END}
    )
    builder.add_edge("generate", END)
    # The supervisor checkpoints completed delegation. Mid-worker durable replay
    # is not claimed here; do not accidentally inherit its saver across RuntimePort.
    return builder.compile(checkpointer=False)


def build_domain_task_handler(deps: DomainGraphDependencies) -> Any:
    graph = build_domain_graph(deps)

    async def handler(spec: AgentSpec, task: TaskRequest) -> TaskResult:
        work = WorkRequest.model_validate(task.payload["work_request"])
        principal = TrustedPrincipal.model_validate(task.payload["principal"])
        final = await graph.ainvoke(
            DomainState(
                spec=spec,
                task=task,
                work=work,
                steps=0,
            ),
            context=InvocationContext(principal),
            # 1.2.12 inherits parent sync durability even when checkpointer=False,
            # then accesses an absent checkpoint future. Explicit exit avoids that
            # inherited path; this worker remains nonpersistent by design.
            durability="exit",
        )
        if error := final.get("error"):
            status = ResultStatus.DENIED if error.code == "policy_denied" else ResultStatus.FAILED
            if error.code.endswith("timeout"):
                status = ResultStatus.TIMED_OUT
            return TaskResult(
                request_id=task.request_id,
                trace_id=task.trace_id,
                run_id=task.run_id,
                agent_id=task.agent_id,
                domain_id=task.domain_id,
                status=status,
                output={"steps": final.get("steps", 0)},
                error=error,
                simulated=deps.model.simulated or deps.retrieval.simulated,
                adapter="domain-taskgraph",
            )
        return TaskResult(
            request_id=task.request_id,
            trace_id=task.trace_id,
            run_id=task.run_id,
            agent_id=task.agent_id,
            domain_id=task.domain_id,
            status=ResultStatus.SUCCEEDED,
            output={
                "draft": final["draft"].model_dump(mode="json"),
                "evidence": final["evidence"].model_dump(mode="json"),
                "steps": final["steps"],
            },
            simulated=final["draft"].simulated,
            adapter="domain-taskgraph",
        )

    return handler
