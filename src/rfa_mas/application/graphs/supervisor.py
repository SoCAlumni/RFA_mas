from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from pydantic import ValidationError

from rfa_mas.contracts import (
    AgentSpec,
    Audience,
    DomainId,
    DraftBundle,
    PublicationStatus,
    ResultStatus,
    ReviewDecision,
    ReviewStatus,
    StructuredError,
    TaskRequest,
    TaskResult,
    TrustedPrincipal,
    WorkRequest,
    WorkStatus,
)
from rfa_mas.ports import ResponsePort, RuntimePort
from rfa_mas.application.graphs.domain import InvocationContext


@dataclass(frozen=True)
class SupervisorDependencies:
    runtime: RuntimePort
    response: ResponsePort
    max_graph_steps: int
    max_tool_calls: int
    validate_resume: Callable[[DraftBundle, TrustedPrincipal], Awaitable[None]]


@dataclass(frozen=True)
class DomainConfiguration:
    domain_id: DomainId
    memory_namespace: str
    capabilities: tuple[str, ...]
    instructions_ref: str


DOMAIN_CONFIGS: dict[DomainId, DomainConfiguration] = {
    DomainId.TRIV3: DomainConfiguration(
        domain_id=DomainId.TRIV3,
        memory_namespace="domain/triv3",
        capabilities=("evidence_search", "benchmark_analysis", "draft_generation"),
        instructions_ref="domain://triv3/v1",
    ),
    DomainId.QUANTIZATION_RESEARCH: DomainConfiguration(
        domain_id=DomainId.QUANTIZATION_RESEARCH,
        memory_namespace="domain/quantization-research",
        capabilities=("evidence_search", "research_synthesis", "draft_generation"),
        instructions_ref="domain://quantization-research/v1",
    ),
}


class SupervisorState(TypedDict, total=False):
    work: WorkRequest
    domain_id: DomainId
    draft: DraftBundle
    review: ReviewDecision
    status: WorkStatus
    publication_status: PublicationStatus
    error: StructuredError
    steps: int


def _route_domain(work: WorkRequest) -> DomainId | None:
    if work.domain_id is not None:
        return work.domain_id
    lowered = work.query.lower()
    if any(term in lowered for term in ("quant", "quantization", "양자화")):
        return DomainId.QUANTIZATION_RESEARCH
    if any(term in lowered for term in ("triv3", "benchmark", "벤치마크", "근거")):
        return DomainId.TRIV3
    return None


def _allowed_audiences(principal: TrustedPrincipal, target: Audience) -> tuple[Audience, ...]:
    """Cap a delegated agent to audiences supported by identity and its output target."""

    allowed = [Audience.PUBLIC]
    if target == Audience.PUBLIC or not principal.authenticated:
        return tuple(allowed)

    if principal.company_id:
        allowed.append(Audience.COMPANY)
    if target == Audience.COMPANY:
        return tuple(allowed)

    if principal.business_units:
        allowed.append(Audience.BUSINESS_UNIT)
    if target == Audience.BUSINESS_UNIT:
        return tuple(allowed)

    allowed.append(Audience.OWNER)
    if target == Audience.PRIVATE:
        allowed.append(Audience.PRIVATE)
    return tuple(allowed)


def _error(work: WorkRequest, code: str, message: str) -> StructuredError:
    return StructuredError(
        code=code,
        retryable=False,
        message=message,
        request_id=work.request_id,
        trace_id=work.trace_id,
        run_id=work.run_id,
    )


def _task_result_matches(result: TaskResult, task: TaskRequest) -> bool:
    return (
        result.request_id == task.request_id
        and result.trace_id == task.trace_id
        and result.run_id == task.run_id
        and result.agent_id == task.agent_id
        and result.domain_id == task.domain_id
    )


def _draft_matches(
    draft: DraftBundle,
    *,
    task: TaskRequest,
    work: WorkRequest,
    spec: AgentSpec,
) -> bool:
    if (
        not draft.draft_id
        or draft.request_id != task.request_id
        or draft.trace_id != task.trace_id
        or draft.run_id != task.run_id
        or draft.agent_id != task.agent_id
        or draft.domain_id != task.domain_id
        or draft.target != work.target
        or draft.audience != work.target.audience
    ):
        return False
    if any(item.audience not in spec.allowed_audiences for item in draft.allowed_evidence):
        return False
    return not (
        work.target.audience == Audience.PUBLIC
        and any(item.audience != Audience.PUBLIC for item in draft.allowed_evidence)
    )


def _review_matches(
    decision: ReviewDecision,
    *,
    draft: DraftBundle,
    work: WorkRequest,
) -> bool:
    return (
        decision.request_id == work.request_id
        and decision.trace_id == work.trace_id
        and decision.run_id == work.run_id
        and decision.agent_id == draft.agent_id
        and decision.domain_id == draft.domain_id
        and decision.draft_id == draft.draft_id
        and decision.draft_version == draft.version
        and decision.content_hash == draft.content_hash
        and decision.target == draft.target
    )


