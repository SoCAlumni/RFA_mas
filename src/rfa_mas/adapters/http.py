from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import quote

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr, ValidationError

from rfa_mas.contracts import (
    AgentSpec,
    ApprovalReference,
    DraftBinding,
    DraftBundle,
    DraftBundleV11,
    DraftTarget,
    ExecutionMode,
    PolicyDecision,
    PolicyRequest,
    PublicationReceipt,
    PublicationStatus,
    ResultStatus,
    ReviewDecision,
    ReviewStatus,
    SimulationScenario,
    SourceRevisionRef,
    StructuredError,
    TaskRequest,
    TaskResult,
    TeamInstance,
    TeamSpec,
    ToolEffect,
    ToolRequest,
    ToolResult,
    sha256_text,
)
from rfa_mas.errors import BackendNotImplementedError, OutcomeUnknownError, RfaError

# Contract versions this consumer understands. Anything else is an explicit error,
# never a best-effort parse or a silent 1.0 fallback.
SUPPORTED_CONTRACT_VERSIONS = frozenset({"1.0", "1.1"})


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
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict) and payload.get("schema_version", "1.0") not in (
        SUPPORTED_CONTRACT_VERSIONS
    ):
        raise RfaError(
            "unsupported_contract_version", "상대 서비스의 계약 버전을 지원하지 않습니다."
        )
    try:
        return model_type.model_validate(payload)
    except (ValueError, ValidationError):
        raise RfaError(
            "upstream_contract_error",
            "reference contract 응답이 합의된 DTO를 충족하지 않습니다.",
        ) from None


def _path(value: str) -> str:
    """One opaque path segment; never lets an ID add path/query structure."""
    return quote(value, safe="")


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

    # -- 1.1 team lifecycle (P1-008): side-effect calls, never retried on timeout --------
    async def prepare(self, spec: TeamSpec, *, idempotency_key: str) -> TeamInstance:
        """POST the exact TeamSpec with the owned idempotency key.

        A timeout may have provisioned members, so it is outcome_unknown (the caller
        keeps the durable slot occupied), never a failure and never an automatic retry.
        """
        try:
            response = await self._client.request(
                "POST",
                "/v1/runtime/teams",
                json_body={"spec": spec.model_dump(mode="json")},
                idempotency_key=idempotency_key,
                retry_read=False,
            )
        except httpx.TimeoutException as exc:
            raise OutcomeUnknownError("runtime team 준비") from exc
        return _validated_response(TeamInstance, response)

    async def cleanup(self, team_id: str, *, idempotency_key: str) -> TeamInstance:
        try:
            response = await self._client.request(
                "POST",
                f"/v1/runtime/teams/{_path(team_id)}/cleanup",
                idempotency_key=idempotency_key,
                retry_read=False,
            )
        except httpx.TimeoutException as exc:
            raise OutcomeUnknownError("runtime team 정리") from exc
        return _validated_response(TeamInstance, response)


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



# -- P1-008: publication through the P1-008C local stand-in (mode=mock receipts only) ------


def core_payload_hash(
    *,
    draft_id: str,
    version: int,
    content: str,
    content_hash: str,
    attachments: tuple[dict[str, Any], ...],
    target: DraftTarget,
    policy_version: str,
    sources: tuple[Any, ...],
) -> str:
    """The core DraftBinding payload hash (same fields/encoding as application/drafts.py).

    The consumer recomputes it from what the authority actually approved, so a stand-in
    approval can never be reused for other content, attachments, target, policy or sources.
    """
    payload = {
        "draft_id": draft_id,
        "version": version,
        "content": content,
        "content_hash": content_hash,
        "attachments": list(attachments),
        "target": target.model_dump(mode="json"),
        "policy_version": policy_version,
        "sources": [item.model_dump(mode="json") for item in sources],
    }
    return sha256_text(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    )


