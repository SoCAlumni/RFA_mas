from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from pydantic import ValidationError

from rfa_mas.application.graphs.domain import InvocationContext
from rfa_mas.contracts import (
    AgentSpec,
    Audience,
    DomainId,
    DraftBundle,
    DraftTarget,
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
from rfa_mas.errors import RfaError
from rfa_mas.ports import ResponsePort, RuntimePort


@dataclass(frozen=True)
class SupervisorDependencies:
    runtime: RuntimePort
    response: ResponsePort
    max_graph_steps: int
    max_tool_calls: int
    validate_resume: Callable[[DraftBundle, TrustedPrincipal], Awaitable[None]]
    # P1-004A: deterministic Supervisor gate for derived knowledge (optional).
    accumulator: Any = None
    # P0-020 explicit team execution. None keeps the single-domain path only.
    team_runner: Any = None
    policy_version: Callable[[], str] | None = None
    # P0-021 durable effect ledger (EffectLedger): cancel/revocation barrier reads.
    effects: Any = None


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
    # Explicit team execution intent/binding: plain JSON, no principal or grants.
    team_request: dict[str, Any] | None
    task_id: str | None
    team_result: dict[str, Any] | None


def _route_domain(work: WorkRequest) -> DomainId | None:
    if work.domain_id is not None:
        return work.domain_id
    lowered = work.query.lower()
    if any(term in lowered for term in ("quant", "quantization", "양자화")):
        return DomainId.QUANTIZATION_RESEARCH
    if any(term in lowered for term in ("triv3", "benchmark", "벤치마크", "근거")):
        return DomainId.TRIV3
    return None



# -- P1-004 deterministic assistant intent routing -------------------------------------
# Ordered rules: first match wins. Text never grants identity, permission or a team.
INTENT_RULES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("unsupported", "destructive-or-external-action",
     ("삭제해", "지워줘", "배포해", "결제", "송금", "delete all", "deploy")),
    ("schedule", "schedule", ("매일", "매주", "예약", "알림", "리마인드", "아침마다", "cron")),
    ("feedback", "feedback", ("피드백", "앞으로는", "다음부터", "선호", "말투")),
    ("store_note", "store", ("저장해", "메모해", "기록해", "정리해 둬", "note:", "remember")),
    ("external_draft", "external", ("공개 채널", "외부에", "외부 공유", "고객", "공지", "답변 초안")),
    ("task_run", "task",
     # Action verbs only: a question that merely mentions a benchmark stays a query.
     ("검증해", "분석해", "조사해", "비교해", "실험해", "연구해", "벤치마크 돌려",
      "run benchmark", "investigate")),
)
BENCHMARK_TERMS = ("벤치마크", "benchmark", "실험", "지연", "latency", "로그")
CHANNEL_ALLOWED = frozenset({"query", "external_draft"})
PATTERN_OUTPUTS = {"benchmark": ("benchmark_report",), "research": ("research_report",)}
NEXT_OPTIONS = {
    "schedule": ("반복 주기와 시각을 함께 적어 주세요. 예: '매일 오전 9시 TRIV3 브리핑', "
                 "'매주 월요일 18시 할 일 후보 정리', 'cron 0 9 * * mon-fri'.",
                 "구조화된 예약은 /v1/schedules로 직접 만들 수 있습니다."),
    "feedback": ("피드백 분류·적용(P1-005B)이 준비되면 같은 요청을 다시 보내세요.",
                 "초안 수정은 검토 단계에서 요청할 수 있습니다."),
    "destructive-or-external-action": ("삭제·배포·결제는 비서가 수행하지 않습니다.",
                                       "자료 수정은 지식 관리 화면/API의 소유자 변경을 사용하세요."),
    "channel-scope": ("외부/내부 채널은 질의와 공개 답변 초안만 요청할 수 있습니다.",),
    "domain-required": ("도메인(triv3 또는 quantization_research)을 지정해 다시 요청하세요.",),
}