def _domain_steps(result: TaskResult, *, required: bool, maximum: int) -> int:
    raw_steps = result.output.get("steps")
    if raw_steps is None and not required:
        return 0
    if (
        isinstance(raw_steps, bool)
        or not isinstance(raw_steps, int)
        or raw_steps < 0
        or raw_steps > maximum
    ):
        raise ValueError("invalid domain step count")
    return raw_steps


def build_supervisor_graph(deps: SupervisorDependencies, *, checkpointer: Any = None) -> Any:
    async def route(state: SupervisorState) -> dict[str, Any]:
        work = state["work"]
        domain_id = _route_domain(work)
        if domain_id is None:
            return {
                "error": StructuredError(
                    code="domain_not_resolved",
                    retryable=False,
                    message=(
                        "지원하는 도메인을 명시하거나 TRIV3/quantization 관련 질의를 사용해 주세요."
                    ),
                    request_id=work.request_id,
                    trace_id=work.trace_id,
                    run_id=work.run_id,
                ),
                "status": WorkStatus.FAILED,
                "steps": 1,
            }
        return {"domain_id": domain_id, "steps": 1}

    def after_route(state: SupervisorState) -> str:
        return "finish" if state.get("error") is not None else "delegate"

    async def delegate(
        state: SupervisorState, runtime: Runtime[InvocationContext]
    ) -> dict[str, Any]:
        work = state["work"]
        domain_id = state["domain_id"]
        config = DOMAIN_CONFIGS[domain_id]
        if state["steps"] >= deps.max_graph_steps:
            return {
                "error": _error(
                    work,
                    "budget_exceeded",
                    "Supervisor graph step 예산을 초과했습니다.",
                ),
                "status": WorkStatus.FAILED,
                "steps": state["steps"],
            }
        delegation_steps = state["steps"] + 1
        remaining_domain_steps = deps.max_graph_steps - delegation_steps
        if remaining_domain_steps < 1:
            return {
                "error": _error(
                    work,
                    "budget_exceeded",
                    "도메인 작업을 시작할 Supervisor graph step 예산이 부족합니다.",
                ),
                "status": WorkStatus.FAILED,
                "steps": delegation_steps,
            }
        spec = AgentSpec(
            agent_id=f"domain-supervisor:{domain_id.value}",
            domain_id=domain_id,
            memory_namespace=config.memory_namespace,
            capabilities=config.capabilities,
            allowed_audiences=_allowed_audiences(runtime.context.principal, work.target.audience),
            instructions_ref=config.instructions_ref,
            max_steps=remaining_domain_steps,
            max_tool_calls=deps.max_tool_calls,
        )
        task = TaskRequest(
            request_id=work.request_id,
            trace_id=work.trace_id,
            run_id=work.run_id,
            agent_id=spec.agent_id,
            domain_id=domain_id,
            idempotency_key=f"{work.idempotency_key}:domain:{domain_id.value}",
            task_type="domain_task",
            payload={
                "work_request": work.model_dump(mode="json"),
                "principal": runtime.context.principal.model_dump(mode="json"),
            },
        )
        try:
            raw_result = await deps.runtime.run(spec, task)
            result = TaskResult.model_validate(raw_result)
        except (ValidationError, TypeError):
            return {
                "error": _error(
                    work,
                    "invalid_runtime_result",
                    "runtime 결과가 합의된 TaskResult 계약을 충족하지 않습니다.",
                ),
                "status": WorkStatus.FAILED,
                "steps": delegation_steps,
            }
        if not _task_result_matches(result, task):
            return {
                "error": _error(
                    work,
                    "runtime_result_binding_mismatch",
                    "runtime 결과의 요청, 실행, agent 또는 domain 식별자가 일치하지 않습니다.",
                ),
                "status": WorkStatus.FAILED,
                "steps": delegation_steps,
            }
        try:
            domain_steps = _domain_steps(
                result,
                required=result.status == ResultStatus.SUCCEEDED,
                maximum=spec.max_steps,
            )
        except ValueError:
            return {
                "error": _error(
                    work,
                    "invalid_runtime_output",
                    "runtime 결과의 domain step 사용량이 유효하지 않습니다.",
                ),
                "status": WorkStatus.FAILED,
                "steps": delegation_steps,
            }
        cumulative_steps = delegation_steps + domain_steps
        if result.status != ResultStatus.SUCCEEDED:
            return {
                "error": result.error
                or _error(work, "domain_task_failed", "도메인 작업이 완료되지 않았습니다."),
                "status": WorkStatus.FAILED,
                "steps": cumulative_steps,
            }
        try:
            draft = DraftBundle.model_validate(result.output["draft"])
        except (KeyError, TypeError, ValidationError):
            return {
                "error": _error(
                    work,
                    "invalid_runtime_output",
                    "runtime 성공 결과에 유효한 DRAFT가 없습니다.",
                ),
                "status": WorkStatus.FAILED,
                "steps": cumulative_steps,
            }
        if not _draft_matches(draft, task=task, work=work, spec=spec):
            return {
                "error": _error(
                    work,
                    "draft_binding_mismatch",
                    (
                        "runtime DRAFT의 식별자, domain, 대상 또는 근거 범위가 "
                        "요청과 일치하지 않습니다."
                    ),
                ),
                "status": WorkStatus.FAILED,
                "steps": cumulative_steps,
            }
        if cumulative_steps >= deps.max_graph_steps:
            return {
                "draft": draft,
                "error": _error(
                    work,
                    "budget_exceeded",
                    "검토를 시작할 Supervisor graph step 예산이 부족합니다.",
                ),
                "status": WorkStatus.FAILED,
                "steps": cumulative_steps,
            }
        return {
            "draft": draft,
            "steps": cumulative_steps,
        }

    def after_delegate(state: SupervisorState) -> str:
        return "finish" if state.get("error") is not None else "review"

    async def review(state: SupervisorState) -> dict[str, Any]:
        work = state["work"]
        if state["steps"] >= deps.max_graph_steps:
            return {
                "error": _error(
                    work,
                    "budget_exceeded",
                    "Supervisor graph step 예산을 초과해 검토를 실행하지 않았습니다.",
                ),
                "status": WorkStatus.FAILED,
                "steps": state["steps"],
            }
        draft = state["draft"]
        try:
            raw_decision = await deps.response.submit_draft(
                draft,
                idempotency_key=f"{work.idempotency_key}:review",
                simulation_scenario=work.simulation_scenario,
            )
            decision = ReviewDecision.model_validate(raw_decision)
        except (ValidationError, TypeError):
            return {
                "error": _error(
                    work,
                    "invalid_review_result",
                    "검토 결과가 합의된 ReviewDecision 계약을 충족하지 않습니다.",
                ),
                "status": WorkStatus.FAILED,
                "steps": state["steps"] + 1,
            }
        if not _review_matches(decision, draft=draft, work=work):
            return {
                "error": _error(
                    work,
                    "approval_binding_mismatch",
                    "검토 결과가 현재 요청과 초안 버전, hash, 대상에 일치하지 않습니다.",
                ),
                "status": WorkStatus.FAILED,
                "steps": state["steps"] + 1,
            }
        if decision.decision == ReviewStatus.APPROVED:
            status = WorkStatus.COMPLETED
        elif decision.decision in {ReviewStatus.PENDING, ReviewStatus.REVISION_REQUESTED}:
            status = WorkStatus.WAITING_APPROVAL
        else:
            status = WorkStatus.FAILED
        return {
            "review": decision,
            "status": status,
            "publication_status": decision.publication_status,
            "steps": state["steps"] + 1,
        }

    async def await_review(
        state: SupervisorState, runtime: Runtime[InvocationContext]
    ) -> dict[str, Any]:
        # Resume is a wakeup only. Submit happens in a prior checkpointed node,
        # so restarting this node cannot re-submit a draft or fabricate approval.
        interrupt(
            {
                "kind": "review_pending",
                "run_id": state["work"].run_id,
                "draft_id": state["draft"].draft_id,
            }
        )
        work, draft = state["work"], state["draft"]
        await deps.validate_resume(draft, runtime.context.principal)
        raw_decision = await deps.response.get_decision(draft.draft_id)
        if raw_decision is None:
            return {"status": WorkStatus.WAITING_APPROVAL}
        decision = ReviewDecision.model_validate(raw_decision)
        if not _review_matches(decision, draft=draft, work=work):
            return {
                "error": _error(
                    work, "approval_binding_mismatch", "현재 초안과 승인 참조가 일치하지 않습니다."
                ),
                "status": WorkStatus.FAILED,
            }
        if decision.decision == ReviewStatus.APPROVED:
            status = WorkStatus.COMPLETED
        elif decision.decision == ReviewStatus.REJECTED:
            status = WorkStatus.FAILED
        else:
            status = WorkStatus.WAITING_APPROVAL
        return {
            "review": decision,
            "status": status,
            "publication_status": decision.publication_status,
        }

    def after_review(state: SupervisorState) -> str:
        return "wait" if state.get("status") == WorkStatus.WAITING_APPROVAL else "finish"

    builder = StateGraph(SupervisorState, context_schema=InvocationContext)
    builder.add_node("route", route)
    builder.add_node("delegate", delegate)
    builder.add_node("review", review)
    builder.add_node("await_review", await_review)
    builder.add_edge(START, "route")
    builder.add_conditional_edges("route", after_route, {"delegate": "delegate", "finish": END})
    builder.add_conditional_edges("delegate", after_delegate, {"review": "review", "finish": END})
    builder.add_conditional_edges("review", after_review, {"wait": "await_review", "finish": END})
    builder.add_conditional_edges(
        "await_review", after_review, {"wait": "await_review", "finish": END}
    )
    return builder.compile(checkpointer=checkpointer)