def local_review_draft(draft: DraftBundle, *, policy_decision_id: str) -> DraftBundleV11:
    """Mirror a core DRAFT into the stand-in's 1.1 review envelope (no attachments).

    Sources are derived exactly like the core binding (local KB: ACL revision = source
    revision), so the stand-in approval binding is comparable with the core binding.
    """
    sources = [
        SourceRevisionRef(
            **ref.model_dump(exclude={"schema_version"}),
            acl_revision=ref.source_revision,
            policy_version=draft.policy_version,
        ).model_dump(mode="json")
        for ref in draft.allowed_evidence
    ]
    data = draft.model_dump(mode="json", exclude={"schema_version"}) | {
        "attachments": [],
        "sources": sources,
        "policy_decision_id": policy_decision_id,
    }
    included = (
        "draft_id", "version", "content", "content_hash", "attachments", "target",
        "policy_version", "policy_decision_id", "sources",
    )
    payload_hash = sha256_text(
        json.dumps({key: data[key] for key in included}, sort_keys=True, ensure_ascii=False,
                   separators=(",", ":"))
    )
    return DraftBundleV11.model_validate(data | {"payload_hash": payload_hash})


class _StandInReview(BaseModel):
    """Consumer-side view of the stand-in review state (additive fields ignored)."""

    model_config = ConfigDict(extra="ignore")

    contract: Literal["1.0", "1.1"]
    draft_id: str
    version: int
    run_id: str
    content: str
    content_hash: str
    target: DraftTarget
    decision: ReviewStatus
    approval: ApprovalReference | None = None
    superseded: bool


class _StandInPublication(BaseModel):
    model_config = ConfigDict(extra="ignore")

    receipt: PublicationReceipt
    external_write_performed: Literal[False]
    simulated: Literal[True]


