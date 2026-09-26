from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import Any

from rfa_mas.contracts import (
    Audience,
    DraftBundle,
    DraftBinding,
    EvaluationCase,
    EvidenceBundle,
    EvidenceItem,
    ExecutionMode,
    JudgeAssessment,
    JudgeDimensions,
    ModelRequest,
    ModelResult,
    PublicationReceipt,
    PublicationStatus,
    ResultStatus,
    RetrievalRequest,
    ReviewDecision,
    ReviewStatus,
    RunResult,
    SimulationScenario,
    StructuredError,
    ToolEffect,
    ToolRequest,
    ToolResult,
    new_id,
    sha256_text,
)
from rfa_mas.errors import OutcomeUnknownError, RfaError
from rfa_mas.adapters.retrieval import LocalRetrieval
from rfa_mas.ports import WorkRepositoryPort

TOKEN_PATTERN = re.compile(r"[0-9A-Za-z가-힣_]{2,}")


def _canonical_fingerprint(value: dict[str, Any]) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class MockModel:
    adapter_name = "mock-model"
    simulated = True

    async def generate(self, request: ModelRequest) -> ModelResult:
        if request.evidence.insufficient or not request.evidence.items:
            content = (
                "요청을 뒷받침할 허용된 근거가 부족합니다. "
                "추가 자료를 제공하거나 공개 범위를 확인해 주세요."
            )
        else:
            request_summary = (
                "공개 대상에 맞춰 허용된 근거만 요약합니다."
                if request.target.audience == Audience.PUBLIC
                else request.query
            )
            lines = [f"요청 요약: {request_summary}", "", "허용된 근거:"]
            for item in request.evidence.items:
                location = item.location.section or item.location.uri
                lines.append(
                    f"- {item.excerpt} [{item.source_id}@{item.source_revision} / {location}]"
                )
            lines.extend(
                [
                    "",
                    "이 초안은 합성/공개 fixture와 결정적 mock 모델로 생성되었습니다.",
                ]
            )
            content = "\n".join(lines)
        return ModelResult(content=content, simulated=True, adapter=self.adapter_name)


class MockRetrieval:
    adapter_name = "mock-retrieval"
    simulated = True

    def __init__(self, repository: WorkRepositoryPort, *, policy_version: str) -> None:
        self._repository = repository
        self._policy_version = policy_version

    async def search(self, request: RetrievalRequest) -> EvidenceBundle:
        if request.simulation_scenario == SimulationScenario.TIMEOUT:
            raise TimeoutError("simulated retrieval timeout")
        if request.simulation_scenario == SimulationScenario.INSUFFICIENT_EVIDENCE:
            return self._bundle(request, ())

        actual = await LocalRetrieval(self._repository, policy_version=self._policy_version).search(request)
        return self._bundle(request, actual.items)

    def _is_accessible(self, document: Any, request: RetrievalRequest) -> bool:
        if document.audience not in request.allowed_audiences:
            return False
        principal = request.principal
        if document.audience == Audience.PUBLIC:
            return True
        if document.audience == Audience.COMPANY:
            return bool(
                principal.authenticated
                and principal.company_id
                and document.company_id
                and document.company_id == principal.company_id
            )
        if document.audience == Audience.BUSINESS_UNIT:
            return bool(
                principal.authenticated
                and document.company_id
                and document.company_id == principal.company_id
                and set(document.required_memberships).intersection(principal.business_units)
            )
        if document.audience in {Audience.OWNER, Audience.PRIVATE}:
            return bool(
                principal.authenticated
                and document.owner_id
                and document.owner_id == principal.user_id
            )
        return False

    def _bundle(self, request: RetrievalRequest, items: tuple[EvidenceItem, ...]) -> EvidenceBundle:
        return EvidenceBundle(
            request_id=request.request_id,
            trace_id=request.trace_id,
            run_id=request.run_id,
            agent_id=request.agent_id,
            domain_id=request.domain_id,
            items=items,
            insufficient=not items,
            policy_version=self._policy_version,
            simulated=True,
            adapter=self.adapter_name,
        )


