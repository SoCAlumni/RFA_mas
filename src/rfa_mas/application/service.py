from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any

from langgraph.types import Command

from rfa_mas.application.graphs.domain import InvocationContext
from rfa_mas.application.graphs.supervisor import build_supervisor_graph
from rfa_mas.application.observations import Observations, native_trace_guard, safe_error_code
from rfa_mas.application.sessions import SessionService
from rfa_mas.contracts import (
    AdapterInfo,
    DirectWorkRequest,
    PublicationStatus,
    ResumeRequest,
    RunResult,
    StructuredError,
    TrustedPrincipal,
    WorkRequest,
    WorkStatus,
    new_id,
    sha256_text,
)
from rfa_mas.errors import OutcomeUnknownError, PolicyDeniedError, ResourceNotFoundError, RfaError
from rfa_mas.ports import TracePort, WorkRepositoryPort


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
        self._dependencies = supervisor_dependencies
        self._graph = None
        self._guard_thread = guard_thread
        self._adapters = adapters
        self.observations = observations

    def start(self, checkpointer: Any) -> None:
        self._graph = build_supervisor_graph(self._dependencies, checkpointer=checkpointer)

    def stop(self) -> None:
        self._graph = None

    @staticmethod
    def _config(thread_id: str) -> dict[str, Any]:
        # Metadata/configurable scalar values can be persisted. No auth/context here.
        return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}

    @native_trace_guard
    async def run(self, request: WorkRequest, principal: TrustedPrincipal) -> RunResult:
        created_at = datetime.now(UTC)
        if not principal.authenticated:
            # Preserve the legacy policy-denied result, without creating durable
            # user state or performing any graph/tool action for anonymous input.
            return self._failed_result(request, created_at, PolicyDeniedError())
        if self._graph is None:
            raise RfaError("configuration_error", "checkpoint lifecycle 초기화가 필요합니다.")
        session_id = request.session_id if isinstance(request, DirectWorkRequest) else None
        task_id = request.task_id if isinstance(request, DirectWorkRequest) else None
        if session_id is not None:
            session = await self.sessions.get(session_id, principal)
            with self._guard_thread(session.thread_id):
                await self._repository.create_owned_run(
                    request, principal, session_id=session_id, task_id=task_id
                )
                return await self._execute(request, principal, session.thread_id, created_at)
        session = await self._repository.create_owned_run(
            request, principal, session_id=None, task_id=task_id
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
        """Owner-authorized error view; never persist its modified draft metadata."""
        await self._repository.get_owned_run(result.run_id, principal)
        return await self._project_error(result, principal, outward=True)

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
            snapshot = await self._graph.aget_state(self._config(record.thread_id))
            work = snapshot.values.get("work")
            if (
                not isinstance(work, WorkRequest)
                or work.run_id != record.run_id
                or snapshot.next != ("await_review",)
                or not snapshot.interrupts
                or record.status not in {WorkStatus.WAITING_APPROVAL, WorkStatus.RUNNING}
            ):
                raise RfaError("resume_unavailable", "이 실행은 승인 대기 재개 대상이 아닙니다.")
            try:
                # ID is already bound to this authorized run and its latest interrupt.
                state = await self._graph.ainvoke(
                    Command(resume={snapshot.interrupts[0].id: wakeup.model_dump(mode="json")}),
                    config=self._config(record.thread_id),
                    context=InvocationContext(principal),
                    durability="sync",
                )
                error = state.get("error")
                result = RunResult(
                    request_id=work.request_id,
                    trace_id=work.trace_id,
                    run_id=work.run_id,
                    agent_id=work.agent_id,
                    domain_id=state.get("domain_id"),
                    status=state["status"],
                    draft=state.get("draft"),
                    review=state.get("review"),
                    publication_status=state.get(
                        "publication_status", PublicationStatus.NOT_REQUESTED
                    ),
                    errors=(error,) if error else (),
                    stop_reason=self._stop_reason(state["status"], error),
                    simulated=any(item.simulated for item in self._adapters),
                    adapters=self._adapters,
                    created_at=record.created_at,
                    updated_at=datetime.now(UTC),
                )
            except RfaError as exc:
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

    async def get(self, run_id: str, principal: TrustedPrincipal) -> RunResult:
        record = await self._repository.get_owned_run(run_id, principal)
        if record.result is None:
            raise ResourceNotFoundError("run result")
        return await self.present_result(record.result, principal)

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
