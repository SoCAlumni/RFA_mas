"""Bounded, payload-free observations of actual calls, not an authorization service."""

from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import wraps
from time import perf_counter
from typing import Any

from langsmith import tracing_context

from rfa_mas.contracts import (
    AgentSpec,
    ObservationCoverage,
    ObservationLedger,
    TraceEvent,
    TrustedPrincipal,
    VersionReferences,
)
from rfa_mas.errors import OutcomeUnknownError, RfaError
from rfa_mas.ports import TracePort, WorkRepositoryPort

_ACTIVE: ContextVar[tuple[str, TrustedPrincipal] | None] = ContextVar(
    "rfa_observation", default=None
)
_ACTOR: ContextVar[str] = ContextVar("rfa_observation_actor", default="assistant-supervisor")
BOUNDARIES = ("request", "retrieval", "policy", "model", "runtime", "approval")
SAFE_ERROR_CODES = frozenset(
    {
        "authentication_required",
        "policy_denied",
        "configuration_error",
        "not_implemented",
        "not_found",
        "idempotency_conflict",
        "invalid_state_transition",
        "draft_version_conflict",
        "thread_busy",
        "resume_unavailable",
        "resume_review_required",
        "resume_error",
        "budget_exceeded",
        "outcome_unknown",
        "domain_not_resolved",
        "domain_task_failed",
        "draft_binding_mismatch",
        "approval_binding_mismatch",
        "invalid_review_result",
        "invalid_runtime_output",
        "invalid_runtime_result",
        "runtime_result_binding_mismatch",
        "retrieval_timeout",
        "internal_error",
        "session_mismatch",
        "storage_busy",
        "review_query_pending",
        # P0-020 team execution stop reasons (fixed codes, never provider text).
        "cancelled",
        "budget_unavailable",
        "team_not_ready",
        "team_binding_conflict",
        "team_selection_denied",
        "team_conflict",
        "runtime_unavailable",
        "role_failed",
        "role_timeout",
        "tool_not_allowed",
        "direct_message_denied",
        "task_domain_mismatch",
        # P1-005A DRAFT lifecycle (fixed codes).
        "approval_required",
        "publication_exists",
        # P0-021 effect ledger (fixed codes).
        "permission_revoked",
        "effect_in_progress",
        "retry_exists",
        # P1-005B feedback memory (fixed codes).
        "feedback_unclassified",
        "feedback_invalid",
    }
)
TEAM_ROLES = frozenset(
    {
        "supervisor",
        "paper_scout",
        "experiment_runner",
        "result_analyst",
        "source_scout",
        "evidence_reviewer",
    }
)


def safe_error_code(value: str) -> str:
    return value if value in SAFE_ERROR_CODES else "internal_error"


def native_trace_guard(function):
    @wraps(function)
    async def guarded(*args, **kwargs):
        # Public SDK context overrides ambient tracing, without reading any key.
        with tracing_context(enabled=False, parent=False):
            return await function(*args, **kwargs)

    return guarded


