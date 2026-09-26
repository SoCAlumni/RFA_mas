from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from rfa_mas.application.graphs.supervisor import build_supervisor_graph
from rfa_mas.contracts import (
    AdapterInfo,
    PublicationStatus,
    RunResult,
    StructuredError,
    TrustedPrincipal,
    WorkRequest,
    WorkStatus,
)
from rfa_mas.errors import OutcomeUnknownError, ResourceNotFoundError, RfaError
from rfa_mas.ports import TracePort, WorkRepositoryPort


class WorkService:
    def __init__(
        self,
        *,
        repository: WorkRepositoryPort,
        trace: TracePort,
        supervisor_dependencies: Any,
        adapters: tuple[AdapterInfo, ...],
    ) -> None:
        self._repository = repository
        self._trace = trace
        self._graph = build_supervisor_graph(supervisor_dependencies)
        self._adapters = adapters

    async def run(self, request: WorkRequest, principal: TrustedPrincipal) -> RunResult:
        created_at = datetime.now(UTC)
        await self._repository.create_run(request)
        await self._repository.transition_run(request.run_id, WorkStatus.RUNNING)
        await self._trace.emit(
            event="work_started",
            request_id=request.request_id,
            trace_id=request.trace_id,
            run_id=request.run_id,
            status=WorkStatus.RUNNING.value,
            metadata={
                "requested_domain": request.domain_id.value if request.domain_id else None,
                "target_audience": request.target.audience.value,
                "scenario": request.simulation_scenario.value,
                "simulated": any(item.simulated for item in self._adapters),
                "adapters": [item.model_dump(mode="json") for item in self._adapters],
            },
        )
        try:
            state = await self._graph.ainvoke({"work": request, "principal": principal, "steps": 0})
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
        await self._trace.emit(
            event="work_finished",
            request_id=request.request_id,
            trace_id=request.trace_id,
            run_id=request.run_id,
            status=result.status.value,
            metadata={
                "domain_id": result.domain_id.value if result.domain_id else None,
                "draft_id": result.draft.draft_id if result.draft else None,
                "content_hash": result.draft.content_hash if result.draft else None,
                "review": result.review.decision.value if result.review else None,
                "publication_status": result.publication_status.value,
                "simulated": result.simulated,
                "adapters": [item.model_dump(mode="json") for item in result.adapters],
            },
        )
        return result

    async def get(self, run_id: str) -> RunResult:
        result = await self._repository.get_result(run_id)
        if result is None:
            raise ResourceNotFoundError(f"run:{run_id}")
        return result

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
            code=error.code,
            retryable=error.retryable,
            message=error.safe_message,
            request_id=request.request_id,
            trace_id=request.trace_id,
            run_id=request.run_id,
        )
        return RunResult(
            request_id=request.request_id,
            trace_id=request.trace_id,
            run_id=request.run_id,
            agent_id=request.agent_id,
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