# -- P0-022 deterministic schedule wording -> allowlisted job + 5-field cron --------------
# First match wins. Only these three job types exist; nothing in the text becomes a
# command, URL or prompt. Weekdays are always names (APScheduler 3.x numbers Monday=0).
SCHEDULE_JOB_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("candidate_scan", ("할 일", "할일", "작업 후보", "후보", "todo")),
    ("kb_refresh", ("자료 정리", "지식 정리", "새로고침", "갱신", "동기화", "refresh")),
    ("briefing", ("브리핑", "요약", "briefing", "알림", "알려", "리마인드", "remind")),
)
_WEEKDAYS = {"월": "mon", "화": "tue", "수": "wed", "목": "thu", "금": "fri", "토": "sat",
             "일": "sun"}
_EXPLICIT_CRON = re.compile(r"cron[:\s]+((?:[a-z0-9*/,\-]+\s+){4}[a-z0-9*/,\-]+)")
_CLOCK = re.compile(r"(\d{1,2}):(\d{2})")
_KOREAN_TIME = re.compile(
    r"(오전|오후|아침|저녁|밤)?\s*(\d{1,2})\s*시(?:\s*(\d{1,2})\s*분|\s*(반))?"
)


def parse_schedule(text: str) -> dict[str, str] | None:
    """Deterministic schedule wording -> {job_type, cron}; None when details are missing."""
    lowered = " ".join(text.lower().split())
    job_type = next(
        (name for name, terms in SCHEDULE_JOB_TERMS if any(t in lowered for t in terms)), None
    )
    if job_type is None:
        return None
    explicit = _EXPLICIT_CRON.search(lowered)
    if explicit:
        return {"job_type": job_type, "cron": explicit.group(1)}
    if "평일" in lowered or "weekday" in lowered:
        day_of_week = "mon-fri"
    elif "주말" in lowered or "weekend" in lowered:
        day_of_week = "sat,sun"
    elif days := re.findall(r"([월화수목금토일])요일", lowered):
        day_of_week = ",".join(dict.fromkeys(_WEEKDAYS[d] for d in days))
    elif any(t in lowered for t in ("매일", "마다", "daily", "every day")):
        day_of_week = "*"
    else:
        return None
    hour = minute = None
    if clock := _CLOCK.search(lowered):
        hour, minute = int(clock.group(1)), int(clock.group(2))
    elif match := _KOREAN_TIME.search(lowered):
        period, hour = match.group(1), int(match.group(2))
        minute = int(match.group(3)) if match.group(3) else 30 if match.group(4) else 0
        if period in {"오후", "저녁", "밤"} and hour < 12:
            hour += 12
        elif period in {"오전", "아침"} and hour == 12:
            hour = 0
    elif "아침" in lowered or "morning" in lowered:
        hour, minute = 9, 0
    elif "저녁" in lowered or "evening" in lowered:
        hour, minute = 18, 0
    if hour is None or minute is None or not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return {"job_type": job_type, "cron": f"{minute} {hour} * * {day_of_week}"}


