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
from rfa_mas.errors import RfaError
from rfa_mas.ports import ModelPort, PolicyPort, RetrievalPort

PRIVATE_CANARY_PATTERN = re.compile(r"SYNTHETIC_PRIVATE_CANARY_[A-Z0-9_]+")
# P1-005: deterministic private-content markers screened BEFORE any model call for a
# non-owner target (synthetic canaries; owner disclosure preferences add more).
SENSITIVE_MARKERS = (
    re.compile(r"SYNTHETIC_PRIVATE_CANARY_[A-Z0-9_]+"),
    re.compile(r"\bCANARY_[A-Z0-9_]+"),
)
SHAREABLE = {
    Audience.PUBLIC: frozenset({Audience.PUBLIC}),
    Audience.COMPANY: frozenset({Audience.PUBLIC, Audience.COMPANY}),
    Audience.BUSINESS_UNIT: frozenset({Audience.PUBLIC, Audience.COMPANY, Audience.BUSINESS_UNIT}),
}


def _sensitive(text: str, extra: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(p.search(text) for p in SENSITIVE_MARKERS) or any(
        marker.lower() in lowered for marker in extra if marker
    )


STYLE_GUIDANCE_HEADER = "[사용자 표현 선호 — 문체에만 적용; 정책·근거·공유 범위를 바꾸지 않음]"


def with_style_guidance(query: str, guidance: tuple[str, ...]) -> str:
    """Advisory owner style preferences (P1-005B) appended as a delimited model input."""
    if not guidance:
        return query
    return "\n".join([query, "", STYLE_GUIDANCE_HEADER, *(f"- {item}" for item in guidance)])


def share_egress_filter(
    evidence: EvidenceBundle,
    *,
    target: Audience,
    endpoint: str,
    markers: tuple[str, ...] = (),
    private_egress: bool = False,
) -> tuple[EvidenceBundle, dict[str, int]]:
    """Shareable-with-target first, then content screen, then endpoint egress.

    Readable is not shareable: an item survives only if its audience may be shared with
    the draft target, it carries no private marker (non-owner targets), and a cloud model
    endpoint receives public material only, unless the owner consented (P1-008K,
    ALLOW_EXTERNAL_EGRESS) and the target is the owner. Items are withheld whole, never
    redacted.
    """
    owner_bound = target in {Audience.OWNER, Audience.PRIVATE}
    kept, withheld = [], {"share": 0, "sensitive": 0, "egress": 0}
    for item in evidence.items:
        if target in SHAREABLE and item.audience not in SHAREABLE[target]:
            withheld["share"] += 1
        elif not owner_bound and _sensitive(item.excerpt, markers):
            withheld["sensitive"] += 1
        elif (
            endpoint != "local"
            and item.audience != Audience.PUBLIC
            and not (private_egress and owner_bound)
        ):
            withheld["egress"] += 1
        else:
            kept.append(item)
    filtered = evidence.model_copy(
        update={
            "items": tuple(kept),
            "insufficient": evidence.insufficient or not kept,
        }
    )
    return filtered, withheld


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
    # P1-005: trusted staged-context factory (owner/public targets, local endpoints).
    context: Any = None
    # "local" for in-process/mock models; anything else is treated as a cloud endpoint.
    model_endpoint: str = "local"
    # P1-008K: owner consent (ALLOW_EXTERNAL_EGRESS) for the owner's context to that endpoint.
    private_egress: bool = False
    # Owner personal disclosure markers (P1-005B feedback); never relaxes policy.
    disclosure_markers: Any = None
    # Owner style preferences (P1-005B feedback): advisory model input only; the
    # deterministic share/content/egress screen above is independent of it.
    style_guidance: Any = None


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
    context_stats: Any
    withheld: Any
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
            request = RetrievalRequest(
                request_id=task.request_id,
                trace_id=task.trace_id,
                run_id=task.run_id,
                agent_id=task.agent_id,
                domain_id=task.domain_id,
                query=work.query,
                # Intersect the policy decision with the delegated agent's audience cap,
                # so the target's excluded audiences (e.g. private) are never read.
                allowed_audiences=tuple(
                    audience
                    for audience in state["policy_decision"].allowed_audiences
                    if audience in state["spec"].allowed_audiences
                ),
                principal=runtime.context.principal,
                simulation_scenario=work.simulation_scenario,
            )
            loaded = None
            if (
                deps.context is not None
                and deps.model_endpoint == "local"
                and work.simulation_scenario == SimulationScenario.SUCCESS
            ):
                # P1-005: staged L0/L1/L2 loading for supported targets; None = unsupported.
                loaded = await deps.context(runtime.context.principal, work, request)
            if loaded is not None:
                evidence, stats = loaded
                return {"evidence": evidence, "context_stats": stats, "steps": state["steps"] + 1}
            evidence = await deps.retrieval.search(request)
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
        except RfaError as exc:
            code = (
                exc.code
                if exc.code in {"policy_denied", "resume_review_required"}
                else "context_unavailable"
            )
            return {
                "error": _error(task, code, "현재 근거를 불러올 수 없습니다."),
                "steps": state["steps"] + 1,
            }

    def after_retrieve(state: DomainState) -> str:
        return "finish" if "error" in state else "generate"

    async def generate(state: DomainState, runtime: Runtime[InvocationContext]) -> dict[str, Any]:
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
        markers: tuple[str, ...] = ()
        if deps.disclosure_markers is not None:
            markers = tuple(
                await deps.disclosure_markers(
                    runtime.context.principal,
                    task.domain_id,
                    target=work.target,
                    run_id=task.run_id,
                )
            )
        target = work.target.audience
        # A public-bound request text itself must not carry private markers to the model.
        if target not in {Audience.OWNER, Audience.PRIVATE} and _sensitive(work.query, markers):
            return {
                "error": _error(task, "policy_denied", "공개 대상 요청에 비공개 정보가 있습니다."),
                "steps": state["steps"] + 1,
            }
        evidence, withheld = share_egress_filter(
            state["evidence"],
            target=target,
            endpoint=deps.model_endpoint,
            markers=markers,
            private_egress=deps.private_egress,
        )
        query = work.query
        if deps.style_guidance is not None:
            owner_bound = target in {Audience.OWNER, Audience.PRIVATE}
            guidance = tuple(
                await deps.style_guidance(
                    runtime.context.principal,
                    task.domain_id,
                    target=work.target,
                    run_id=task.run_id,
                    # Same content screen as the request text for a non-owner target.
                    keep=lambda text: owner_bound or not _sensitive(text, markers),
                )
            )
            query = with_style_guidance(work.query, guidance)
        model_result = await deps.model.generate(
            ModelRequest(
                request_id=task.request_id,
                trace_id=task.trace_id,
                run_id=task.run_id,
                agent_id=task.agent_id,
                domain_id=task.domain_id,
                query=query,
                evidence=evidence,
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
            # Only evidence that actually passed the share/sensitive/egress screen.
            allowed_evidence=_evidence_refs(evidence),
            content=content,
            simulated=model_result.simulated or evidence.simulated,
            adapter=f"domain-taskgraph:{model_result.adapter}+{evidence.adapter}",
        )
        return {
            "draft": draft,
            "evidence": evidence,
            "withheld": withheld,
            "steps": state["steps"] + 1,
        }

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
                # Measured staging/screen facts only (counts, characters); no text.
                "context": final.get("context_stats"),
                "withheld": final.get("withheld"),
            },
            simulated=final["draft"].simulated,
            adapter="domain-taskgraph",
        )

    return handler
