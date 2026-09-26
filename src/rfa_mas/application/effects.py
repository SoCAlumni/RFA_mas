"""P0-021: durable effect-ledger guard for review submission (ResponsePort).

The review authority (teammate Response service; mock/local stand-in here) may keep an
in-memory idempotency cache, but such caches are optimizations only: they vanish with the
process. This guard records a durable INTENT before `submit_draft`, marks INFLIGHT right
before dispatch and stores the returned decision as an approval MIRROR (never an approval
itself). Replaying the same key reuses the mirror; a different payload under the same key
is rejected. An intent left by a stopped process becomes OUTCOME_UNKNOWN and is resolved
only by `get_decision` (a result query). The draft is never re-submitted automatically.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from pydantic import ValidationError

from rfa_mas.contracts import DraftBundle, ReviewDecision, SimulationScenario
from rfa_mas.errors import OutcomeUnknownError, ResourceNotFoundError, RfaError
from rfa_mas.ports.interfaces import EffectLedger, EffectRecord, Reconciliation

REVIEW_KIND = "review_submission"
# Transport outcomes after which the authority may or may not have accepted the draft.
_UNKNOWN_CODES = frozenset({"outcome_unknown", "upstream_timeout"})


def payload_fingerprint(value: dict[str, Any]) -> str:
    canonical = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def review_ref(draft_id: str, version: int, content_hash: str) -> str:
    """Where the submission result is found: the authority's decision for this version."""
    return f"review:{draft_id}@v{version}:{content_hash}"


def _parse_review_ref(ref: str | None) -> tuple[str, int, str] | None:
    if not ref or not ref.startswith("review:"):
        return None
    try:
        draft_id, rest = ref[len("review:") :].rsplit("@v", 1)
        version, content_hash = rest.split(":", 1)
        return draft_id, int(version), content_hash
    except ValueError:
        return None


def _decision(raw: Any) -> ReviewDecision | None:
    if raw is None:
        return None
    try:
        data = raw.model_dump() if hasattr(raw, "model_dump") else raw
        return ReviewDecision.model_validate(data)
    except (ValidationError, TypeError):
        return None


class EffectGuardedResponse:
    """ResponsePort decorator; composes over the configured (observed) port."""

    def __init__(self, inner: Any, ledger: EffectLedger) -> None:
        self.inner = inner
        self.ledger = ledger
        self.adapter_name = getattr(inner, "adapter_name", "unnamed-response")
        self.simulated = getattr(inner, "simulated", True)
        # Same-process serialization only; the durable ledger row is the authority.
        self._locks: dict[str, asyncio.Lock] = {}

    async def submit_draft(
        self,
        draft: DraftBundle,
        *,
        idempotency_key: str,
        simulation_scenario: SimulationScenario = SimulationScenario.SUCCESS,
    ) -> Any:
        key = f"{REVIEW_KIND}:{idempotency_key}"
        fingerprint = payload_fingerprint(
            {
                "draft": draft.model_dump(mode="json"),
                "simulation_scenario": SimulationScenario(simulation_scenario).value,
            }
        )
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            started, record = await self.ledger.begin_effect(
                draft.run_id,
                operation_key=key,
                kind=REVIEW_KIND,
                payload_fingerprint=fingerprint,
                result_ref=review_ref(draft.draft_id, draft.version, draft.content_hash),
            )
            if not started:
                return await self._replay(record, draft)
            await self.ledger.advance_effect(draft.run_id, key, state="inflight")
            try:
                raw = await self.inner.submit_draft(
                    draft, idempotency_key=idempotency_key, simulation_scenario=simulation_scenario
                )
            except RfaError as exc:
                if isinstance(exc, OutcomeUnknownError) or exc.code in _UNKNOWN_CODES or (
                    exc.retryable
                ):
                    await self._unknown(draft.run_id, key)
                else:  # A definite refusal: the authority accepted nothing.
                    await self.ledger.advance_effect(
                        draft.run_id, key, state="completed", outcome=exc.code
                    )
                raise
            except (Exception, asyncio.CancelledError):
                # Timeout/transport failure/cancellation after dispatch: may have happened.
                await self._unknown(draft.run_id, key)
                raise
            decision = _decision(raw)
            await self.ledger.advance_effect(
                draft.run_id,
                key,
                state="completed",
                outcome=decision.decision.value if decision else "invalid_review_result",
                approval=decision.model_dump(mode="json") if decision else None,
            )
            return raw

    async def _unknown(self, run_id: str, key: str) -> None:
        await asyncio.shield(self.ledger.advance_effect(run_id, key, state="outcome_unknown"))

    async def _replay(self, record: EffectRecord, draft: DraftBundle) -> ReviewDecision:
        if record.state == "completed":
            if record.approval is None:
                code = record.outcome or "invalid_review_result"
                raise RfaError(code, "이전 검토 요청 결과입니다.")
            return ReviewDecision.model_validate(record.approval)
        if record.state == "outcome_unknown":
            found = await self._query(
                draft.run_id, draft.draft_id, draft.version, draft.content_hash
            )
            if found is not None:
                await self._complete(draft.run_id, record.operation_key, found)
                return found
            # Reconciled by query only: never re-submitted automatically.
            raise OutcomeUnknownError("review submission")
        # INTENT/INFLIGHT held by another live invocation: never a second dispatch.
        raise RfaError("effect_in_progress", "같은 검토 요청이 아직 처리 중입니다.", retryable=True)

    async def _query(
        self, run_id: str, draft_id: str, version: int, content_hash: str
    ) -> ReviewDecision | None:
        try:
            decision = _decision(await self.inner.get_decision(draft_id))
        except (RfaError, TimeoutError):
            return None
        if decision is None or (
            decision.run_id,
            decision.draft_id,
            decision.draft_version,
            decision.content_hash,
        ) != (run_id, draft_id, version, content_hash):
            return None
        return decision

    async def _complete(self, run_id: str, key: str, decision: ReviewDecision) -> None:
        await self.ledger.advance_effect(
            run_id,
            key,
            state="completed",
            outcome=decision.decision.value,
            approval=decision.model_dump(mode="json"),
        )

    async def get_decision(self, draft_id: str) -> Any:
        raw = await self.inner.get_decision(draft_id)
        decision = _decision(raw)
        if decision is not None and decision.draft_id == draft_id:
            # Any decision for this exact version proves the authority received it.
            ref = review_ref(decision.draft_id, decision.draft_version, decision.content_hash)
            try:
                records = await self.ledger.effects_by_ref(
                    decision.run_id, kind=REVIEW_KIND, result_ref=ref
                )
            except ResourceNotFoundError:
                records = ()
            for record in records:
                if record.state == "outcome_unknown":
                    await self._complete(decision.run_id, record.operation_key, decision)
        return raw

    async def reconcile(self, record: EffectRecord) -> Reconciliation:
        """Resolve one unknown submission by result query; never by re-submitting."""
        parsed = _parse_review_ref(record.result_ref)
        found = None
        if record.state == "outcome_unknown" and parsed is not None and record.run_id:
            found = await self._query(record.run_id, *parsed)
            if found is not None:
                await self._complete(record.run_id, record.operation_key, found)
        return Reconciliation(
            operation_key=record.operation_key,
            kind=record.kind,
            before=record.state,
            after="completed" if found is not None else record.state,
            method="result_query",
            found=found is not None,
        )