def route_intent(text: str, *, domain_id: DomainId | None, task_id: str | None,
                 ingress: str) -> dict[str, Any]:
    """Pure, deterministic routing. The same input always yields the same decision."""
    lowered = " ".join(text.lower().split())
    intent, rule = "query", "default-query"
    for name, rule_id, terms in INTENT_RULES:
        if any(term in lowered for term in terms):
            intent, rule = name, rule_id
            break
    if task_id is not None and intent in {"query", "task_run"}:
        intent, rule = "task_run", "existing-task"
    domain = domain_id or _route_domain(
        WorkRequest(query=text, domain_id=None, target=DraftTarget(audience=Audience.OWNER))
    )
    decision: dict[str, Any] = {
        "intent": intent, "domain_id": domain, "rule": rule, "supported": True,
    }
    if ingress != "direct" and intent not in CHANNEL_ALLOWED:
        return decision | {"intent": "unsupported", "rule": "channel-scope", "supported": False,
                           "limitations": ("channel_scope",),
                           "next_options": NEXT_OPTIONS["channel-scope"]}
    if intent in {"feedback", "unsupported"}:
        return decision | {"supported": False, "limitations": (f"{intent}_not_available",),
                           "next_options": NEXT_OPTIONS[rule if intent == "unsupported" else intent]}
    if domain is None:
        return decision | {"supported": False, "limitations": ("domain_not_resolved",),
                           "next_options": NEXT_OPTIONS["domain-required"]}
    if intent == "schedule":
        parsed = parse_schedule(text)
        if parsed is None:
            return decision | {"supported": False, "limitations": ("schedule_details_required",),
                               "next_options": NEXT_OPTIONS["schedule"]}
        # Intent only: the owner-scoped ScheduleService validates and stores it.
        decision["schedule"] = parsed | {"domain_id": domain,
                                         "task_ref": task_id}
    if intent == "task_run":
        pattern = "benchmark" if any(t in lowered for t in BENCHMARK_TERMS) else "research"
        decision["task_candidate"] = {"goal": text[:2000], "pattern": pattern}
        decision["task_ref"] = task_id
    return decision



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
    async def barrier(work: WorkRequest, principal: TrustedPrincipal) -> dict[str, Any] | None:
        """Durable cancel/revocation barrier, checked before every effectful step.

        It is read from the ledger store on each step, so a barrier recorded by another
        request or process (or before a restart) stops the next step here.
        """
        if deps.effects is None:
            return None
        reason = await deps.effects.run_barrier(work.run_id, principal)
        if reason is None:
            return None
        code = "cancelled" if reason == "cancelled" else "permission_revoked"
        return {
            "error": _error(work, code, "실행이 중단되어 이후 작업을 시작하지 않았습니다."),
            "status": WorkStatus.CANCELLED,
        }

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
        if state.get("error") is not None:
            return "finish"
        return "delegate_team" if state.get("team_request") else "delegate"

    async def delegate_team(
        state: SupervisorState, runtime: Runtime[InvocationContext]
    ) -> dict[str, Any]:
        from rfa_mas.application.workers import team_draft
        from rfa_mas.contracts import TeamExecutionRequest

        work = state["work"]
        work = work.model_copy(update={"domain_id": state["domain_id"]})
        steps = state["steps"] + 1
        stopped = await barrier(work, runtime.context.principal)
        if stopped is not None:
            return stopped | {"steps": steps}
        if deps.team_runner is None or deps.policy_version is None:
            return {
                "error": _error(work, "not_implemented", "팀 실행이 구성되지 않았습니다."),
                "status": WorkStatus.FAILED,
                "steps": steps,
            }
        principal = runtime.context.principal
        request = TeamExecutionRequest.model_validate(state["team_request"])
        try:
            lifecycle = await deps.team_runner.ensure_and_bind(
                work, request, principal, task_id=state.get("task_id")
            )
            result = await deps.team_runner.execute(
                work,
                request,
                lifecycle,
                principal,
                _allowed_audiences(principal, work.target.audience),
            )
        except RfaError as exc:
            return {"error": _error(work, exc.code, exc.safe_message), "status": WorkStatus.FAILED,
                    "steps": steps}
        update: dict[str, Any] = {
            "steps": steps + result.usage.steps,
            "team_result": {"status": result.status, "stop_reason": result.stop_reason,
                            "task_id": result.task_id},
        }
        if result.status != "completed":
            # Partial/failed/cancelled team work never starts review; the safe partial
            # result stays in the team receipt store under the fixed stop reason.
            code = result.stop_reason or "role_failed"
            update.update(
                error=_error(work, code, "팀 실행을 안전하게 중단했습니다."),
                status=WorkStatus.CANCELLED if result.status == "cancelled" else WorkStatus.FAILED,
            )
            return update
        if deps.accumulator is not None:
            # Only this Supervisor gate turns reviewed role outputs into derived knowledge;
            # workers never write the KB. Failures keep the run result and are counted.
            try:
                proposals = await deps.accumulator.team_proposals(
                    work.domain_id, principal, result
                )
                report = await deps.accumulator.accumulate(work.domain_id, proposals, principal)
                update["team_result"] = update["team_result"] | {
                    "accumulated": sum(i.review_state == "accepted" for i in report.items),
                    "rejected": sum(i.review_state == "rejected" for i in report.items),
                }
            except RfaError:
                update["team_result"] = update["team_result"] | {"accumulation": "failed"}
        draft = team_draft(work, result, policy_version=deps.policy_version())
        if work.target.audience == Audience.PUBLIC and any(
            item.audience != Audience.PUBLIC for item in draft.allowed_evidence
        ):
            update.update(error=_error(work, "draft_binding_mismatch", "공개 대상 근거가 아닙니다."),
                          status=WorkStatus.FAILED)
            return update
        update["draft"] = draft
        return update

    async def delegate(
        state: SupervisorState, runtime: Runtime[InvocationContext]
    ) -> dict[str, Any]:
        work = state["work"]
        domain_id = state["domain_id"]
        config = DOMAIN_CONFIGS[domain_id]
        stopped = await barrier(work, runtime.context.principal)
        if stopped is not None:
            return stopped | {"steps": state["steps"]}
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

    async def review(
        state: SupervisorState, runtime: Runtime[InvocationContext]
    ) -> dict[str, Any]:
        work = state["work"]
        stopped = await barrier(work, runtime.context.principal)
        if stopped is not None:
            return stopped | {"steps": state["steps"]}
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
        stopped = await barrier(work, runtime.context.principal)
        if stopped is not None:
            return stopped | {"steps": state["steps"]}
        await deps.validate_resume(draft, runtime.context.principal)
        if state["steps"] >= deps.max_graph_steps:
            return {
                "error": _error(work, "budget_exceeded", "검토 상태 조회 예산을 초과했습니다."),
                "status": WorkStatus.FAILED,
            }
        steps = state["steps"] + 1
        try:
            raw_decision = await deps.response.get_decision(draft.draft_id)
        except RfaError as exc:
            if not exc.retryable and exc.code != "outcome_unknown":
                raise
            return {
                "status": WorkStatus.WAITING_APPROVAL,
                "steps": steps,
                "error": _error(
                    work, "review_query_pending", "검토 상태를 확정할 수 없어 재조회를 기다립니다."
                ),
            }
        except TimeoutError:
            return {
                "status": WorkStatus.WAITING_APPROVAL,
                "steps": steps,
                "error": _error(
                    work, "review_query_pending", "검토 상태를 확정할 수 없어 재조회를 기다립니다."
                ),
            }
        if raw_decision is None:
            return {"status": WorkStatus.WAITING_APPROVAL, "steps": steps, "error": None}
        decision = ReviewDecision.model_validate(raw_decision)
        if not _review_matches(decision, draft=draft, work=work):
            return {
                "error": _error(
                    work, "approval_binding_mismatch", "현재 초안과 승인 참조가 일치하지 않습니다."
                ),
                "status": WorkStatus.FAILED,
                "steps": steps,
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
            "steps": steps,
            "error": None,
        }

    def after_review(state: SupervisorState) -> str:
        return "wait" if state.get("status") == WorkStatus.WAITING_APPROVAL else "finish"

    builder = StateGraph(SupervisorState, context_schema=InvocationContext)
    builder.add_node("route", route)
    builder.add_node("delegate", delegate)
    builder.add_node("delegate_team", delegate_team)
    builder.add_node("review", review)
    builder.add_node("await_review", await_review)
    builder.add_edge(START, "route")
    builder.add_conditional_edges(
        "route",
        after_route,
        {"delegate": "delegate", "delegate_team": "delegate_team", "finish": END},
    )
    builder.add_conditional_edges("delegate", after_delegate, {"review": "review", "finish": END})
    builder.add_conditional_edges(
        "delegate_team", after_delegate, {"review": "review", "finish": END}
    )
    builder.add_conditional_edges("review", after_review, {"wait": "await_review", "finish": END})
    builder.add_conditional_edges(
        "await_review", after_review, {"wait": "await_review", "finish": END}
    )
    return builder.compile(checkpointer=checkpointer)
