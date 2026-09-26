"""Task-owned team lifecycle, independent of graph role execution (P0-020).

Identity/selector resolver/runtime descriptor are trusted composition inputs.
Neither caller decisions nor tracing decorators are capability authorities.
Unknown effects keep the durable slot occupied; no blind provisioning retry.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from rfa_mas.application.team_selector import SelectionDecision, SelectionRequest, TeamSelector
from rfa_mas.contracts import (
    AgentSpec,
    Audience,
    DomainId,
    ExecutionMode,
    MemberLifecycle,
    PersistentTask,
    TeamInstance,
    TeamLifecycle,
    TeamMember,
    TeamSpec,
    TrustedPrincipal,
    new_id,
)
from rfa_mas.errors import ResourceNotFoundError, RfaError
from rfa_mas.ports import RuntimePort, WorkRepositoryPort


@dataclass(frozen=True)
class RuntimeLifecycleSupport:
    supported: bool
    runtime_kind: str
    expected_mode: ExecutionMode


SelectorResolver = Callable[[TrustedPrincipal, DomainId], Awaitable[TeamSelector]]
ROLE_CAPABILITIES = {
    "supervisor": (),
    "paper_scout": ("evidence_search",),
    "source_scout": ("evidence_search",),
    "experiment_runner": ("experiment_run",),
    "result_analyst": ("result_analysis",),
    "evidence_reviewer": ("evidence_review",),
}


class TeamFactory:
    def __init__(
        self,
        repository: WorkRepositoryPort,
        runtime: RuntimePort,
        resolve_selector: SelectorResolver,
        lifecycle_support: Callable[[], RuntimeLifecycleSupport],
    ) -> None:
        self.repository = repository
        self.runtime = runtime
        self.resolve_selector = resolve_selector
        self.lifecycle_support = lifecycle_support

    @staticmethod
    def _principal(principal: TrustedPrincipal) -> TrustedPrincipal:
        try:
            principal = TrustedPrincipal.model_validate(
                principal.model_dump(warnings=False), strict=True
            )
            if not principal.authenticated or not principal.user_id:
                raise ValueError("unauthenticated")
        except (ValueError, AttributeError):
            raise RfaError("authentication_required", "유효한 인증이 필요합니다.") from None
        return principal

    def _support(self) -> RuntimeLifecycleSupport:
        support = self.lifecycle_support()
        if support.supported is not True:
            raise RfaError("not_implemented", "선택한 runtime은 팀 lifecycle을 지원하지 않습니다.")
        return support

    @staticmethod
    def _spec(task: PersistentTask, selected: SelectionDecision) -> TeamSpec:
        budget = selected.execution_budget
        assert budget is not None and selected.template is not None
        return TeamSpec(
            task_id=task.task_id,
            team_id=task.team_id,
            owner_id=task.owner_id,
            domain_id=task.domain_id,
            template=selected.template,
            definition_digest=selected.definition_digest,
            execution_budget=budget,
            members=tuple(
                TeamMember(
                    role=role,
                    spec=AgentSpec(
                        agent_id=f"{task.team_id}:{role}",
                        domain_id=task.domain_id,
                        memory_namespace=f"domains/{task.domain_id}/tasks/{task.task_id}/"
                        f"teams/{task.team_id}/{role}",
                        capabilities=ROLE_CAPABILITIES[role],
                        allowed_audiences=(Audience.OWNER, Audience.PUBLIC),
                        instructions_ref=f"approved:{selected.definition_digest}:{role}",
                        max_steps=min(100, budget.max_steps),
                        max_tool_calls=min(100, budget.max_tool_calls),
                    ),
                    # Empty source/tool scopes confer no data/tool authority. P0-020
                    # assigns execution leases via trusted policy and role budgets.
                )
                for role in selected.roles
            ),
        )

    async def ensure(
        self,
        request: SelectionRequest,
        principal: TrustedPrincipal,
        *,
        idempotency_key: str,
        task_id: str | None = None,
    ) -> TeamLifecycle:
        principal = self._principal(principal)
        support = self._support()  # Before either reservation or reuse, never hasattr(runtime).
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 160:
            raise RfaError("invalid_team_request", "유효한 멱등 key가 필요합니다.")
        try:
            # SelectionRequest explicitly rejects mutated/ambiguous budgets.
            request = SelectionRequest.model_validate(request.model_dump(warnings=False))
        except (ValueError, AttributeError):
            raise RfaError("invalid_team_request", "팀 요청 형식이 올바르지 않습니다.") from None
        previous = None
        if task_id is not None:
            previous = await self.repository.get_team_lifecycle(task_id, principal)
            if previous.task.domain_id != request.domain_id:
                raise ResourceNotFoundError("task")
            if previous.task.status != "active" or previous.task.goal != request.goal:
                raise RfaError("team_conflict", "기존 Task 목적/상태와 일치하지 않습니다.")
        # Re-resolve on EVERY invocation, including idempotent replay. No cached grants/pins.
        selector = await self.resolve_selector(principal, request.domain_id)
        selected = selector.select(request, principal)
        if selected.status != "selected" or selected.template is None:
            raise RfaError(
                "team_selection_denied", "현재 권한·승인·예산으로 팀을 선택할 수 없습니다."
            )
        if selected.template.runtime_kind != support.runtime_kind:
            raise RfaError("runtime_unavailable", "선택한 팀 runtime을 사용할 수 없습니다.")
        task = (
            previous.task
            if previous
            else PersistentTask(
                task_id=new_id("task"),
                team_id=new_id("team"),
                owner_id=principal.user_id,
                domain_id=request.domain_id,
                goal=request.goal,
            )
        )
        spec = self._spec(task, selected)
        if previous and (previous.team.spec != spec or previous.team.mode != support.expected_mode):
            raise RfaError("team_conflict", "기존 팀의 승인/권한/실행 조건을 다시 확인해야 합니다.")
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "request": request.model_dump(mode="json")
                    | {"outputs": sorted(request.outputs)},
                    "task_id": task_id,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        record, created = await self.repository.reserve_team(
            task,
            spec,
            principal,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            mode=support.expected_mode,
            existing_task_id=task_id,
        )
        # Racing create/replayed key returns the ORIGINAL identifiers. Verify current
        # selection against that persisted binding before trusting or executing it.
        if (
            record.team.spec != self._spec(record.task, selected)
            or record.team.mode != support.expected_mode
        ):
            raise RfaError("team_conflict", "저장된 팀의 실행 조건이 변경되었습니다.")
        if not created:
            return record
        return await self._prepare(record, principal)

    @staticmethod
    def _bound(reply: TeamInstance, record: TeamLifecycle) -> TeamInstance:
        if not isinstance(reply, TeamInstance):
            raise ValueError("runtime did not return TeamInstance")
        # JSON strict validation rejects model_copy bypasses, including nested bool budgets.
        # Serializer warnings can echo attacker-controlled values before validation.
        reply = TeamInstance.model_validate_json(reply.model_dump_json(warnings=False), strict=True)
        if reply.spec != record.team.spec or reply.mode != record.team.mode:
            raise ValueError("runtime binding mismatch")
        expected = {member.spec.agent_id for member in record.team.spec.members}
        if {m.agent_id for m in reply.member_states} != expected:
            raise ValueError("member state coverage missing")
        failures = {
            m.agent_id
            for m in reply.member_states
            if m.prepare == "failed" or m.cleanup == "failed"
        }
        if set(reply.failed_agent_ids) != failures or len(reply.failed_agent_ids) != len(failures):
            raise ValueError("failed member results are inconsistent")
        if record.operation == "prepare":
            if reply.state not in {"ready", "failed", "unknown"}:
                raise ValueError("invalid prepare state")
            if any(m.cleanup != "not_requested" for m in reply.member_states):
                raise ValueError("prepare cannot imply cleanup")
            if reply.state == "ready" and (
                any(m.prepare != "prepared" for m in reply.member_states)
                or reply.failed_agent_ids
                or reply.runtime_ref is None
            ):
                raise ValueError("partial team is not ready")
        else:
            if reply.state not in {"cleaned", "failed", "unknown"}:
                raise ValueError("invalid cleanup state")
            for field in ("runtime_ref", "sandbox_id"):
                known = getattr(record.team, field)
                if known is not None and getattr(reply, field) != known:
                    raise ValueError("cleanup changed known runtime identity")
            old = {m.agent_id: m for m in record.team.member_states}
            if any(m.prepare != old[m.agent_id].prepare for m in reply.member_states):
                raise ValueError("cleanup changed prepare result")
            targets = {m.agent_id for m in old.values() if m.prepare in {"prepared", "unknown"}}
            if reply.state == "cleaned" and any(
                m.cleanup != ("cleaned" if m.agent_id in targets else "not_requested")
                for m in reply.member_states
            ):
                raise ValueError("cleanup did not cover targets")
        return reply

    @staticmethod
    def _unknown(record: TeamLifecycle) -> TeamInstance:
        states = tuple(
            MemberLifecycle(
                agent_id=m.agent_id,
                prepare="unknown" if record.operation == "prepare" else m.prepare,
                cleanup=("unknown" if m.prepare in {"prepared", "unknown"} else "not_requested")
                if record.operation == "cleanup"
                else "not_requested",
            )
            for m in record.team.member_states
        )
        return record.team.model_copy(update={"state": "unknown", "member_states": states})

    async def _prepare(self, record: TeamLifecycle, principal: TrustedPrincipal) -> TeamLifecycle:
        reason = "outcome_unknown"
        try:
            reply = await asyncio.wait_for(
                self.runtime.prepare(
                    record.team.spec.model_copy(deep=True),
                    idempotency_key=record.operation_key,
                ),
                timeout=record.team.spec.execution_budget.timeout_seconds,
            )
            try:
                reply = self._bound(reply, record)
                reason = {
                    "ready": "ready",
                    "failed": "partial_failure",
                    "unknown": "outcome_unknown",
                }[reply.state]
            except (ValueError, TypeError):
                reply, reason = self._unknown(record), "invalid_contract"
        except asyncio.CancelledError:
            await self.repository.finish_team_operation(
                record.task.task_id,
                principal,
                generation=record.generation,
                instance=self._unknown(record),
                reason="outcome_unknown",
            )
            raise
        except Exception:
            reply = self._unknown(record)  # Never store raw runtime error text.
        saved = await self.repository.finish_team_operation(
            record.task.task_id,
            principal,
            generation=record.generation,
            instance=reply,
            reason=reason,
        )
        if reason == "partial_failure":
            return await self.cleanup(record.task.task_id, principal)
        return saved

    async def cleanup(self, task_id: str, principal: TrustedPrincipal) -> TeamLifecycle:
        principal = self._principal(principal)
        support = self._support()
        current = await self.repository.get_team_lifecycle(task_id, principal)
        if (
            current.team.mode != support.expected_mode
            or current.team.spec.template.runtime_kind != support.runtime_kind
        ):
            raise RfaError("runtime_unavailable", "기존 runtime의 정리가 필요합니다.")
        record, started = await self.repository.start_team_cleanup(task_id, principal)
        if not started:
            return record
        reason = "outcome_unknown"
        try:
            # Only the persisted owned ID goes to cleanup, NEVER an untrusted reply's ID.
            reply = await asyncio.wait_for(
                self.runtime.cleanup(
                    record.team.spec.team_id,
                    idempotency_key=record.operation_key,
                ),
                timeout=record.team.spec.execution_budget.timeout_seconds,
            )
            try:
                reply = self._bound(reply, record)
                reason = {
                    "cleaned": "cleaned",
                    "failed": "cleanup_failed",
                    "unknown": "outcome_unknown",
                }[reply.state]
            except (ValueError, TypeError):
                reply, reason = self._unknown(record), "invalid_contract"
        except asyncio.CancelledError:
            await self.repository.finish_team_operation(
                task_id,
                principal,
                generation=record.generation,
                instance=self._unknown(record),
                reason="outcome_unknown",
            )
            raise
        except Exception:
            reply = self._unknown(record)
        return await self.repository.finish_team_operation(
            task_id,
            principal,
            generation=record.generation,
            instance=reply,
            reason=reason,
        )