class Observations:
    def __init__(self, repository: WorkRepositoryPort, trace: TracePort, *, policy: Any):
        self.repository, self.trace = repository, trace
        self.policy = policy

    @asynccontextmanager
    async def scope(self, run_id: str, principal: TrustedPrincipal):
        await self.repository.get_owned_run(run_id, principal)
        token = _ACTIVE.set((run_id, principal))
        try:
            yield
        finally:
            _ACTIVE.reset(token)

    async def record(
        self,
        boundary: str,
        status: str,
        *,
        mode: str = "local",
        duration_ms: float | None = None,
        reason: str | None = None,
        origin: str = "port",
        draft_id: str | None = None,
        sources: tuple[tuple[str, str], ...] = (),
        transport: str | None = None,
        provider_kind: str = "builtin",
    ):
        active = _ACTIVE.get()
        if active is None:
            return None
        run_id, principal = active
        context = await self.repository.observation_context(run_id, principal)
        context = context.model_copy(
            update={
                "agent_id": await self.repository.observation_alias(
                    run_id, principal, "actor", _ACTOR.get()
                )
            }
        )
        # Values below come from configured call sites, never provider text/IDs.
        provider = await self.repository.observation_alias(
            run_id, principal, "provider", f"{boundary}:{provider_kind}"
        )
        policy = await self.repository.observation_alias(
            run_id, principal, "policy", self.policy.policy_version
        )
        safe_sources = []
        if context.domain_id is not None and sources:
            known = await self.repository.list_documents(context.domain_id.value)
            refs = {(d.source_id, d.source_revision) for d in known}
            for source in sources:
                if source in refs:
                    safe_sources.append(
                        await self.repository.observation_alias(
                            run_id, principal, "source", repr(source)
                        )
                    )
        draft = None
        if draft_id is not None:
            stored = await self.repository.get_owned_run(run_id, principal)
            if stored.result and stored.result.draft and stored.result.draft.draft_id == draft_id:
                draft = await self.repository.observation_alias(
                    run_id, principal, "draft", draft_id
                )
        event = TraceEvent(
            execution=context,
            event=boundary,
            status=status,
            mode=mode,
            versions=VersionReferences(
                code="rfa-observations-v1", policy=policy, sources=tuple(safe_sources)
            ),
            timestamp=datetime.now(UTC),
            duration_ms=duration_ms,
            # A persisted intent can precede a failed file export, so it does not
            # prove the inner port was invoked. Completion/exception confirms it.
            call_count=int(transport is not None or origin == "service" and status == "started"),
            reason_code=reason,
            draft_id=draft,
        )
        record = await self.repository.append_observation(
            run_id,
            principal,
            event,
            origin=origin,
            provider_ref=provider,
            transport=transport,
            provider_kind=provider_kind,
        )
        await self.trace.emit_observation(record)
        return record

    async def ledger(self, run_id: str, principal: TrustedPrincipal) -> ObservationLedger:
        context = await self.repository.observation_context(run_id, principal)
        records = await self.repository.list_observations(run_id, principal)
        # Presence proves collection at that boundary only. Never infer absent calls as zero.
        collected = {r.event.event for r in records if r.origin in {"service", "port"}}
        incomplete = {
            name
            for name in (*BOUNDARIES, "tool")
            if sum(
                r.origin == "port" and r.event.event == name and r.event.status == "started"
                for r in records
            )
            > sum(
                r.origin == "port" and r.event.event == name and r.transport is not None
                for r in records
            )
        }
        coverage = tuple(
            ObservationCoverage(
                boundary=name,
                state="incomplete"
                if name in incomplete
                else "collected"
                if name in collected
                else "uncollected",
                calls=sum(r.event.call_count for r in records if r.event.event == name)
                if name in collected and name not in incomplete
                else None,
            )
            for name in (*BOUNDARIES, "tool", "publish", "internal_nodes")
        )
        receipts = [r for r in records if r.origin == "test_sink"]
        coverage += (
            ObservationCoverage(
                boundary="test_sink",
                state="collected" if receipts else "uncollected",
                calls=sum(r.event.status == "succeeded" for r in receipts) if receipts else None,
            ),
        )
        return ObservationLedger(execution=context, observations=records, coverage=coverage)

    async def record_test_sink(self, *, received: bool):
        """Called only by an in-process test sink AFTER receipt; never a tool success proxy."""
        if type(received) is not bool:
            raise ValueError("sink receipt must be an observed boolean")
        return await self.record(
            "tool",
            "succeeded" if received else "denied",
            mode="mock",
            reason="ok" if received else "denied",
            origin="test_sink",
            provider_kind="test",
        )

    async def bind_domain(self, domain):
        active = _ACTIVE.get()
        if active is not None:
            run_id, principal = active
            await self.repository.observation_alias(run_id, principal, "domain", domain.value)

    async def team_actor(self, agent_id: str) -> str | None:
        """Safe actor label only for a member of the team durably bound to this Run."""
        active = _ACTIVE.get()
        if active is None:
            return None
        run_id, principal = active
        binding = await self.repository.run_team_binding(run_id, principal)
        if binding is None:
            return None
        task_id, team_id = binding
        lifecycle = await self.repository.get_team_lifecycle(task_id, principal)
        for member in lifecycle.team.spec.members:
            if member.spec.agent_id == agent_id and lifecycle.team.spec.team_id == team_id:
                return f"team-member:{member.role}" if member.role in TEAM_ROLES else None
        return None


