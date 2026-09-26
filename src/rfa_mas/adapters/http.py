from __future__ import annotations

import ipaddress
from typing import Any

import httpx
from pydantic import BaseModel, SecretStr, ValidationError

from rfa_mas.contracts import (
    AgentSpec,
    DraftBundle,
    PolicyDecision,
    PolicyRequest,
    PublicationStatus,
    ResultStatus,
    ReviewDecision,
    ReviewStatus,
    SimulationScenario,
    StructuredError,
    TaskRequest,
    TaskResult,
    ToolEffect,
    ToolRequest,
    ToolResult,
)
from rfa_mas.errors import BackendNotImplementedError, OutcomeUnknownError, RfaError


def require_loopback_reference_url(url: str, *, setting_name: str) -> None:
    """P0 HTTP contracts are executable only against a local fixture/service."""

    try:
        parsed = httpx.URL(url)
        host = parsed.host
        is_loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except (TypeError, ValueError):
        is_loopback = False
    if not is_loopback:
        raise BackendNotImplementedError(f"P0 non-loopback endpoint via {setting_name}")


def _validated_response[ContractT: BaseModel](
    model_type: type[ContractT], response: httpx.Response
) -> ContractT:
    try:
        return model_type.model_validate(response.json())
    except (ValueError, ValidationError):
        raise RfaError(
            "upstream_contract_error",
            "reference contract 응답이 합의된 DTO를 충족하지 않습니다.",
        ) from None


class ReferenceHttpClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        token: SecretStr | None,
        max_read_retries: int,
        endpoint_name: str = "REFERENCE_BASE_URL",
    ) -> None:
        require_loopback_reference_url(str(client.base_url), setting_name=endpoint_name)
        self.client = client
        self._token = token
        self._max_read_retries = max_read_retries

    def headers(self, *, idempotency_key: str | None = None) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token.get_secret_value()}"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        retry_read: bool = False,
    ) -> httpx.Response:
        attempts = self._max_read_retries + 1 if retry_read else 1
        for attempt in range(attempts):
            try:
                response = await self.client.request(
                    method,
                    path,
                    json=json_body,
                    headers=self.headers(idempotency_key=idempotency_key),
                )
                response.raise_for_status()
                return response
            except httpx.TimeoutException:
                if attempt + 1 >= attempts:
                    raise
            except httpx.TransportError:
                if attempt + 1 >= attempts:
                    raise RfaError(
                        "upstream_transport_error",
                        "reference contract 전송에 실패했습니다.",
                        retryable=retry_read,
                    ) from None
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code < 500 or attempt + 1 >= attempts:
                    raise RfaError(
                        "upstream_http_error",
                        f"reference contract가 HTTP {exc.response.status_code}를 반환했습니다.",
                        retryable=exc.response.status_code >= 500,
                    ) from None
        raise AssertionError("unreachable")


class ResponseHttpAdapter:
    adapter_name = "reference-http-response"

    def __init__(self, client: ReferenceHttpClient, *, simulated: bool = False) -> None:
        self._client = client
        self.simulated = simulated

    async def submit_draft(
        self,
        draft: DraftBundle,
        *,
        idempotency_key: str,
        simulation_scenario: SimulationScenario = SimulationScenario.SUCCESS,
    ) -> ReviewDecision:
        body = {
            "draft": draft.model_dump(mode="json"),
            "simulation_scenario": simulation_scenario.value,
        }
        try:
            response = await self._client.request(
                "POST",
                "/v1/reviews",
                json_body=body,
                idempotency_key=idempotency_key,
                retry_read=False,
            )
        except httpx.TimeoutException:
            return ReviewDecision(
                request_id=draft.request_id,
                trace_id=draft.trace_id,
                run_id=draft.run_id,
                agent_id=draft.agent_id,
                domain_id=draft.domain_id,
                draft_id=draft.draft_id,
                draft_version=draft.version,
                content_hash=draft.content_hash,
                target=draft.target,
                decision=ReviewStatus.PENDING,
                publication_status=PublicationStatus.NOT_REQUESTED,
                safe_reason=(
                    "timeout으로 검토 요청 결과를 확정할 수 없습니다. 자동 재시도하지 않습니다."
                ),
                simulated=self.simulated,
                adapter=self.adapter_name,
            )
        parsed = _validated_response(ReviewDecision, response)
        return parsed.model_copy(
            update={"simulated": self.simulated or parsed.simulated, "adapter": self.adapter_name}
        )

    async def get_decision(self, draft_id: str) -> ReviewDecision | None:
        try:
            response = await self._client.request("GET", f"/v1/reviews/{draft_id}", retry_read=True)
        except httpx.TimeoutException as exc:
            raise RfaError(
                "upstream_timeout",
                "검토 상태 조회 시간이 초과되었습니다.",
                retryable=True,
            ) from exc
        except RfaError as exc:
            if "HTTP 404" in exc.safe_message:
                return None
            raise
        parsed = _validated_response(ReviewDecision, response)
        return parsed.model_copy(
            update={"simulated": self.simulated or parsed.simulated, "adapter": self.adapter_name}
        )


