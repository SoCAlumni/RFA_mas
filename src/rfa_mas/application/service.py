from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import is_dataclass, replace
from datetime import UTC, datetime
from typing import Any

from langgraph.types import Command

from rfa_mas.application.effects import EffectGuardedResponse
from rfa_mas.application.graphs.domain import InvocationContext
from rfa_mas.application.graphs.supervisor import build_supervisor_graph
from rfa_mas.application.observations import Observations, native_trace_guard, safe_error_code
from rfa_mas.application.sessions import SessionService
from rfa_mas.contracts import (
    AdapterInfo,
    DirectWorkRequest,
    DraftBundle,
    PublicationStatus,
    ResumeRequest,
    RunRecord,
    RunResult,
    SessionDetail,
    StructuredError,
    TrustedPrincipal,
    WorkRequest,
    WorkStatus,
    new_id,
    sha256_text,
)
from rfa_mas.errors import OutcomeUnknownError, PolicyDeniedError, ResourceNotFoundError, RfaError
from rfa_mas.ports import TracePort, WorkRepositoryPort
from rfa_mas.ports.interfaces import EffectRecord, Reconciliation, RetryLink

_TERMINAL = frozenset(
    {WorkStatus.COMPLETED, WorkStatus.FAILED, WorkStatus.CANCELLED, WorkStatus.OUTCOME_UNKNOWN}
)
_STOPPABLE = frozenset({WorkStatus.CREATED, WorkStatus.RUNNING, WorkStatus.WAITING_APPROVAL})