class ObservedPort:
    """Explicit methods preserve port identity/capabilities; no automatic profiler."""

    def __init__(
        self,
        inner: Any,
        observer: Observations,
        boundary: str,
        *,
        mode: str,
        provider_kind: str = "builtin",
    ):
        self.inner, self.observer, self.boundary, self.mode = inner, observer, boundary, mode
        self.adapter_name, self.simulated = inner.adapter_name, inner.simulated
        self.provider_kind = provider_kind

    @property
    def policy_version(self):
        return self.inner.policy_version

    async def _call(self, method: str, *args, **kwargs):
        await self.observer.record(
            self.boundary, "started", mode=self.mode, provider_kind=self.provider_kind
        )
        start = perf_counter()
        try:
            result = await getattr(self.inner, method)(*args, **kwargs)
        except BaseException as exc:
            import asyncio

            cancelled = isinstance(exc, asyncio.CancelledError)
            unknown = isinstance(exc, OutcomeUnknownError)
            reason = (
                "cancelled"
                if cancelled
                else "timeout"
                if isinstance(exc, (TimeoutError, OutcomeUnknownError))
                or isinstance(exc, RfaError)
                and exc.code == "upstream_timeout"
                else "provider_error"
            )
            await self.observer.record(
                self.boundary,
                "cancelled" if cancelled else "outcome_unknown" if unknown else "failed",
                mode=self.mode,
                duration_ms=(perf_counter() - start) * 1000,
                reason=reason,
                transport="raised",
                provider_kind=self.provider_kind,
            )
            raise
        outcome = "succeeded"
        reason = None
        if self.boundary == "policy" and getattr(result, "allowed", None) is False:
            outcome = "denied"
        elif self.boundary == "approval":
            outcome = {
                "rejected": "denied",
                "pending": "waiting",
                "revision_requested": "waiting",
                "outcome_unknown": "outcome_unknown",
            }.get(getattr(result, "decision", None), "succeeded")
            if result is None:
                outcome = "waiting"
        elif self.boundary in {"runtime", "tool"}:
            runtime_status = getattr(result, "status", None)
            outcome = {
                "succeeded": "succeeded",
                "failed": "failed",
                "denied": "denied",
                "timed_out": "failed",
                "outcome_unknown": "outcome_unknown",
            }.get(runtime_status, "failed")
            if runtime_status in {"timed_out", "outcome_unknown"}:
                reason = "timeout"
            elif runtime_status is None:
                reason = "invalid_contract"
        sources = (
            tuple((item.source_id, item.source_revision) for item in result.items)
            if self.boundary == "retrieval" and hasattr(result, "items")
            else ()
        )
        await self.observer.record(
            self.boundary,
            outcome,
            mode=self.mode,
            duration_ms=(perf_counter() - start) * 1000,
            reason="denied" if outcome == "denied" else reason,
            sources=sources,
            transport="returned",
            provider_kind=self.provider_kind,
        )
        return result

    async def generate(self, request):
        return await self._call("generate", request)

    async def search(self, request):
        return await self._call("search", request)

    async def load_context(self, request):
        return await self._call("load_context", request)

    async def evaluate(self, request):
        return await self._call("evaluate", request)

    async def run(self, spec, request):
        # Current registered role, not an arbitrary caller/provider actor label.
        checked = AgentSpec.model_validate(spec.model_dump())
        await self.observer.bind_domain(checked.domain_id)
        expected = f"domain-supervisor:{checked.domain_id.value}"
        actor = (
            expected
            if checked.agent_id == expected
            else await self.observer.team_actor(checked.agent_id) or "assistant-supervisor"
        )
        token = _ACTOR.set(actor)
        try:
            return await self._call("run", spec, request)
        finally:
            _ACTOR.reset(token)

    async def execute(self, request):
        return await self._call("execute", request)

    async def status(self, run_id):
        return await self._call("status", run_id)

    async def cancel(self, run_id):
        return await self._call("cancel", run_id)

    async def prepare(self, spec, *, idempotency_key):
        if not callable(getattr(self.inner, "prepare", None)):
            raise RfaError("not_implemented", "runtime prepare가 지원되지 않습니다.")
        return await self._call("prepare", spec, idempotency_key=idempotency_key)

    async def cleanup(self, team_id, *, idempotency_key):
        if not callable(getattr(self.inner, "cleanup", None)):
            raise RfaError("not_implemented", "runtime cleanup이 지원되지 않습니다.")
        return await self._call("cleanup", team_id, idempotency_key=idempotency_key)

    async def submit_draft(self, draft, **kwargs):
        return await self._call("submit_draft", draft, **kwargs)

    async def get_decision(self, draft_id):
        return await self._call("get_decision", draft_id)