class MockResponse:
    adapter_name = "mock-response"
    simulated = True

    def __init__(self) -> None:
        self._by_idempotency: dict[str, tuple[str, ReviewDecision]] = {}
        self._by_draft: dict[str, ReviewDecision] = {}
        self._lock = asyncio.Lock()

    async def submit_draft(
        self,
        draft: DraftBundle,
        *,
        idempotency_key: str,
        simulation_scenario: SimulationScenario = SimulationScenario.SUCCESS,
    ) -> ReviewDecision:
        fingerprint = _canonical_fingerprint(
            {
                "draft": draft.model_dump(mode="json"),
                "simulation_scenario": simulation_scenario.value,
            }
        )
        async with self._lock:
            cached = self._by_idempotency.get(idempotency_key)
            if cached is not None:
                cached_fingerprint, cached_result = cached
                if cached_fingerprint != fingerprint:
                    raise RfaError(
                        "idempotency_conflict",
                        "같은 idempotency key로 다른 초안 승인 요청을 보낼 수 없습니다.",
                    )
                return cached_result

            if simulation_scenario == SimulationScenario.TIMEOUT:
                decision = ReviewStatus.PENDING
                publication_status = PublicationStatus.NOT_REQUESTED
                reason = (
                    "검토 요청 timeout으로 승인 결과를 확정할 수 없습니다. "
                    "외부 게시 요청은 실행되지 않았습니다."
                )
            elif simulation_scenario in {
                SimulationScenario.REVISION_REQUESTED,
                SimulationScenario.INSUFFICIENT_EVIDENCE,
            }:
                decision = ReviewStatus.REVISION_REQUESTED
                publication_status = PublicationStatus.NOT_REQUESTED
                reason = "허용된 근거와 초안 내용을 보완해야 합니다."
            else:
                decision = ReviewStatus.APPROVED
                publication_status = PublicationStatus.NOT_REQUESTED
                reason = (
                    "P0 mock 검토가 초안 버전과 hash를 확인했습니다. 외부 게시 권한은 없습니다."
                )

            result = ReviewDecision(
                request_id=draft.request_id,
                trace_id=draft.trace_id,
                run_id=draft.run_id,
                agent_id=draft.agent_id,
                domain_id=draft.domain_id,
                draft_id=draft.draft_id,
                draft_version=draft.version,
                content_hash=draft.content_hash,
                target=draft.target,
                decision=decision,
                publication_status=publication_status,
                safe_reason=reason,
                simulated=True,
                adapter=self.adapter_name,
            )
            self._by_idempotency[idempotency_key] = (fingerprint, result)
            self._by_draft[draft.draft_id] = result
            return result

    async def get_decision(self, draft_id: str) -> ReviewDecision | None:
        return self._by_draft.get(draft_id)

    async def decide(
        self,
        draft_id: str,
        *,
        draft_version: int,
        content_hash: str,
        decision: ReviewStatus,
    ) -> ReviewDecision:
        """Manual reviewer decision (P1-008 parity with the local stand-in).

        Bound to the latest submitted version/hash; a stale or changed draft is rejected.
        Only the review authority stand-in changes a decision; callers never self-approve.
        """
        if decision == ReviewStatus.PENDING:
            raise RfaError("invalid_request", "대기 상태는 결정이 아닙니다.")
        async with self._lock:
            current = self._by_draft.get(draft_id)
            if current is None:
                raise RfaError("not_found", "검토 요청을 찾을 수 없습니다.")
            if (current.draft_version, current.content_hash) != (draft_version, content_hash):
                raise RfaError("approval_binding_mismatch", "최신 초안 버전과 결정이 다릅니다.")
            updated = current.model_copy(
                update={"decision": decision, "safe_reason": "수동 검토 결정(모의)입니다."}
            )
            self._by_draft[draft_id] = updated
            return updated


class MockPublisher:
    """P1-005A in-process publication stand-in: no network, no external write.

    It keeps an in-memory sink of published payload bindings keyed by the owned
    idempotency key, so a retried/queried publication never produces a second effect.
    `lose_ack` keys simulate an effect whose acknowledgement was lost (outcome_unknown);
    `fail` keys simulate a definite rejection. Receipts are always mode=mock.
    """

    adapter_name = "mock-publisher"
    simulated = True
    mode = ExecutionMode.MOCK

    def __init__(
        self, *, lose_ack: frozenset[str] = frozenset(), fail: frozenset[str] = frozenset()
    ) -> None:
        self.sink: list[dict[str, Any]] = []
        self.calls = 0
        self._receipts: dict[str, tuple[str, PublicationReceipt]] = {}
        self._lose_ack = set(lose_ack)
        self._fail = set(fail)
        self._lock = asyncio.Lock()

    async def authorize(self, binding: DraftBinding) -> None:
        """P1-008E: the core review is this mock authority's approval; nothing to prove."""
        return None

    async def publish(
        self,
        binding: DraftBinding,
        *,
        run_id: str,
        publication_id: str,
        approval_id: str,
        idempotency_key: str,
    ) -> PublicationReceipt:
        fingerprint = _canonical_fingerprint(binding.model_dump(mode="json"))
        async with self._lock:
            self.calls += 1
            cached = self._receipts.get(idempotency_key)
            if cached is not None:
                if cached[0] != fingerprint:
                    raise RfaError(
                        "idempotency_conflict", "같은 key로 다른 게시 내용을 보낼 수 없습니다."
                    )
                return cached[1]
            if idempotency_key in self._fail:
                raise RfaError("publication_rejected", "모의 게시 대상이 요청을 거절했습니다.")
            receipt = PublicationReceipt(
                publication_id=publication_id,
                run_id=run_id,
                idempotency_key=idempotency_key,
                binding=binding,
                approval_id=approval_id,
                status=PublicationStatus.SUCCEEDED,
                external_result_ref="local-artifact:" + new_id("mockpub"),
                mode=ExecutionMode.MOCK,
                next_action="none",
            )
            self._receipts[idempotency_key] = (fingerprint, receipt)
            self.sink.append(
                {
                    "idempotency_key": idempotency_key,
                    "draft_id": binding.draft_id,
                    "version": binding.version,
                    "payload_hash": binding.payload_hash,
                }
            )
            if idempotency_key in self._lose_ack:
                self._lose_ack.discard(idempotency_key)
                raise OutcomeUnknownError("mock publication acknowledgement")
            return receipt

    async def query(self, idempotency_key: str) -> PublicationReceipt | None:
        async with self._lock:
            cached = self._receipts.get(idempotency_key)
            return cached[1] if cached else None