class WorkService:
    def __init__(
        self,
        *,
        repository: WorkRepositoryPort,
        trace: TracePort,
        supervisor_dependencies: Any,
        adapters: tuple[AdapterInfo, ...],
        guard_thread: Callable[[str], AbstractContextManager[None]],
        observations: Observations,
    ) -> None:
        self._repository = repository
        self.sessions = SessionService(repository)
        self._trace = trace
        self._dependencies = self._with_effects(supervisor_dependencies)
        self._graph = None
        self._guard_thread = guard_thread
        self._adapters = adapters
        self.observations = observations

    def start(self, checkpointer: Any) -> None:
        # Re-applied on start: a replaced Response port is guarded by the same ledger.
        self._dependencies = self._with_effects(self._dependencies)
        self._graph = build_supervisor_graph(self._dependencies, checkpointer=checkpointer)

    def stop(self) -> None:
        self._graph = None

    def _with_effects(self, deps: Any) -> Any:
        """P0-021: every review submission passes the durable effect ledger.

        Composition over the configured (observed) Response port. The graph reads only
        the ledger's barrier; it never receives a URL, credential or SQL handle.
        """
        ledger = self._repository if hasattr(self._repository, "begin_effect") else None
        if ledger is None or not is_dataclass(deps) or not hasattr(deps, "effects"):
            return deps
        response = deps.response
        if not isinstance(response, EffectGuardedResponse):
            response = EffectGuardedResponse(response, ledger)
        return replace(deps, response=response, effects=ledger)

    @staticmethod
    def _config(thread_id: str) -> dict[str, Any]:
        # Metadata/configurable scalar values can be persisted. No auth/context here.
        return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}

    @native_trace_guard
    async def run(
        self, request: WorkRequest, principal: TrustedPrincipal, *, retry_of: str | None = None
    ) -> RunResult:
        created_at = datetime.now(UTC)
        if not principal.authenticated:
            # Preserve the legacy policy-denied result, without creating durable
            # user state or performing any graph/tool action for anonymous input.
            return self._failed_result(request, created_at, PolicyDeniedError())
        if self._graph is None:
            raise RfaError("configuration_error", "checkpoint lifecycle 초기화가 필요합니다.")
        session_id = request.session_id if isinstance(request, DirectWorkRequest) else None
        task_id = request.task_id if isinstance(request, DirectWorkRequest) else None
        lineage = {"retry_of": retry_of} if retry_of is not None else {}
        if session_id is not None:
            session = await self.sessions.get(session_id, principal)
            with self._guard_thread(session.thread_id):
                await self._repository.create_owned_run(
                    request, principal, session_id=session_id, task_id=task_id, **lineage
                )
                return await self._execute(request, principal, session.thread_id, created_at)
        session = await self._repository.create_owned_run(
            request, principal, session_id=None, task_id=task_id, **lineage
        )
        with self._guard_thread(session.thread_id):
            return await self._execute(request, principal, session.thread_id, created_at)

    async def _execute(
        self,
        request: WorkRequest,
        principal: TrustedPrincipal,
        thread_id: str,
        created_at: datetime,
    ) -> RunResult:
        async with self.observations.scope(request.run_id, principal):
            await self.observations.record("request", "started", origin="service")
            result = await self._execute_inner(request, principal, thread_id, created_at)
            await self.observations.record(
                "stop",
                self._trace_status(result.status),
                origin="service",
                mode="mock" if result.simulated else "local",
                draft_id=result.draft.draft_id if result.draft else None,
            )
            return await self.present_result(result, principal)

    @staticmethod
    def _trace_status(status: WorkStatus) -> str:
        return {
            WorkStatus.COMPLETED: "succeeded",
            WorkStatus.WAITING_APPROVAL: "waiting",
            WorkStatus.CANCELLED: "cancelled",
            WorkStatus.OUTCOME_UNKNOWN: "outcome_unknown",
        }.get(status, "failed")

    async def _project_error(
        self, result: RunResult, principal: TrustedPrincipal, *, outward: bool = False
    ) -> RunResult:
        if not result.errors and result.status != WorkStatus.FAILED:
            return result
        context = await self._repository.observation_context(result.run_id, principal)
        errors = tuple(
            StructuredError(
                code=safe_error_code(error.code),
                message="작업을 안전하게 중단했습니다.",
                request_id=context.request_id,
                trace_id=context.trace_id,
                run_id=result.run_id,
                retryable=False,
            )
            for error in result.errors
        )
        # Preserve authorized partial output and its content binding; only the
        # error-facing correlation fields and free-text review reason are projected.
        correlation = dict(
            request_id=context.request_id, trace_id=context.trace_id, agent_id=context.agent_id
        )
        draft = result.draft
        if outward and draft is not None:
            draft = draft.model_copy(update=correlation)
        review = (
            result.review.model_copy(
                update={**correlation, "safe_reason": "검토 상태를 확인하세요."}
            )
            if outward and result.review
            else result.review
        )

        return result.model_copy(
            update=dict(
                request_id=context.request_id,
                trace_id=context.trace_id,
                agent_id="assistant-supervisor",
                errors=errors,
                draft=draft,
                review=review,
                stop_reason=errors[0].code if errors else "internal_error",
            )
        )

    async def present_result(self, result: RunResult, principal: TrustedPrincipal) -> RunResult:
        """Current-authorized view; never rewrite historical draft/approval storage."""
        await self._repository.get_owned_run(result.run_id, principal)
        if result.draft is not None:
            try:
                await self._dependencies.validate_resume(result.draft, principal)
            except RfaError as exc:
                if exc.code not in {"resume_review_required", "policy_denied"}:
                    raise
                result = result.model_copy(update={
                    "draft": None, "review": None, "stop_reason": exc.code,
                    "errors": (StructuredError(code=exc.code,
                        message="현재 자료 권한으로 결과를 표시할 수 없습니다.",
                        request_id=result.request_id,trace_id=result.trace_id,
                        run_id=result.run_id,retryable=False),),
                })
        return await self._project_error(result, principal, outward=True)

    async def present_session(self, session_id: str, principal: TrustedPrincipal) -> SessionDetail:
        detail = await self.sessions.get(session_id, principal)
        runs, results = [], {}
        for record in detail.runs:
            if record.result is None:
                runs.append(record)
                continue
            result = await self.present_result(record.result, principal)
            results[record.run_id] = result
            runs.append(record.model_copy(update={"result":result,
                "request_id":result.request_id,"trace_id":result.trace_id}))
        messages = []
        for message in detail.messages:
            if message.role == "assistant":
                result = results.get(message.run_id)
                content = (result.draft.content if result and result.draft
                           else "현재 자료 권한으로 이전 응답을 표시할 수 없습니다.")
                message = message.model_copy(update={"content":content})
            messages.append(message)
        return detail.model_copy(update={"runs":tuple(runs),"messages":tuple(messages)})

    async def _execute_inner(
        self,
        request: WorkRequest,
        principal: TrustedPrincipal,
        thread_id: str,
        created_at: datetime,
    ) -> RunResult:
        await self._repository.transition_run(request.run_id, WorkStatus.RUNNING)
        try:
            # The current worker payload is the frozen 1.0 WorkRequest. Keep the
            # session/task binding durably in the repository, and adapt only this
            # legacy graph boundary rather than weakening its strict DTO parser.
            graph_request = WorkRequest.model_validate(
                {name: getattr(request, name) for name in WorkRequest.model_fields}
                | {
                    "schema_version": "1.0",
                    # Runtime/Response reuse the work key. They must receive the
                    # same identity namespace as storage, not a global client key.
                    "idempotency_key": "owned:"
                    + sha256_text(json.dumps([principal.user_id, request.idempotency_key])),
                }
            )
            state = await self._graph.ainvoke(
                {
                    "work": graph_request,
                    "steps": 0,
                    "draft": None,
                    "review": None,
                    "error": None,
                    "domain_id": None,
                    "status": WorkStatus.RUNNING,
                    "publication_status": PublicationStatus.NOT_REQUESTED,
                    # Explicit 1.1 team intent only; frozen 1.0 work stays unchanged.
                    "team_request": request.team.model_dump(mode="json")
                    if isinstance(request, DirectWorkRequest) and request.team is not None
                    else None,
                    "task_id": request.task_id if isinstance(request, DirectWorkRequest) else None,
                    "team_result": None,
                },
                config=self._config(thread_id),
                context=InvocationContext(principal),
                durability="sync",
            )
            status = state.get("status", WorkStatus.FAILED)
            error = state.get("error")
            result = RunResult(
                request_id=request.request_id,
                trace_id=request.trace_id,
                run_id=request.run_id,
                agent_id=request.agent_id,
                domain_id=state.get("domain_id"),
                status=status,
                draft=state.get("draft"),
                review=state.get("review"),
                publication_status=state.get("publication_status", PublicationStatus.NOT_REQUESTED),
                errors=(error,) if error else (),
                stop_reason=self._stop_reason(status, error),
                simulated=any(item.simulated for item in self._adapters),
                adapters=self._adapters,
                created_at=created_at,
                updated_at=datetime.now(UTC),
            )
        except OutcomeUnknownError as exc:
            result = self._error_result(
                request,
                created_at,
                exc,
                status=WorkStatus.OUTCOME_UNKNOWN,
            )
        except RfaError as exc:
            result = self._failed_result(request, created_at, exc)
        except Exception:
            result = self._failed_result(
                request,
                created_at,
                RfaError(
                    "internal_error",
                    "예상하지 못한 내부 오류로 작업을 안전하게 종료했습니다.",
                ),
            )

        result = await self._project_error(result, principal)
        await self._repository.transition_run(request.run_id, result.status)
        await self._repository.save_result(result)
        await self._repository.save_checkpoint(
            request.run_id,
            "supervisor_final",
            {
                "status": result.status.value,
                "domain_id": result.domain_id.value if result.domain_id else None,
                "draft_id": result.draft.draft_id if result.draft else None,
                "draft_version": result.draft.version if result.draft else None,
                "evidence_count": len(result.draft.allowed_evidence) if result.draft else 0,
            },
        )
        return result

    @native_trace_guard
    async def resume(
        self, run_id: str, wakeup: ResumeRequest, principal: TrustedPrincipal
    ) -> RunResult:
        async with self.observations.scope(run_id, principal):
            await self.observations.record("request", "started", origin="service")
            result = await self._resume_inner(run_id, wakeup, principal)
            await self.observations.record(
                "stop",
                self._trace_status(result.status),
                origin="service",
                mode="mock" if result.simulated else "local",
                draft_id=result.draft.draft_id if result.draft else None,
            )
            return await self.present_result(result, principal)

    async def _resume_inner(
        self, run_id: str, wakeup: ResumeRequest, principal: TrustedPrincipal
    ) -> RunResult:
        # Authorize before looking up ANY checkpoint. Never accept client thread IDs.
        record = await self._repository.get_owned_run(run_id, principal)
        if self._graph is None:
            raise RfaError("configuration_error", "checkpoint lifecycle 초기화가 필요합니다.")
        with self._guard_thread(record.thread_id):
            record = await self._repository.get_owned_run(run_id, principal)
            if record.status in {WorkStatus.COMPLETED, WorkStatus.FAILED, WorkStatus.CANCELLED}:
                if record.result is None:
                    raise RfaError("resume_unavailable", "재개 가능한 결과가 없습니다.")
                return record.result
            barrier = await self._barrier(run_id, principal)
            if barrier is not None and record.status in _STOPPABLE:
                # Recorded while no invocation could stop the run (e.g. before a restart).
                return await self._stop_durably(record, principal, barrier)
            snapshot = await self._graph.aget_state(self._config(record.thread_id))
            work = snapshot.values.get("work")
            if (
                not isinstance(work, WorkRequest)
                or work.run_id != record.run_id
                or record.status not in {WorkStatus.WAITING_APPROVAL, WorkStatus.RUNNING}
            ):
                raise RfaError("resume_unavailable", "이 실행은 승인 대기 재개 대상이 아닙니다.")
            if snapshot.next == ("await_review",) and snapshot.interrupts:
                # ID is already bound to this authorized run and its latest interrupt.
                command: Command | None = Command(
                    resume={snapshot.interrupts[0].id: wakeup.model_dump(mode="json")}
                )
            elif record.status == WorkStatus.RUNNING and snapshot.next:
                # P0-021: the process stopped mid-graph. The checkpoint and the DB are not
                # atomic, so continue from the last saved node; effects already recorded in
                # the ledger are reused or reconciled by query there, never repeated.
                command = None
            elif record.status == WorkStatus.RUNNING and not snapshot.next:
                # The graph finished but the DB result was never written: finalize only.
                return await self._finalize(work, snapshot.values, record, principal)
            else:
                raise RfaError("resume_unavailable", "이 실행은 승인 대기 재개 대상이 아닙니다.")
            try:
                draft = snapshot.values.get("draft")
                if command is None and isinstance(draft, DraftBundle):
                    # Re-authorize the checkpointed draft before any later step.
                    await self._dependencies.validate_resume(draft, principal)
                state = await self._graph.ainvoke(
                    command,
                    config=self._config(record.thread_id),
                    context=InvocationContext(principal),
                    durability="sync",
                )
                result = self._state_result(work, state, record.created_at)
            except OutcomeUnknownError as exc:
                result = self._error_result(
                    work, record.created_at, exc, status=WorkStatus.OUTCOME_UNKNOWN
                )
            except RfaError as exc:
                if exc.code in {"resume_review_required", "policy_denied"}:
                    # Revoked access observed at resume: no later effect for this run.
                    await self._repository.set_run_barrier(run_id, principal, "permission_revoked")
                result = self._failed_result(work, record.created_at, exc)
            except Exception:
                result = self._failed_result(
                    work,
                    record.created_at,
                    RfaError("resume_error", "재개 중 오류가 발생하여 안전하게 중단했습니다."),
                )
            result = await self._project_error(result, principal)
            if result.status != record.status:
                await self._repository.transition_run(run_id, result.status)
            await self._repository.save_result(result)
            return result

    def _state_result(
        self, work: WorkRequest, state: dict[str, Any], created_at: datetime
    ) -> RunResult:
        status = state.get("status", WorkStatus.FAILED)
        error = state.get("error")
        return RunResult(
            request_id=work.request_id,
            trace_id=work.trace_id,
            run_id=work.run_id,
            agent_id=work.agent_id,
            domain_id=state.get("domain_id"),
            status=status,
            draft=state.get("draft"),
            review=state.get("review"),
            publication_status=state.get("publication_status", PublicationStatus.NOT_REQUESTED),
            errors=(error,) if error else (),
            stop_reason=self._stop_reason(status, error),
            simulated=any(item.simulated for item in self._adapters),
            adapters=self._adapters,
            created_at=created_at,
            updated_at=datetime.now(UTC),
        )

    async def _finalize(
        self, work: WorkRequest, values: dict[str, Any], record: RunRecord,
        principal: TrustedPrincipal,
    ) -> RunResult:
        result = await self._project_error(
            self._state_result(work, values, record.created_at), principal
        )
        if result.status != record.status:
            await self._repository.transition_run(record.run_id, result.status)
        await self._repository.save_result(result)
        return result

    async def _barrier(self, run_id: str, principal: TrustedPrincipal) -> str | None:
        reader = getattr(self._repository, "run_barrier", None)
        return await reader(run_id, principal) if reader is not None else None

    async def _stop_durably(
        self, record: RunRecord, principal: TrustedPrincipal, reason: str
    ) -> RunResult:
        """Terminal CANCELLED for a run that no invocation is executing (lock held)."""
        code = "cancelled" if reason == "cancelled" else "permission_revoked"
        error = StructuredError(
            code=code,
            retryable=False,
            message="실행을 중단했습니다.",
            request_id=record.request_id,
            trace_id=record.trace_id,
            run_id=record.run_id,
        )
        now = datetime.now(UTC)
        if record.result is not None:
            # Keep the historical draft/review mirror; only the lifecycle ends here.
            result = record.result.model_copy(
                update={"status": WorkStatus.CANCELLED, "errors": (error,),
                        "stop_reason": code, "updated_at": now}
            )
        else:
            result = RunResult(
                request_id=record.request_id,
                trace_id=record.trace_id,
                run_id=record.run_id,
                agent_id="assistant-supervisor",
                domain_id=record.domain_id,
                status=WorkStatus.CANCELLED,
                errors=(error,),
                stop_reason=code,
                simulated=any(item.simulated for item in self._adapters),
                adapters=self._adapters,
                created_at=record.created_at,
                updated_at=now,
            )
        result = await self._project_error(result, principal)
        await self._repository.transition_run(record.run_id, WorkStatus.CANCELLED)
        await self._repository.save_result(result)
        return result

    async def get(self, run_id: str, principal: TrustedPrincipal) -> RunResult:
        record = await self._repository.get_owned_run(run_id, principal)
        if record.result is None:
            raise ResourceNotFoundError("run result")
        return await self.present_result(record.result, principal)

    # -- P1-004 thin assistant execution boundary ---------------------------------------
    async def assist(self, request, principal: TrustedPrincipal, *, knowledge=None):
        """Route untrusted text deterministically, then run exactly one existing path.

        store/query never create a Task or team; task_run uses the explicit team path;
        channel ingress may only query or draft for a public target through this Supervisor.
        """
        from rfa_mas.application.graphs.supervisor import PATTERN_OUTPUTS, route_intent
        from rfa_mas.contracts import (
            Audience,
            AssistantRequest,
            AssistantResponse,
            DraftTarget,
            IntentDecision,
            KnowledgeWrite,
            TeamExecutionRequest,
        )

        request = AssistantRequest.model_validate(request.model_dump())
        decision = IntentDecision.model_validate(
            route_intent(request.text, domain_id=request.domain_id, task_id=request.task_id,
                         ingress=request.ingress)
        )
        if not decision.supported:
            return AssistantResponse(decision=decision, status="unsupported",
                                     stop_reason=decision.limitations[0] if decision.limitations
                                     else "unsupported", next_options=decision.next_options)
        if decision.intent == "store_note":
            if knowledge is None:
                raise RfaError("not_implemented", "노트 저장 경로가 구성되지 않았습니다.")
            digest = sha256_text(json.dumps([principal.user_id, request.text]))
            stored = await knowledge.write(
                KnowledgeWrite.model_validate({
                    "domain_id": decision.domain_id,
                    "provenance": {"provider": "note", "namespace": "assistant",
                                   "external_id": f"note-{digest[:32]}"},
                    "provider_revision": "1",
                    "title": " ".join(request.text.split())[:80],
                    "content": request.text,
                }),
                principal,
            )
            return AssistantResponse(decision=decision, status="stored",
                                     stored_source_id=stored.document.source_id,
                                     stored_revision=stored.document.source_revision)
        target = request.target
        if decision.intent == "external_draft" or request.ingress == "public":
            target = DraftTarget(audience=Audience.PUBLIC)
        team = None
        if decision.intent == "task_run" and decision.task_candidate is not None:
            pattern = decision.task_candidate.pattern
            goal = decision.task_candidate.goal
            if request.task_id is not None:
                # A follow-up Run keeps the durable Task goal/pattern; the new text is the
                # Run's query. Ownership is re-checked by the repository.
                existing = await self._repository.get_team_lifecycle(request.task_id, principal)
                goal = existing.task.goal
                pattern = existing.team.spec.template.pattern
            team = TeamExecutionRequest(goal=goal,
                                        outputs=PATTERN_OUTPUTS[pattern],
                                        requested_pattern=pattern)
        work = DirectWorkRequest(
            query=request.text,
            domain_id=decision.domain_id,
            target=target,
            session_id=request.session_id,
            task_id=request.task_id if team is not None else None,
            team=team,
        )
        result = await self.run(work, principal)
        return await self._assistant_result(decision, result, principal, team is not None)

    async def _assistant_result(self, decision, result: RunResult, principal, team_run: bool):
        from rfa_mas.contracts import AssistantResponse

        task_id = team_id = None
        partial = False
        if team_run:
            try:
                team = await self._repository.get_team_result(result.run_id, principal)
            except RfaError:
                team = None
            if team is not None:
                task_id, team_id = team.task_id, team.team_id
                partial = team.status == "partial"
        status = {
            WorkStatus.COMPLETED: "completed",
            WorkStatus.WAITING_APPROVAL: "waiting_approval",
            WorkStatus.CANCELLED: "cancelled",
        }.get(result.status, "partial" if partial else "failed")
        options = ()
        reason = result.stop_reason
        if status in {"failed", "partial"}:
            options = {
                "budget_exceeded": ("목표 범위를 좁히거나 허용 예산 안에서 다시 요청하세요.",),
                "budget_unavailable": ("사용량을 보고하는 모델 경로가 필요합니다.",),
                "retrieval_timeout": ("잠시 후 같은 요청을 다시 보내세요.",),
                "policy_denied": ("허용된 자료 범위에서 질문을 바꿔 다시 요청하세요.",),
            }.get(reason or "", ("요청을 확인한 뒤 다시 시도하세요.",))
        elif status == "cancelled":
            options = ("필요하면 새 실행으로 다시 요청하세요. 이미 발생한 결과는 되돌리지 않았습니다.",)
        return AssistantResponse(decision=decision, status=status, run=result, task_id=task_id,
                                 team_id=team_id, stop_reason=reason, partial=partial,
                                 next_options=options)


    async def team_result(self, run_id: str, principal: TrustedPrincipal):
        """Owner-authorized team receipt (roles, partial results, budget usage)."""
        await self._repository.get_owned_run(run_id, principal)
        result = await self._repository.get_team_result(run_id, principal)
        if result is None:
            raise ResourceNotFoundError("team result")
        return result

    async def cancel(self, run_id: str, principal: TrustedPrincipal) -> str:
        """Owner-authorized cancel: a durable barrier, then stop the run.

        Returns 'cancelling' when a live invocation was signalled (it stops at its next
        effect boundary) and 'cancelled' when the run was ended here. Effects that already
        happened are not undone, and a terminal run cannot be cancelled.
        """
        return await self._halt(run_id, principal, "cancelled")

    async def revoke(self, run_id: str, principal: TrustedPrincipal) -> str:
        """Owner revokes this run's delegated permission; no later effect may start.

        Works on terminal runs too: e.g. a completed run can no longer be published.
        """
        record = await self._repository.get_owned_run(run_id, principal)
        if record.status in _TERMINAL:
            await self._repository.set_run_barrier(run_id, principal, "permission_revoked")
            return "revoked"
        return await self._halt(run_id, principal, "permission_revoked")

    async def _halt(self, run_id: str, principal: TrustedPrincipal, reason: str) -> str:
        record = await self._repository.get_owned_run(run_id, principal)
        if record.status in _TERMINAL:
            raise RfaError("invalid_state_transition", "이미 종료된 실행입니다.")
        await self._repository.set_run_barrier(run_id, principal, reason)
        runner = getattr(self._dependencies, "team_runner", None)
        if runner is not None and await runner.cancel(run_id):
            return "cancelling"
        try:
            with self._guard_thread(record.thread_id):
                current = await self._repository.get_owned_run(run_id, principal)
                if current.status in _STOPPABLE:
                    await self._stop_durably(current, principal, reason)
        except RfaError as exc:
            if exc.code != "thread_busy":
                raise
            return "cancelling"  # The live invocation stops at its next effect boundary.
        return "cancelled" if reason == "cancelled" else "revoked"

    # -- P0-021 explicit retry, ledger view and reconciliation --------------------------
    async def retry(
        self, run_id: str, principal: TrustedPrincipal, *, idempotency_key: str
    ) -> RunResult:
        """Explicit owner retry of a FAILED/CANCELLED run: always a NEW run (retry_of).

        Unknown effects of the source are first reconciled by result query only; any that
        stay unknown remain visible on the source. Nothing is retried automatically: the
        owner's explicit request creates new effects under new keys.
        """
        source = await self._repository.get_owned_run(run_id, principal)
        if source.status not in {
            WorkStatus.FAILED, WorkStatus.CANCELLED, WorkStatus.OUTCOME_UNKNOWN
        }:
            raise RfaError(
                "invalid_state_transition", "종료된 실패·취소 실행만 재시도할 수 있습니다."
            )
        await self.reconcile(run_id, principal)
        stored = await self._repository.owned_run_request(run_id, principal)
        request = DirectWorkRequest(
            query=stored["query"],
            domain_id=stored.get("domain_id"),
            target=stored.get("target") or {"audience": "owner"},
            simulation_scenario=stored.get("simulation_scenario", "success"),
            idempotency_key=idempotency_key,
            session_id=source.session_id,
            task_id=source.task_id,
            team=stored.get("team"),
        )
        return await self.run(request, principal, retry_of=run_id)

    async def effects(self, run_id: str, principal: TrustedPrincipal) -> tuple[EffectRecord, ...]:
        return await self._repository.run_effects(run_id, principal)

    async def retry_links(self, run_id: str, principal: TrustedPrincipal) -> tuple[RetryLink, ...]:
        return await self._repository.retry_links(run_id, principal)

    async def reconcile(
        self, run_id: str, principal: TrustedPrincipal
    ) -> tuple[Reconciliation, ...]:
        """Resolve OUTCOME_UNKNOWN effects by result query only, never by re-execution.

        Review submissions are queried at the review authority. Publications are
        reconciled by DraftLifecycle.query (P1-005A). Local role/team operations have no
        cross-process status to query and stay unknown (explicit retry = new run).
        """
        response = self._dependencies.response
        results = []
        for effect in await self._repository.run_effects(run_id, principal):
            if effect.state != "outcome_unknown" or effect.kind == "publication":
                continue
            if effect.kind == "review_submission" and isinstance(response, EffectGuardedResponse):
                results.append(await response.reconcile(effect))
                continue
            results.append(
                Reconciliation(
                    operation_key=effect.operation_key,
                    kind=effect.kind,
                    before=effect.state,
                    after=effect.state,
                    method="unsupported",
                    found=False,
                )
            )
        return tuple(results)

    def _failed_result(
        self, request: WorkRequest, created_at: datetime, error: RfaError
    ) -> RunResult:
        return self._error_result(request, created_at, error, status=WorkStatus.FAILED)

    def _error_result(
        self,
        request: WorkRequest,
        created_at: datetime,
        error: RfaError,
        *,
        status: WorkStatus,
    ) -> RunResult:
        structured = StructuredError(
            code=safe_error_code(error.code),
            retryable=False,
            message="작업을 안전하게 중단했습니다.",
            request_id=new_id("error"),
            trace_id=new_id("error"),
            run_id=request.run_id,
        )
        return RunResult(
            request_id=structured.request_id,
            trace_id=structured.trace_id,
            run_id=request.run_id,
            agent_id="assistant-supervisor",
            status=status,
            errors=(structured,),
            stop_reason=structured.code,
            simulated=any(item.simulated for item in self._adapters),
            adapters=self._adapters,
            created_at=created_at,
            updated_at=datetime.now(UTC),
        )

    @staticmethod
    def _stop_reason(status: WorkStatus, error: StructuredError | None) -> str:
        if error is not None:
            return error.code
        if status == WorkStatus.WAITING_APPROVAL:
            return "review_pending_or_revision_required"
        if status == WorkStatus.COMPLETED:
            return "mock_review_completed_no_external_write"
        return status.value