class PublicationHttpAdapter:
    """DraftLifecycle Publisher over the P1-008C stand-in (/v1/local/publications).

    Receipts are synthetic local-artifact references with mode=mock: never a real
    publication or share permission. Before the single POST it proves, from the stand-in's
    own ApprovalReference, that the approval is APPROVED, unexpired, from the expected
    approver and bound to exactly this draft/version/content/target/policy/sources (and no
    attachments). A timeout, transport failure or 5xx after dispatch is outcome_unknown.
    The stand-in has no lookup by idempotency key, so a result is reconciled only from a
    receipt reference seen by this process; otherwise it stays unknown (never re-POSTed).
    """

    adapter_name = "reference-http-publisher"
    simulated = True
    mode = ExecutionMode.MOCK

    def __init__(
        self,
        client: ReferenceHttpClient,
        *,
        expected_approver: Callable[[], Awaitable[str | None]] | None = None,
        simulate_outcome: Literal["succeeded", "outcome_unknown"] = "succeeded",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client
        self._expected_approver = expected_approver
        self._simulate = simulate_outcome
        self._clock = clock
        # Process-local optimization only: owned key -> (stand-in receipt id, core context).
        self._seen: dict[str, tuple[str, dict[str, Any]]] = {}
        self.posts = 0

    async def _approval(self, binding: DraftBinding) -> ApprovalReference:
        try:
            response = await self._client.request(
                "GET", f"/v1/local/reviews/{_path(binding.draft_id)}", retry_read=True
            )
        except httpx.TimeoutException:
            raise RfaError("upstream_timeout", "승인 조회 시간이 초과되었습니다.") from None
        except RfaError as exc:
            if "HTTP 404" in exc.safe_message:
                raise RfaError("approval_required", "게시 서비스에 승인 기록이 없습니다.") from None
            raise
        view = _validated_response(_StandInReview, response)
        approval = view.approval
        if view.contract != "1.1" or approval is None or view.superseded:
            raise RfaError("approval_required", "게시 서비스의 1.1 승인 참조가 없습니다.")
        if approval.decision != ReviewStatus.APPROVED or approval.mode != ExecutionMode.MOCK:
            raise RfaError("approval_required", "게시 서비스의 승인이 없습니다.")
        now = self._clock()
        if not approval.issued_at <= now < approval.expires_at:
            raise RfaError("approval_expired", "게시 승인이 만료되었습니다.")
        if (
            self._expected_approver is not None
            and approval.approver_id != await self._expected_approver()
        ):
            raise RfaError("approval_binding_mismatch", "승인자가 일치하지 않습니다.")
        approved = approval.binding
        recomputed = core_payload_hash(
            draft_id=view.draft_id,
            version=view.version,
            content=view.content,
            content_hash=view.content_hash,
            attachments=(),
            target=view.target,
            policy_version=approved.policy_version,
            sources=approved.sources,
        )
        if (
            (approved.draft_id, approved.version, approved.content_hash, approved.target)
            != (binding.draft_id, binding.version, binding.content_hash, binding.target)
            or (approved.policy_version, approved.sources)
            != (binding.policy_version, binding.sources)
            or (view.draft_id, view.version) != (binding.draft_id, binding.version)
            or sha256_text(view.content) != binding.content_hash
            or recomputed != binding.payload_hash
        ):
            raise RfaError("approval_binding_mismatch", "승인된 초안과 게시 내용이 다릅니다.")
        return approval

    def _core(
        self, remote: PublicationReceipt, approval_binding: DraftBinding, context: dict[str, Any]
    ) -> PublicationReceipt:
        if remote.binding != approval_binding or remote.mode != ExecutionMode.MOCK:
            raise OutcomeUnknownError("게시 영수증 대사")  # Never trust a mismatched receipt.
        return PublicationReceipt(
            publication_id=context["publication_id"],
            run_id=context["run_id"],
            idempotency_key=context["idempotency_key"],
            binding=context["binding"],
            approval_id=context["approval_id"],
            status=remote.status,
            external_result_ref=remote.external_result_ref,
            mode=ExecutionMode.MOCK,
            next_action="query" if remote.status == PublicationStatus.OUTCOME_UNKNOWN else "none",
        )

    async def publish(
        self,
        binding: DraftBinding,
        *,
        run_id: str,
        publication_id: str,
        approval_id: str,
        idempotency_key: str,
    ) -> PublicationReceipt:
        approval = await self._approval(binding)
        context = {
            "publication_id": publication_id,
            "run_id": run_id,
            "idempotency_key": idempotency_key,
            "binding": binding,
            "approval_id": approval_id,
            "approved": approval.binding,
        }
        body = {
            "approval_id": approval.approval_id,
            "draft_id": approval.binding.draft_id,
            "version": approval.binding.version,
            "payload_hash": approval.binding.payload_hash,
            "simulate_outcome": self._simulate,
        }
        self.posts += 1
        try:
            response = await self._client.request(
                "POST",
                "/v1/local/publications",
                json_body=body,
                idempotency_key=idempotency_key,
                retry_read=False,
            )
        except httpx.TimeoutException as exc:
            raise OutcomeUnknownError("게시 요청") from exc
        except RfaError as exc:
            if exc.code == "upstream_http_error" and not exc.retryable:
                # A definite 4xx refusal (auth, expired/superseded approval): no effect.
                raise RfaError(
                    "publication_rejected", "게시 서비스가 요청을 거절했습니다."
                ) from None
            raise OutcomeUnknownError("게시 요청") from None  # 5xx/transport after dispatch
        try:
            view = _validated_response(_StandInPublication, response)
        except RfaError:
            raise OutcomeUnknownError("게시 영수증") from None
        self._seen[idempotency_key] = (view.receipt.publication_id, context)
        receipt = self._core(view.receipt, approval.binding, context)
        if receipt.status != PublicationStatus.SUCCEEDED:
            raise OutcomeUnknownError("게시 결과")
        return receipt

    async def query(self, idempotency_key: str) -> PublicationReceipt | None:
        seen = self._seen.get(idempotency_key)
        if seen is None:
            return None  # No receipt reference: stays unknown, never re-POSTed.
        remote_id, context = seen
        try:
            response = await self._client.request(
                "GET", f"/v1/local/publications/{_path(remote_id)}", retry_read=True
            )
        except (httpx.TimeoutException, RfaError):
            return None
        try:
            view = _validated_response(_StandInPublication, response)
            return self._core(view.receipt, context["approved"], context)
        except RfaError:
            return None


# -- P1-008: authenticated review callbacks (contract; mounted by the core API later) -------

CALLBACK_SIGNATURE_HEADER = "X-RFA-Signature"
CALLBACK_TIMESTAMP_HEADER = "X-RFA-Timestamp"


class ReviewCallback(BaseModel):
    """Decision event pushed by the review authority. Transport-authenticated only."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    schema_version: Literal["1.1"] = "1.1"
    event_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$")
    run_id: str
    draft_id: str
    draft_version: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    target: DraftTarget
    decision: ReviewStatus
    approver_id: str = Field(min_length=1, max_length=160)
    issued_at: AwareDatetime


def sign_callback(body: bytes, *, secret: SecretStr, timestamp: str) -> str:
    """HMAC-SHA256 over '<timestamp>.<raw body>' (the sender's side of the contract)."""
    digest = hmac.new(
        secret.get_secret_value().encode(), timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    return "sha256=" + digest


class ReviewCallbackVerifier:
    """Verify a pushed review decision before it can wake anything.

    Order: authenticate the raw bytes (no parsing/echo of unauthenticated input), reject
    stale timestamps, parse, bind the approver to the installation owner, then bind
    run/draft/version/hash/target to the CURRENT draft. A replayed event with the same
    payload is idempotent (same result, no new effect); a reused event id with another
    payload is a conflict. The seen-set is process-local: a replay after restart yields
    the same decision mirror, and a callback only triggers a re-query, never an approval.
    """

    def __init__(
        self,
        *,
        secret: SecretStr,
        expected_approver: str,
        max_skew: timedelta = timedelta(minutes=5),
        simulated: bool = True,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if len(secret.get_secret_value()) < 16:
            raise ValueError("callback secret is too short")
        self._secret = secret
        self._approver = expected_approver
        self._max_skew = max_skew
        self._simulated = simulated
        self._clock = clock
        self._seen: dict[str, tuple[str, ReviewDecision]] = {}

    def verify(
        self, body: bytes, *, signature: str | None, timestamp: str | None
    ) -> ReviewCallback:
        if not signature or not timestamp:
            raise RfaError("authentication_required", "서명된 callback만 받을 수 있습니다.")
        expected = sign_callback(body, secret=self._secret, timestamp=timestamp)
        if not hmac.compare_digest(signature.encode(), expected.encode()):
            raise RfaError("authentication_required", "callback 서명이 올바르지 않습니다.")
        try:
            sent = datetime.fromtimestamp(int(timestamp), UTC)
        except (ValueError, OverflowError, OSError):
            raise RfaError(
                "authentication_required", "callback 시각이 올바르지 않습니다."
            ) from None
        if abs(self._clock() - sent) > self._max_skew:
            raise RfaError("callback_expired", "오래된 callback입니다.")
        try:
            callback = ReviewCallback.model_validate_json(body)
        except ValidationError:
            raise RfaError("invalid_callback", "callback 형식이 올바르지 않습니다.") from None
        if callback.approver_id != self._approver:
            raise RfaError("approval_binding_mismatch", "승인자가 일치하지 않습니다.")
        return callback

    def accept(self, callback: ReviewCallback, draft: DraftBundle) -> tuple[ReviewDecision, bool]:
        """Return (decision mirror, is_new). Binding is checked against the current draft."""
        fingerprint = sha256_text(callback.model_dump_json())
        seen = self._seen.get(callback.event_id)
        if seen is not None:
            if seen[0] != fingerprint:
                raise RfaError("idempotency_conflict", "같은 event로 다른 결정을 받을 수 없습니다.")
            return seen[1], False
        if (
            callback.run_id,
            callback.draft_id,
            callback.draft_version,
            callback.content_hash,
            callback.target,
        ) != (draft.run_id, draft.draft_id, draft.version, draft.content_hash, draft.target):
            raise RfaError("approval_binding_mismatch", "현재 초안과 결정 대상이 다릅니다.")
        decision = ReviewDecision(
            request_id=draft.request_id,
            trace_id=draft.trace_id,
            run_id=draft.run_id,
            agent_id=draft.agent_id,
            domain_id=draft.domain_id,
            draft_id=draft.draft_id,
            draft_version=draft.version,
            content_hash=draft.content_hash,
            target=draft.target,
            decision=callback.decision,
            safe_reason="검토 서비스 callback 결정입니다.",
            simulated=self._simulated,
            adapter="review-callback",
        )
        self._seen[callback.event_id] = (fingerprint, decision)
        return decision, True