class MockTool:
    adapter_name = "mock-tool"
    simulated = True

    def __init__(self) -> None:
        self._idempotency: dict[str, tuple[str, ToolResult]] = {}
        self._lock = asyncio.Lock()

    async def execute(self, request: ToolRequest) -> ToolResult:
        fingerprint = _canonical_fingerprint(request.model_dump(mode="json"))
        async with self._lock:
            cached = self._idempotency.get(request.idempotency_key)
            if cached is not None:
                cached_fingerprint, cached_result = cached
                if cached_fingerprint != fingerprint:
                    raise RfaError(
                        "idempotency_conflict",
                        "같은 idempotency key로 다른 tool 요청을 실행할 수 없습니다.",
                    )
                return cached_result

            simulate = str(request.arguments.get("simulate", ""))
            if simulate == "timeout" and request.effect == ToolEffect.WRITE:
                status = ResultStatus.OUTCOME_UNKNOWN
                error = StructuredError(
                    code="outcome_unknown",
                    message="write timeout으로 실행 결과를 확정할 수 없습니다.",
                    retryable=False,
                    request_id=request.request_id,
                    trace_id=request.trace_id,
                    run_id=request.run_id,
                )
                output: dict[str, Any] = {}
            elif request.effect == ToolEffect.WRITE:
                status = ResultStatus.DENIED
                error = StructuredError(
                    code="external_writes_disabled",
                    message="P0에서는 외부 write를 실행할 수 없습니다.",
                    retryable=False,
                    request_id=request.request_id,
                    trace_id=request.trace_id,
                    run_id=request.run_id,
                )
                output = {}
            elif simulate == "timeout":
                status = ResultStatus.TIMED_OUT
                error = StructuredError(
                    code="read_timeout",
                    message="mock read 시간이 초과되었습니다.",
                    retryable=True,
                    request_id=request.request_id,
                    trace_id=request.trace_id,
                    run_id=request.run_id,
                )
                output = {}
            else:
                status = ResultStatus.SUCCEEDED
                error = None
                output = {"echo": request.arguments, "note": "simulated read-only tool result"}
            result = ToolResult(
                request_id=request.request_id,
                trace_id=request.trace_id,
                run_id=request.run_id,
                agent_id=request.agent_id,
                domain_id=request.domain_id,
                idempotency_key=request.idempotency_key,
                status=status,
                output=output,
                error=error,
                simulated=True,
                adapter=self.adapter_name,
            )
            self._idempotency[request.idempotency_key] = (fingerprint, result)
            return result


class MockJudge:
    adapter_name = "mock-judge"
    simulated = True

    async def evaluate(self, case: EvaluationCase, result: RunResult) -> JudgeAssessment:
        draft = result.draft
        content = draft.content if draft else ""
        evidence_faithfulness = (
            1.0
            if draft and draft.allowed_evidence
            else 0.5
            if draft and "근거가 부족" in content
            else 0.0
        )
        question_resolution = 1.0 if draft and case.input in content else 0.5 if draft else 0.0
        task_candidate_usefulness = 0.75 if draft and len(content) >= 40 else 0.25 if draft else 0.0
        dimensions = JudgeDimensions(
            evidence_faithfulness=evidence_faithfulness,
            question_resolution=question_resolution,
            task_candidate_usefulness=task_candidate_usefulness,
        )
        score = (
            sum(
                (
                    dimensions.evidence_faithfulness,
                    dimensions.question_resolution,
                    dimensions.task_candidate_usefulness,
                )
            )
            / 3
        )
        return JudgeAssessment(
            kind="mock",
            score=score,
            reason=(
                "결정적 mock이 근거 충실도, 질문 해결도, 작업 후보 유용성만 "
                "보조 평가했습니다. 권한·개인정보 판정에는 사용되지 않습니다."
            ),
            dimensions=dimensions,
            simulated=True,
            adapter=self.adapter_name,
        )