class ToolHttpAdapter:
    adapter_name = "reference-http-tool"

    def __init__(self, client: ReferenceHttpClient, *, simulated: bool = False) -> None:
        self._client = client
        self.simulated = simulated

    async def execute(self, request: ToolRequest) -> ToolResult:
        if request.effect == ToolEffect.WRITE:
            return ToolResult(
                request_id=request.request_id,
                trace_id=request.trace_id,
                run_id=request.run_id,
                agent_id=request.agent_id,
                domain_id=request.domain_id,
                idempotency_key=request.idempotency_key,
                status=ResultStatus.DENIED,
                error=StructuredError(
                    code="external_writes_disabled",
                    retryable=False,
                    message="P0에서는 HTTP tool adapter도 외부 write를 실행할 수 없습니다.",
                    request_id=request.request_id,
                    trace_id=request.trace_id,
                    run_id=request.run_id,
                ),
                simulated=self.simulated,
                adapter=self.adapter_name,
            )
        try:
            response = await self._client.request(
                "POST",
                "/v1/tools/execute",
                json_body=request.model_dump(mode="json"),
                idempotency_key=request.idempotency_key,
                retry_read=request.effect == ToolEffect.READ,
            )
        except httpx.TimeoutException:
            status = (
                ResultStatus.OUTCOME_UNKNOWN
                if request.effect == ToolEffect.WRITE
                else ResultStatus.TIMED_OUT
            )
            return ToolResult(
                request_id=request.request_id,
                trace_id=request.trace_id,
                run_id=request.run_id,
                agent_id=request.agent_id,
                domain_id=request.domain_id,
                idempotency_key=request.idempotency_key,
                status=status,
                error=StructuredError(
                    code=status.value,
                    retryable=request.effect == ToolEffect.READ,
                    message=(
                        "write timeout으로 결과를 확정할 수 없습니다."
                        if request.effect == ToolEffect.WRITE
                        else "read timeout이 발생했습니다."
                    ),
                    request_id=request.request_id,
                    trace_id=request.trace_id,
                    run_id=request.run_id,
                ),
                simulated=self.simulated,
                adapter=self.adapter_name,
            )
        parsed = _validated_response(ToolResult, response)
        return parsed.model_copy(
            update={"simulated": self.simulated or parsed.simulated, "adapter": self.adapter_name}
        )


class RuntimeHttpAdapter:
    adapter_name = "reference-http-runtime"

    def __init__(self, client: ReferenceHttpClient, *, simulated: bool = False) -> None:
        self._client = client
        self.simulated = simulated

    async def run(self, spec: AgentSpec, request: TaskRequest) -> TaskResult:
        try:
            response = await self._client.request(
                "POST",
                "/v1/runtime/tasks",
                json_body={
                    "spec": spec.model_dump(mode="json"),
                    "request": request.model_dump(mode="json"),
                },
                idempotency_key=request.idempotency_key,
                retry_read=False,
            )
        except httpx.TimeoutException as exc:
            raise OutcomeUnknownError("runtime task 생성") from exc
        parsed = _validated_response(TaskResult, response)
        return parsed.model_copy(
            update={"simulated": self.simulated or parsed.simulated, "adapter": self.adapter_name}
        )

    async def status(self, run_id: str) -> TaskResult | None:
        try:
            response = await self._client.request(
                "GET", f"/v1/runtime/tasks/{run_id}", retry_read=True
            )
        except httpx.TimeoutException as exc:
            raise RfaError(
                "upstream_timeout",
                "runtime 상태 조회 시간이 초과되었습니다.",
                retryable=True,
            ) from exc
        except RfaError as exc:
            if "HTTP 404" in exc.safe_message:
                return None
            raise
        parsed = _validated_response(TaskResult, response)
        return parsed.model_copy(
            update={"simulated": self.simulated or parsed.simulated, "adapter": self.adapter_name}
        )

    async def cancel(self, run_id: str) -> TaskResult:
        try:
            response = await self._client.request(
                "POST", f"/v1/runtime/tasks/{run_id}/cancel", retry_read=False
            )
        except httpx.TimeoutException as exc:
            raise OutcomeUnknownError("runtime task 취소") from exc
        parsed = _validated_response(TaskResult, response)
        return parsed.model_copy(
            update={"simulated": self.simulated or parsed.simulated, "adapter": self.adapter_name}
        )


class PolicyHttpAdapter:
    adapter_name = "reference-http-policy"
    policy_version = "reference-http-v1"

    def __init__(self, client: ReferenceHttpClient, *, simulated: bool = False) -> None:
        self._client = client
        self.simulated = simulated

    async def evaluate(self, request: PolicyRequest) -> PolicyDecision:
        try:
            response = await self._client.request(
                "POST",
                "/v1/policy/decisions",
                json_body=request.model_dump(mode="json"),
                retry_read=True,
            )
        except httpx.TimeoutException as exc:
            raise RfaError(
                "upstream_timeout",
                "정책 조회 시간이 초과되었습니다.",
                retryable=True,
            ) from exc
        parsed = _validated_response(PolicyDecision, response)
        return parsed.model_copy(
            update={"simulated": self.simulated or parsed.simulated, "adapter": self.adapter_name}
        )
