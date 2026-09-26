from __future__ import annotations

import hmac
import json
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from rfa_mas.adapters.local import LocalPolicy
from rfa_mas.adapters.mock import MockResponse, MockTool
from rfa_mas.contracts import (
    AgentSpec,
    Audience,
    DraftBundle,
    EvidenceBundle,
    EvidenceItem,
    EvidenceRef,
    PolicyDecision,
    PolicyRequest,
    ResultStatus,
    ReviewDecision,
    ReviewStatus,
    SimulationScenario,
    SourceLocation,
    StructuredError,
    TaskRequest,
    TaskResult,
    ToolRequest,
    ToolResult,
    WorkRequest,
    sha256_text,
)
from rfa_mas.errors import RfaError


class ReviewSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft: DraftBundle
    simulation_scenario: SimulationScenario = SimulationScenario.SUCCESS


class RuntimeSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec: AgentSpec
    request: TaskRequest


class ReviewDecisionRequest(BaseModel):
    """Manual reviewer decision bound to the exact submitted version/hash (P1-008)."""

    model_config = ConfigDict(extra="forbid")

    draft_version: int
    content_hash: str
    decision: ReviewStatus


def create_reference_contract_app(
    *, service_token: SecretStr | None = None, manual_decisions: bool = False
) -> FastAPI:
    """Local fixture for our provisional contract, not a teammate service implementation.

    Defaults reproduce the frozen 1.0 fixture exactly. P1-008 opt-ins: service_token
    makes every route require that bearer token (synthetic test secret only), so a
    missing/forged identity proof is rejected before any handler runs; manual_decisions
    adds the reviewer decision route used to reproduce rejected/revision decisions.
    """

    app = FastAPI(title="RFA provisional teammate contract fixture", version="1.0")
    if service_token is not None:
        expected = f"Bearer {service_token.get_secret_value()}".encode()

        @app.middleware("http")
        async def authenticated(request: Request, call_next):
            supplied = request.headers.get("authorization", "").encode()
            if not hmac.compare_digest(supplied, expected):
                return JSONResponse({"detail": "authentication required"}, status_code=401)
            return await call_next(request)

    response_adapter = MockResponse()
    tool_adapter = MockTool()
    policy_adapter = LocalPolicy()
    runtime_results: dict[str, TaskResult] = {}
    runtime_idempotency: dict[str, tuple[str, TaskResult]] = {}

    def failed_runtime_task(
        request: TaskRequest, code: str, message: str, *, steps: int = 0
    ) -> TaskResult:
        return TaskResult(
            request_id=request.request_id,
            trace_id=request.trace_id,
            run_id=request.run_id,
            agent_id=request.agent_id,
            domain_id=request.domain_id,
            status=ResultStatus.FAILED,
            output={"steps": steps},
            error=StructuredError(
                code=code,
                message=message,
                retryable=False,
                request_id=request.request_id,
                trace_id=request.trace_id,
                run_id=request.run_id,
            ),
            simulated=True,
            adapter="reference-contract-runtime-fixture",
        )

    @app.post("/v1/reviews", response_model=ReviewDecision)
    async def submit_review(
        body: ReviewSubmission,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> ReviewDecision:
        if not idempotency_key:
            raise HTTPException(status_code=400, detail="Idempotency-Key required")
        return await response_adapter.submit_draft(
            body.draft,
            idempotency_key=idempotency_key,
            simulation_scenario=body.simulation_scenario,
        )

    @app.get("/v1/reviews/{draft_id}", response_model=ReviewDecision)
    async def get_review(draft_id: str) -> ReviewDecision:
        result = await response_adapter.get_decision(draft_id)
        if result is None:
            raise HTTPException(status_code=404, detail="review not found")
        return result

    if manual_decisions:

        @app.post("/v1/reviews/{draft_id}/decision", response_model=ReviewDecision)
        async def decide_review(draft_id: str, body: ReviewDecisionRequest) -> ReviewDecision:
            try:
                return await response_adapter.decide(
                    draft_id,
                    draft_version=body.draft_version,
                    content_hash=body.content_hash,
                    decision=body.decision,
                )
            except RfaError as exc:
                status = {"not_found": 404, "invalid_request": 422}.get(exc.code, 409)
                raise HTTPException(status_code=status, detail=exc.code) from None

    @app.post("/v1/tools/execute", response_model=ToolResult)
    async def execute_tool(request: ToolRequest) -> ToolResult:
        return await tool_adapter.execute(request)

    @app.post("/v1/policy/decisions", response_model=PolicyDecision)
    async def decide_policy(request: PolicyRequest) -> PolicyDecision:
        return await policy_adapter.evaluate(request)

    @app.post("/v1/runtime/tasks", response_model=TaskResult)
    async def run_task(body: RuntimeSubmission) -> TaskResult:
        fingerprint = sha256_text(
            json.dumps(
                body.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        if cached := runtime_idempotency.get(body.request.idempotency_key):
            cached_fingerprint, cached_result = cached
            if cached_fingerprint == fingerprint:
                return cached_result
            return failed_runtime_task(
                body.request,
                "idempotency_conflict",
                "같은 idempotency key로 다른 runtime 요청을 실행할 수 없습니다.",
            )
        if (
            body.spec.agent_id != body.request.agent_id
            or body.spec.domain_id != body.request.domain_id
        ):
            result = failed_runtime_task(
                body.request,
                "agent_spec_binding_mismatch",
                "AgentSpec과 TaskRequest의 agent 또는 domain 식별자가 일치하지 않습니다.",
            )
            runtime_results[body.request.run_id] = result
            runtime_idempotency[body.request.idempotency_key] = (fingerprint, result)
            return result
        try:
            work = WorkRequest.model_validate(body.request.payload["work_request"])
        except (KeyError, TypeError, ValidationError):
            result = failed_runtime_task(
                body.request,
                "invalid_task_payload",
                "reference runtime fixture에 유효한 work_request payload가 필요합니다.",
            )
            runtime_results[body.request.run_id] = result
            runtime_idempotency[body.request.idempotency_key] = (fingerprint, result)
            return result
        if (
            work.request_id != body.request.request_id
            or work.trace_id != body.request.trace_id
            or work.run_id != body.request.run_id
            or (work.domain_id is not None and work.domain_id != body.request.domain_id)
            or Audience.PUBLIC not in body.spec.allowed_audiences
        ):
            result = failed_runtime_task(
                body.request,
                "task_payload_binding_mismatch",
                "work_request payload가 task 식별자 또는 허용 범위와 일치하지 않습니다.",
            )
            runtime_results[body.request.run_id] = result
            runtime_idempotency[body.request.idempotency_key] = (fingerprint, result)
            return result
        if body.spec.max_steps < 3:
            result = failed_runtime_task(
                body.request,
                "budget_exceeded",
                "reference runtime fixture의 domain task step 예산이 부족합니다.",
                steps=body.spec.max_steps,
            )
            runtime_results[body.request.run_id] = result
            runtime_idempotency[body.request.idempotency_key] = (fingerprint, result)
            return result

        location = SourceLocation(
            uri="fixture://reference-contract/public-evidence",
            section="synthetic-public-evidence",
        )
        evidence_text = "이 근거는 HTTP runtime reference contract 검증용 합성 공개 자료입니다."
        evidence_hash = sha256_text(evidence_text)
        evidence_item = EvidenceItem(
            source_id="reference-public-evidence",
            source_revision="1",
            location=location,
            audience=Audience.PUBLIC,
            excerpt=evidence_text,
            content_hash=evidence_hash,
            policy_version="reference-fixture-v1",
        )
        evidence = EvidenceBundle(
            request_id=body.request.request_id,
            trace_id=body.request.trace_id,
            run_id=body.request.run_id,
            agent_id=body.request.agent_id,
            domain_id=body.request.domain_id,
            items=(evidence_item,),
            policy_version="reference-fixture-v1",
            simulated=True,
            adapter="reference-contract-runtime-fixture",
        )
        content = (
            "HTTP runtime reference contract가 합성 공개 근거로 생성한 모의 DRAFT입니다. "
            "실제 NVIDIA 모델 또는 외부 서비스를 호출하지 않았습니다."
        )
        draft = DraftBundle(
            request_id=body.request.request_id,
            trace_id=body.request.trace_id,
            run_id=body.request.run_id,
            agent_id=body.request.agent_id,
            domain_id=body.request.domain_id,
            content_hash=sha256_text(content),
            target=work.target,
            audience=work.target.audience,
            policy_version="reference-fixture-v1",
            allowed_evidence=(
                EvidenceRef(
                    source_id=evidence_item.source_id,
                    source_revision=evidence_item.source_revision,
                    location=evidence_item.location,
                    audience=evidence_item.audience,
                    content_hash=evidence_item.content_hash,
                ),
            ),
            content=content,
            simulated=True,
            adapter="reference-contract-runtime-fixture",
        )
        result = TaskResult(
            request_id=body.request.request_id,
            trace_id=body.request.trace_id,
            run_id=body.request.run_id,
            agent_id=body.request.agent_id,
            domain_id=body.request.domain_id,
            status=ResultStatus.SUCCEEDED,
            output={
                "draft": draft.model_dump(mode="json"),
                "evidence": evidence.model_dump(mode="json"),
                "steps": 3,
            },
            simulated=True,
            adapter="reference-contract-runtime-fixture",
        )
        runtime_results[body.request.run_id] = result
        runtime_idempotency[body.request.idempotency_key] = (fingerprint, result)
        return result

    @app.get("/v1/runtime/tasks/{run_id}", response_model=TaskResult)
    async def task_status(run_id: str) -> TaskResult:
        if run_id not in runtime_results:
            raise HTTPException(status_code=404, detail="task not found")
        return runtime_results[run_id]

    @app.post("/v1/runtime/tasks/{run_id}/cancel", response_model=TaskResult)
    async def cancel_task(run_id: str) -> TaskResult:
        existing = runtime_results.get(run_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="task not found")
        cancelled = existing.model_copy(
            update={
                "status": ResultStatus.FAILED,
                "error": StructuredError(
                    code="cancelled",
                    message="reference fixture에서 task가 취소되었습니다.",
                    retryable=False,
                    request_id=existing.request_id,
                    trace_id=existing.trace_id,
                    run_id=existing.run_id,
                ),
            }
        )
        runtime_results[run_id] = cancelled
        return cancelled

    return app
