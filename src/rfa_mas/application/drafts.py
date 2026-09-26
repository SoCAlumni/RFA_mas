"""P1-005A: immutable DRAFT versions, approval invalidation and mock publication state.

The review authority (the teammate response module; a mock/local stand-in here) owns
approvals. This service only mirrors its decision and checks, at the moment of use, that
the decision still binds the CURRENT draft version/content hash/target, the current policy
version and the current source/ACL revisions (via the same ResumePolicy used on resume).

Publication state is separate from the Run lifecycle. A durable PENDING receipt is written
before the publisher is called; an unknown outcome is reconciled only by querying the
publisher with the same owned idempotency key, never by publishing again.

P1-008E: the publication authority's own approval proof (Publisher.authorize) runs before
that durable intent, so a premature publish is approval_required and leaves no receipt. A
publisher refusal that provably dispatched nothing withdraws the untouched intent; only a
definite rejection of an attempted publish is FAILED.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Protocol

from rfa_mas.application.graphs.domain import SENSITIVE_MARKERS
from rfa_mas.application.state_machine import ensure_publication_transition
from rfa_mas.contracts import (
    AttachmentRef,
    Audience,
    DraftBinding,
    DraftBundle,
    DraftEditRequest,
    DraftState,
    ExecutionMode,
    PublicationReceipt,
    PublicationStatus,
    PublishRequest,
    ReviewDecision,
    ReviewStatus,
    SourceRevisionRef,
    TrustedPrincipal,
    new_id,
    sha256_text,
)
from rfa_mas.errors import (
    OutcomeUnknownError,
    PublicationNotAttemptedError,
    ResourceNotFoundError,
    RfaError,
)

# Current-policy/source failures withhold the stored draft from outward views.
_WITHHOLD = {"resume_review_required", "policy_denied"}
_HIDDEN_REASONS = {"sources_changed", "policy_changed"}


class Publisher(Protocol):
    adapter_name: str
    simulated: bool
    mode: ExecutionMode

    async def authorize(self, binding: DraftBinding) -> None:
        """Prove the publication authority's own approval for exactly this binding.

        Runs before any durable intent and dispatches nothing. Raises approval_required
        when that authority holds no valid approval, or another RfaError (e.g. timeout).
        """
        ...

    async def publish(
        self,
        binding: DraftBinding,
        *,
        run_id: str,
        publication_id: str,
        approval_id: str,
        idempotency_key: str,
    ) -> PublicationReceipt: ...

    async def query(self, idempotency_key: str) -> PublicationReceipt | None: ...


def draft_binding(draft: DraftBundle, attachments: tuple[AttachmentRef, ...]) -> DraftBinding:
    """Exact publication binding: content, attachments, target, policy and source revisions."""
    sources = tuple(
        SourceRevisionRef(
            **ref.model_dump(exclude={"schema_version"}),
            # Local KB semantics (P1-001A): the ACL revision is the source revision.
            acl_revision=ref.source_revision,
            policy_version=draft.policy_version,
        )
        for ref in draft.allowed_evidence
    )
    payload = {
        "draft_id": draft.draft_id,
        "version": draft.version,
        "content": draft.content,
        "content_hash": draft.content_hash,
        "attachments": [item.model_dump(mode="json") for item in attachments],
        "target": draft.target.model_dump(mode="json"),
        "policy_version": draft.policy_version,
        "sources": [item.model_dump(mode="json") for item in sources],
    }
    return DraftBinding(
        draft_id=draft.draft_id,
        version=draft.version,
        content_hash=draft.content_hash,
        payload_hash=sha256_text(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        ),
        target=draft.target,
        policy_version=draft.policy_version,
        sources=sources,
    )


class DraftLifecycle:
    def __init__(
        self,
        *,
        repository: Any,
        dependencies: Callable[[], Any],
        publisher: Publisher | None,
    ) -> None:
        self._repository = repository
        # Read the Supervisor dependencies at call time: the response authority,
        # ResumePolicy and policy version are the same objects the graph uses.
        self._deps = dependencies
        self._publisher = publisher

    # -- read side ------------------------------------------------------------------
    async def _versions(self, run_id: str, principal: TrustedPrincipal):
        versions = await self._repository.draft_versions(run_id, principal)
        if not versions:
            raise ResourceNotFoundError("draft")
        return [
            (draft, tuple(AttachmentRef.model_validate(item) for item in attachments))
            for draft, attachments in versions
        ]

    async def _review(
        self, run_id: str, draft: DraftBundle, principal: TrustedPrincipal
    ) -> ReviewDecision | None:
        raw = None
        try:
            raw = await self._deps().response.get_decision(draft.draft_id)
        except (RfaError, TimeoutError):
            raw = None  # Unknown review state is never an approval.
        if raw is None:
            record = await self._repository.get_owned_run(run_id, principal)
            raw = record.result.review if record.result is not None else None
        if raw is None:
            return None
        data = raw.model_dump() if hasattr(raw, "model_dump") else raw
        return ReviewDecision.model_validate(data)

    async def _approval(
        self, draft: DraftBundle, review: ReviewDecision | None, principal: TrustedPrincipal
    ) -> tuple[bool, str | None]:
        deps = self._deps()
        if sha256_text(draft.content) != draft.content_hash:
            return False, "content_hash_mismatch"
        current_policy = deps.policy_version() if deps.policy_version else None
        if current_policy is None or draft.policy_version != current_policy:
            return False, "policy_changed"
        try:
            await deps.validate_resume(draft, principal)
        except RfaError as exc:
            if exc.code in _WITHHOLD:
                return False, "sources_changed"
            raise
        if review is None:
            return False, "review_missing"
        if review.draft_id != draft.draft_id or review.run_id != draft.run_id:
            return False, "approval_binding_mismatch"
        if review.draft_version != draft.version:
            return False, (
                "draft_changed"
                if review.draft_version < draft.version
                else "approval_binding_mismatch"
            )
        if review.content_hash != draft.content_hash or review.target != draft.target:
            return False, "approval_binding_mismatch"
        if review.decision != ReviewStatus.APPROVED:
            return False, "review_" + review.decision.value
        return True, None

    async def state(self, run_id: str, principal: TrustedPrincipal) -> DraftState:
        versions = await self._versions(run_id, principal)
        draft, attachments = versions[-1]
        review = await self._review(run_id, draft, principal)
        valid, reason = await self._approval(draft, review, principal)
        publication = await self._repository.get_publication(run_id, principal)
        visible = reason not in _HIDDEN_REASONS
        return DraftState(
            run_id=run_id,
            draft=draft if visible else None,
            current_version=draft.version,
            versions=tuple(item.version for item, _ in versions),
            attachments=attachments if visible else (),
            review=review if visible else None,
            approval_valid=valid,
            invalid_reason=reason,
            publication=publication,
            publication_status=publication.status
            if publication
            else PublicationStatus.NOT_REQUESTED,
            publication_mode=publication.mode if publication else None,
        )

    # -- owner edits and re-review ----------------------------------------------------
    async def edit(
        self, run_id: str, body: DraftEditRequest, principal: TrustedPrincipal
    ) -> DraftState:
        versions = await self._versions(run_id, principal)
        current, _ = versions[-1]
        if body.expected_version != current.version:
            raise RfaError("draft_version_conflict", "최신 DRAFT 버전이 아닙니다.")
        if await self._repository.get_publication(run_id, principal) is not None:
            raise RfaError("publication_exists", "게시 요청이 있는 초안은 수정할 수 없습니다.")
        deps = self._deps()
        target = body.target or current.target
        if target.audience not in {Audience.OWNER, Audience.PRIVATE} and any(
            pattern.search(body.content) for pattern in SENSITIVE_MARKERS
        ):
            raise RfaError(
                "policy_denied", "비공개 표식이 있는 내용은 이 대상과 공유할 수 없습니다."
            )
        candidate = DraftBundle.model_validate(
            {
                **current.model_dump(),
                "version": current.version + 1,
                "content": body.content,
                "content_hash": sha256_text(body.content),
                "target": target.model_dump(),
                "audience": target.audience,
                "policy_version": deps.policy_version(),
            }
        )
        try:
            # A changed target is re-authorized for every cited source (share policy).
            await deps.validate_resume(candidate, principal)
        except RfaError as exc:
            if exc.code in _WITHHOLD:
                raise RfaError(
                    "policy_denied", "현재 자료 권한과 정책으로 이 대상의 초안을 만들 수 없습니다."
                ) from None
            raise
        await self._repository.append_draft_version(
            run_id,
            principal,
            candidate,
            tuple(item.model_dump(mode="json") for item in body.attachments),
        )
        return await self.state(run_id, principal)

    async def request_review(self, run_id: str, principal: TrustedPrincipal) -> DraftState:
        versions = await self._versions(run_id, principal)
        draft, _ = versions[-1]
        if await self._repository.get_publication(run_id, principal) is not None:
            raise RfaError("publication_exists", "이미 게시 요청이 있습니다.")
        review = await self._review(run_id, draft, principal)
        if (
            review is not None
            and review.draft_version == draft.version
            and review.content_hash == draft.content_hash
            and review.target == draft.target
        ):
            # Already submitted; the review authority owns the decision.
            return await self.state(run_id, principal)
        deps = self._deps()
        if draft.policy_version != deps.policy_version():
            raise RfaError("resume_review_required", "현재 정책으로 새 초안을 만들어야 합니다.")
        await deps.validate_resume(draft, principal)
        key = "owned:" + sha256_text(
            json.dumps(
                [principal.user_id, run_id, draft.draft_id, draft.version, draft.content_hash]
            )
        )
        await deps.response.submit_draft(draft, idempotency_key=key)
        return await self.state(run_id, principal)

    # -- publication --------------------------------------------------------------------
    @staticmethod
    def _owned_key(principal: TrustedPrincipal, run_id: str, key: str) -> str:
        return "owned:" + sha256_text(json.dumps([principal.user_id, run_id, key]))

    async def publish(
        self, run_id: str, body: PublishRequest, principal: TrustedPrincipal
    ) -> PublicationReceipt:
        if self._publisher is None:
            raise RfaError("not_implemented", "게시 adapter가 구성되지 않았습니다.")
        existing = await self._repository.get_publication(run_id, principal)
        if existing is not None:
            if existing.idempotency_key != body.idempotency_key:
                raise RfaError("publication_exists", "이 실행에는 이미 게시 요청이 있습니다.")
            if existing.status in {PublicationStatus.PENDING, PublicationStatus.OUTCOME_UNKNOWN}:
                return await self.query(run_id, principal)  # reconcile, never republish
            return existing
        versions = await self._versions(run_id, principal)
        draft, attachments = versions[-1]
        review = await self._review(run_id, draft, principal)
        valid, reason = await self._approval(draft, review, principal)
        if not valid:
            raise RfaError(
                "approval_required",
                f"현재 초안 버전에 유효한 승인이 없습니다({reason}).",
            )
        binding = draft_binding(draft, attachments)
        # P1-008E: the publication authority's proof precedes the durable intent, so a
        # premature publish cannot leave a receipt that blocks this run after approval.
        await self._publisher.authorize(binding)
        approval_id = "review:" + sha256_text(
            json.dumps([review.draft_id, review.draft_version, review.content_hash, review.adapter])
        )[:32]
        created = PublicationReceipt(
            publication_id=new_id("pub"),
            run_id=run_id,
            idempotency_key=body.idempotency_key,
            binding=binding,
            approval_id=approval_id,
            status=PublicationStatus.PENDING,
            mode=self._publisher.mode,
            next_action="query",
        )
        ensure_publication_transition(PublicationStatus.NOT_REQUESTED, created.status)
        pending = await self._repository.put_publication(created, principal, expected_status=None)
        if pending.publication_id != created.publication_id:
            return pending  # A concurrent same-key request owns this publication.
        owned = self._owned_key(principal, run_id, body.idempotency_key)
        try:
            outcome = await self._publisher.publish(
                binding,
                run_id=run_id,
                publication_id=pending.publication_id,
                approval_id=approval_id,
                idempotency_key=owned,
            )
            update = {
                "status": PublicationStatus.SUCCEEDED,
                "external_result_ref": outcome.external_result_ref,
                "mode": outcome.mode,
                "next_action": "none",
            }
        except PublicationNotAttemptedError:
            # Refused before dispatch (e.g. the approval expired since authorize): nothing
            # was sent, so the untouched intent is withdrawn instead of becoming FAILED.
            await self._repository.withdraw_unsent_publication(pending, principal)
            raise
        except (OutcomeUnknownError, TimeoutError):
            update = {"status": PublicationStatus.OUTCOME_UNKNOWN, "next_action": "query"}
        except RfaError:
            update = {"status": PublicationStatus.FAILED, "next_action": "review"}
        return await self._advance(pending, update, principal)

    async def _advance(
        self, current: PublicationReceipt, update: dict, principal: TrustedPrincipal
    ) -> PublicationReceipt:
        ensure_publication_transition(current.status, update["status"])
        receipt = PublicationReceipt.model_validate({**current.model_dump(), **update})
        return await self._repository.put_publication(
            receipt, principal, expected_status=current.status.value
        )

    async def publication(self, run_id: str, principal: TrustedPrincipal) -> PublicationReceipt:
        receipt = await self._repository.get_publication(run_id, principal)
        if receipt is None:
            raise ResourceNotFoundError("publication")
        return receipt

    async def query(self, run_id: str, principal: TrustedPrincipal) -> PublicationReceipt:
        """Reconcile a pending/unknown publication by result lookup only."""
        receipt = await self.publication(run_id, principal)
        if receipt.status not in {PublicationStatus.PENDING, PublicationStatus.OUTCOME_UNKNOWN}:
            return receipt
        if self._publisher is None:
            return receipt
        found = await self._publisher.query(
            self._owned_key(principal, run_id, receipt.idempotency_key)
        )
        if found is None or found.status != PublicationStatus.SUCCEEDED:
            if receipt.status == PublicationStatus.PENDING:
                # A crash between the durable intent and the call: unknown, never "not sent".
                return await self._advance(
                    receipt,
                    {"status": PublicationStatus.OUTCOME_UNKNOWN, "next_action": "query"},
                    principal,
                )
            return receipt
        if found.binding != receipt.binding:
            raise RfaError("approval_binding_mismatch", "게시 결과가 승인된 초안과 다릅니다.")
        return await self._advance(
            receipt,
            {
                "status": PublicationStatus.SUCCEEDED,
                "external_result_ref": found.external_result_ref,
                "mode": found.mode,
                "next_action": "none",
            },
            principal,
        )
