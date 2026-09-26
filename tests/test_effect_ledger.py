"""P0-021: durable effect ledger, idempotent replay, crash reconciliation and barriers.

Synthetic fixtures only: offline settings, temp SQLite, an in-process fake review authority
and fake publishers that play the role of external systems outliving our process. Nothing
here is a live NVIDIA/teammate/publication result; publication receipts stay mode=mock.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace

import pytest

from rfa_mas.adapters.mock import MockPublisher
from rfa_mas.application import workers
from rfa_mas.bootstrap import build_container
from rfa_mas.contracts import (
    Audience,
    DirectWorkRequest,
    DomainId,
    DraftEditRequest,
    DraftTarget,
    PublicationStatus,
    PublishRequest,
    ResumeRequest,
    ReviewDecision,
    ReviewStatus,
    TeamExecutionRequest,
    WorkStatus,
    sha256_text,
)
from rfa_mas.errors import RfaError
from scripts.contract_baseline import offline_settings


class Crash(BaseException):
    """Simulated process death: not an Exception, so no handler records an outcome."""


class Authority:
    """Test-owned review ORIGINAL that outlives our process; counts accepted submissions."""

    adapter_name = "fixture-review-authority"
    simulated = True

    def __init__(self, decision: ReviewStatus = ReviewStatus.APPROVED):
        self.next_decision = decision
        self.decisions: dict[str, ReviewDecision] = {}
        self.submissions = 0
        self.queries = 0

    async def submit_draft(self, draft, **kwargs):
        self.submissions += 1
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
            decision=self.next_decision,
            safe_reason="synthetic decision",
            simulated=True,
            adapter=self.adapter_name,
        )
        self.decisions[draft.draft_id] = decision
        return decision

    async def get_decision(self, draft_id):
        self.queries += 1
        return self.decisions.get(draft_id)


async def boot(path):
    container = build_container(offline_settings(path))
    await container.startup()
    return container


def install(container, authority):
    container.service._dependencies = replace(container.service._dependencies, response=authority)
    container.service.start(container.checkpoints.saver)


def work(**overrides):
    return DirectWorkRequest(
        **{
            "query": "TRIV3 공개 트랙",
            "domain_id": DomainId.TRIV3,
            "target": DraftTarget(audience=Audience.PUBLIC),
            **overrides,
        }
    )


async def stored_draft(container, run_id, owner, version=1):
    versions = await container.repository.draft_versions(run_id, owner)
    return next(draft for draft, _ in versions if draft.version == version)


def effect(effects, kind):
    matches = [item for item in effects if item.kind == kind]
    assert len(matches) == 1, [item.operation_key for item in effects]
    return matches[0]


# -- AC1: completed effects are reused; a different payload is rejected -----------------


async def test_completed_review_submission_is_reused_in_a_fresh_container(tmp_path):
    authority = Authority(ReviewStatus.PENDING)
    first = await boot(tmp_path)
    install(first, authority)
    owner = await first.repository.local_principal()
    result = await first.service.run(work(), owner)
    assert result.status == WorkStatus.WAITING_APPROVAL
    draft = await stored_draft(first, result.run_id, owner)
    record = effect(await first.service.effects(result.run_id, owner), "review_submission")
    assert (record.state, record.outcome, record.next_action) == ("completed", "pending", "none")
    assert record.result_ref == f"review:{draft.draft_id}@v1:{draft.content_hash}"
    # The approval MIRROR is stored with the effect; the payload itself is not.
    assert ReviewDecision.model_validate(record.approval) == authority.decisions[draft.draft_id]
    assert draft.content not in json.dumps(record.model_dump(mode="json"), ensure_ascii=False)
    await first.shutdown()

    second = await boot(tmp_path)  # New repository/runtime/caches: a fresh process.
    install(second, authority)
    try:
        guard = second.service._dependencies.response
        key = record.operation_key.removeprefix("review_submission:")
        for _ in range(2):  # The same event delivered twice.
            replay = await guard.submit_draft(draft, idempotency_key=key)
            assert replay == authority.decisions[draft.draft_id]
        assert authority.submissions == 1
        changed = draft.model_copy(
            update={"content": "다른 공개 요약", "content_hash": sha256_text("다른 공개 요약")}
        )
        with pytest.raises(RfaError) as conflict:
            await guard.submit_draft(changed, idempotency_key=key)
        assert conflict.value.code == "idempotency_conflict"
        assert authority.submissions == 1
    finally:
        await second.shutdown()


async def test_publication_key_reused_for_another_run_is_rejected(tmp_path):
    container = await boot(tmp_path)
    install(container, Authority())
    try:
        owner = await container.repository.local_principal()
        first = await container.service.run(work(), owner)
        second = await container.service.run(work(), owner)
        publisher = MockPublisher()
        container.drafts._publisher = publisher
        receipt = await container.drafts.publish(
            first.run_id, PublishRequest(idempotency_key="pub-shared"), owner
        )
        assert receipt.status == PublicationStatus.SUCCEEDED
        with pytest.raises(RfaError) as conflict:
            await container.drafts.publish(
                second.run_id, PublishRequest(idempotency_key="pub-shared"), owner
            )
        assert conflict.value.code == "idempotency_conflict"
        assert publisher.calls == 1 and len(publisher.sink) == 1
        assert await container.repository.get_publication(second.run_id, owner) is None
        record = effect(await container.service.effects(first.run_id, owner), "publication")
        assert (record.state, record.outcome) == ("completed", "succeeded")
        assert record.result_ref == "publication:" + receipt.publication_id
        assert record.approval["approval_id"] == receipt.approval_id
        assert record.approval["payload_hash"] == receipt.binding.payload_hash
    finally:
        await container.shutdown()


async def test_concurrent_same_review_and_publication_requests_call_once(tmp_path):
    authority = Authority()
    container = await boot(tmp_path)
    install(container, authority)
    try:
        owner = await container.repository.local_principal()
        result = await container.service.run(work(), owner)
        await container.drafts.edit(
            result.run_id,
            DraftEditRequest(expected_version=1, content="수정한 공개 트랙 요약"),
            owner,
        )
        before = authority.submissions
        await asyncio.gather(
            container.drafts.request_review(result.run_id, owner),
            container.drafts.request_review(result.run_id, owner),
        )
        assert authority.submissions == before + 1
        publisher = MockPublisher()
        container.drafts._publisher = publisher
        receipts = await asyncio.gather(
            *(
                container.drafts.publish(
                    result.run_id, PublishRequest(idempotency_key="pub-twice"), owner
                )
                for _ in range(2)
            )
        )
        assert publisher.calls == 1 and len(publisher.sink) == 1
        assert receipts[0].publication_id == receipts[1].publication_id
        assert receipts[0].binding.version == 2
    finally:
        await container.shutdown()


# -- AC3: crash after intent / after the call, reconciled by query in a NEW process ------

FIXTURES = r'''
import asyncio, json, os, sys
from dataclasses import replace
from pathlib import Path
from scripts.contract_baseline import offline_settings
from rfa_mas.bootstrap import build_container
from rfa_mas.contracts import (Audience, DirectWorkRequest, DomainId, DraftTarget,
    ExecutionMode, PublicationReceipt, PublicationStatus, PublishRequest, ReviewDecision,
    ReviewStatus)

class Authority:
    adapter_name = "fixture-review-authority"
    simulated = True
    def __init__(self): self.decision = None
    async def submit_draft(self, draft, **kwargs):
        self.decision = ReviewDecision(request_id=draft.request_id, trace_id=draft.trace_id,
            run_id=draft.run_id, agent_id=draft.agent_id, domain_id=draft.domain_id,
            draft_id=draft.draft_id, draft_version=draft.version,
            content_hash=draft.content_hash, target=draft.target,
            decision=ReviewStatus.APPROVED, safe_reason="synthetic", simulated=True,
            adapter=self.adapter_name)
        return self.decision
    async def get_decision(self, draft_id): return self.decision

class FilePublisher:
    """Synthetic external sink (a local file) that outlives the publishing process."""
    adapter_name = "fixture-file-publisher"
    simulated = True
    mode = ExecutionMode.MOCK
    def __init__(self, sink, crash=None):
        self.sink, self.crash, self.calls = Path(sink), crash, 0
    async def authorize(self, binding):  # P1-008E: no separate publication authority.
        return None
    async def publish(self, binding, *, run_id, publication_id, approval_id, idempotency_key):
        self.calls += 1
        if self.crash == "after_intent":
            os._exit(17)  # Process dies before the effect happens.
        receipt = PublicationReceipt(publication_id=publication_id, run_id=run_id,
            idempotency_key=idempotency_key, binding=binding, approval_id=approval_id,
            status=PublicationStatus.SUCCEEDED, external_result_ref="local-artifact:file-sink",
            mode=ExecutionMode.MOCK, next_action="none")
        with self.sink.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"key": idempotency_key,
                                     "receipt": receipt.model_dump(mode="json")}) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if self.crash == "after_call":
            os._exit(17)  # Effect happened; our result was never saved.
        return receipt
    async def query(self, idempotency_key):
        if not self.sink.exists():
            return None
        for line in self.sink.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            if item["key"] == idempotency_key:
                return PublicationReceipt.model_validate(item["receipt"])
        return None
'''

CRASHING = FIXTURES + r'''
async def main():
    root, sink, crash, key = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
    instance = build_container(offline_settings(root))
    await instance.startup()
    instance.service._dependencies = replace(instance.service._dependencies,
                                             response=Authority())
    instance.service.start(instance.checkpoints.saver)
    owner = await instance.repository.local_principal()
    result = await instance.service.run(DirectWorkRequest(query="TRIV3 공개 트랙",
        domain_id=DomainId.TRIV3, target=DraftTarget(audience=Audience.PUBLIC)), owner)
    print(json.dumps({"run_id": result.run_id, "status": result.status.value}), flush=True)
    instance.drafts._publisher = FilePublisher(sink, crash)
    await instance.drafts.publish(result.run_id, PublishRequest(idempotency_key=key), owner)
    print("not-crashed", flush=True)
asyncio.run(main())
'''

RECONCILING = FIXTURES + r'''
async def main():
    root, sink, run_id, key = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
    instance = build_container(offline_settings(root))
    await instance.startup()
    try:
        owner = await instance.repository.local_principal()
        before = {e.kind: e.state for e in await instance.service.effects(run_id, owner)}
        publisher = FilePublisher(sink)
        instance.drafts._publisher = publisher
        first = await instance.drafts.publish(run_id, PublishRequest(idempotency_key=key), owner)
        second = await instance.drafts.publish(run_id, PublishRequest(idempotency_key=key), owner)
        ledger = {e.kind: e for e in await instance.service.effects(run_id, owner)}
        sink_lines = len(Path(sink).read_text().splitlines()) if Path(sink).exists() else 0
        print(json.dumps({"before": before.get("publication"), "first": first.status.value,
            "second": second.status.value, "next": second.next_action,
            "same_publication": first.publication_id == second.publication_id,
            "ref": second.external_result_ref, "calls": publisher.calls, "sink": sink_lines,
            "ledger": ledger["publication"].state,
            "ledger_next": ledger["publication"].next_action}))
    finally:
        await instance.shutdown()
asyncio.run(main())
'''


def _python(script, *args):
    return subprocess.run(
        [sys.executable, "-c", script, *map(str, args)],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
    )


@pytest.mark.parametrize(
    ("crash", "expected"),
    [
        (
            "after_intent",
            {"first": "outcome_unknown", "second": "outcome_unknown", "next": "query",
             "ref": None, "sink": 0, "ledger": "outcome_unknown", "ledger_next": "query"},
        ),
        (
            "after_call",
            {"first": "succeeded", "second": "succeeded", "next": "none",
             "ref": "local-artifact:file-sink", "sink": 1, "ledger": "completed",
             "ledger_next": "none"},
        ),
    ],
)
async def test_process_death_is_reconciled_by_query_and_never_republished(
    tmp_path, crash, expected
):
    root = tmp_path.resolve()
    sink = root / "external-sink.jsonl"
    died = await asyncio.to_thread(_python, CRASHING, root, sink, crash, "pub-crash")
    assert died.returncode == 17, died.stderr[-2000:]
    started = json.loads(died.stdout.splitlines()[0])
    assert started["status"] == "completed" and "not-crashed" not in died.stdout
    fresh = await asyncio.to_thread(
        _python, RECONCILING, root, sink, started["run_id"], "pub-crash"
    )
    assert fresh.returncode == 0, fresh.stderr[-2000:]
    observed = json.loads(fresh.stdout)
    # The dead process left a durable intent; the new process only QUERIES it.
    assert observed.pop("before") == "outcome_unknown"
    assert observed.pop("calls") == 0 and observed.pop("same_publication") is True
    assert observed == expected
    with sqlite3.connect(offline_settings(root).database_path) as db:
        assert db.execute("SELECT count(*) FROM publications").fetchone()[0] == 1


# -- Team/role effects: durable intent, restart -> unknown, never re-executed ------------


def team_work(**values):
    goal = "TRIV3 benchmark 로그 검증과 지연 비교"
    return DirectWorkRequest(
        query=goal,
        domain_id=DomainId.TRIV3,
        target=DraftTarget(audience=Audience.OWNER),
        team=TeamExecutionRequest(goal=goal, outputs=("benchmark_report",)),
        **values,
    )


async def test_team_prepare_crash_is_unknown_and_never_prepared_again(tmp_path):
    first = await boot(tmp_path)
    owner = await first.repository.local_principal()
    request = team_work(run_id="run_team_prepare_crash")
    await first.repository.create_owned_run(request, owner, session_id=None)

    async def crash(*args, **kwargs):
        raise Crash()

    first.team_factory.runtime.prepare = crash
    with pytest.raises(Crash):
        await first.team_runner.ensure_and_bind(request, request.team, owner, task_id=None)
    await first.shutdown()

    second = await boot(tmp_path)
    try:
        record = effect(await second.service.effects(request.run_id, owner), "team_prepare")
        assert (record.state, record.next_action) == ("outcome_unknown", "query")
        calls = []
        original = second.team_factory.runtime.prepare

        async def spy(*args, **kwargs):
            calls.append(True)
            return await original(*args, **kwargs)

        second.team_factory.runtime.prepare = spy
        with pytest.raises(RfaError) as blocked:
            await second.team_runner.ensure_and_bind(request, request.team, owner, task_id=None)
        assert blocked.value.code == "team_not_ready" and calls == []
    finally:
        await second.shutdown()


async def test_team_and_role_effects_are_ledgered_and_restart_role_is_unknown(tmp_path):
    container = await boot(tmp_path)
    try:
        owner = await container.repository.local_principal()
        request = team_work(run_id="run_role_restart")
        await container.repository.create_owned_run(request, owner, session_id=None)
        lifecycle = await container.team_runner.ensure_and_bind(
            request, request.team, owner, task_id=None
        )
        prepared = effect(await container.service.effects(request.run_id, owner), "team_prepare")
        assert (prepared.state, prepared.outcome) == ("completed", "ready")
        assert prepared.result_ref == f"team:{lifecycle.task.task_id}@1"
        key = f"role:{request.run_id}:paper_scout"
        member = next(m for m in lifecycle.team.spec.members if m.role == "paper_scout")
        started = await container.repository.begin_role_execution(
            request.run_id, owner, execution_key=key, role="paper_scout",
            agent_id=member.spec.agent_id,
        )
        assert started[0] == "started"
        role = effect(await container.service.effects(request.run_id, owner), "role_execution")
        assert role.state == "inflight"
        await container.repository.initialize()  # Simulated fresh process start.
        role = effect(await container.service.effects(request.run_id, owner), "role_execution")
        assert (role.state, role.next_action) == ("outcome_unknown", "query")
        with pytest.raises(RfaError) as late:
            await container.repository.finish_role_execution(
                request.run_id, owner,
                workers.RoleOutcome(role="paper_scout", agent_id=member.spec.agent_id,
                                    execution_key=key, status="succeeded", simulated=True),
            )
        assert late.value.code == "invalid_state_transition"
        [reconciled] = [
            item for item in await container.service.reconcile(request.run_id, owner)
            if item.kind == "role_execution"
        ]
        assert reconciled.method == "unsupported" and reconciled.after == "outcome_unknown"
    finally:
        await container.shutdown()


# -- AC4: cancel / permission revocation block every later call -------------------------


async def test_cancelled_waiting_run_blocks_resume_review_and_publication(tmp_path):
    authority = Authority(ReviewStatus.PENDING)
    first = await boot(tmp_path)
    install(first, authority)
    owner = await first.repository.local_principal()
    result = await first.service.run(work(), owner)
    assert result.status == WorkStatus.WAITING_APPROVAL
    assert await first.service.cancel(result.run_id, owner) == "cancelled"
    record = await first.repository.get_owned_run(result.run_id, owner)
    assert record.status == WorkStatus.CANCELLED and record.result.stop_reason == "cancelled"
    await first.shutdown()

    second = await boot(tmp_path)  # The barrier is durable across restarts.
    install(second, authority)
    try:
        queries, submissions = authority.queries, authority.submissions
        resumed = await second.service.resume(result.run_id, ResumeRequest(event_id="late"), owner)
        assert resumed.status == WorkStatus.CANCELLED
        assert authority.queries == queries  # Terminal: no graph wakeup, no authority query.
        # Even a later approval by the authority cannot publish a cancelled run.
        authority.decisions[result.draft.draft_id] = authority.decisions[
            result.draft.draft_id
        ].model_copy(update={"decision": ReviewStatus.APPROVED})
        assert (await second.drafts.state(result.run_id, owner)).approval_valid
        publisher = MockPublisher()
        second.drafts._publisher = publisher
        with pytest.raises(RfaError) as publish:
            await second.drafts.publish(result.run_id, PublishRequest(idempotency_key="p"), owner)
        assert publish.value.code == "cancelled"
        # A new version needs a new review request: blocked before any submission.
        await second.drafts.edit(
            result.run_id, DraftEditRequest(expected_version=1, content="취소 뒤 수정본"), owner
        )
        with pytest.raises(RfaError) as review:
            await second.drafts.request_review(result.run_id, owner)
        assert review.value.code == "cancelled"
        assert authority.submissions == submissions and publisher.calls == 0
        assert await second.repository.get_publication(result.run_id, owner) is None
        with pytest.raises(RfaError) as terminal:
            await second.service.cancel(result.run_id, owner)
        assert terminal.value.code == "invalid_state_transition"
    finally:
        await second.shutdown()


async def test_revoked_completed_run_is_never_published(tmp_path):
    container = await boot(tmp_path)
    install(container, Authority())
    try:
        owner = await container.repository.local_principal()
        result = await container.service.run(work(), owner)
        assert result.status == WorkStatus.COMPLETED
        assert (await container.drafts.state(result.run_id, owner)).approval_valid
        assert await container.service.revoke(result.run_id, owner) == "revoked"
        publisher = MockPublisher()
        container.drafts._publisher = publisher
        with pytest.raises(RfaError) as denied:
            await container.drafts.publish(
                result.run_id, PublishRequest(idempotency_key="after-revoke"), owner
            )
        assert denied.value.code == "permission_revoked"
        assert publisher.calls == 0 and publisher.sink == []
        assert await container.repository.get_publication(result.run_id, owner) is None
        record = await container.repository.get_owned_run(result.run_id, owner)
        assert record.status == WorkStatus.COMPLETED  # Terminal history is preserved.
    finally:
        await container.shutdown()


async def test_cancel_during_domain_work_stops_before_review_submission(tmp_path):
    authority = Authority()
    container = await boot(tmp_path)
    install(container, authority)
    try:
        owner = await container.repository.local_principal()
        handler = container.runtime._handlers["domain_task"]
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow(spec, request):
            entered.set()
            await release.wait()
            return await handler(spec, request)

        container.runtime.register("domain_task", slow)
        request = work()
        task = asyncio.create_task(container.service.run(request, owner))
        await asyncio.wait_for(entered.wait(), 10)
        assert await container.service.cancel(request.run_id, owner) == "cancelling"
        release.set()
        result = await asyncio.wait_for(task, 10)
        assert result.status == WorkStatus.CANCELLED and result.stop_reason == "cancelled"
        assert authority.submissions == 0
        effects = await container.service.effects(request.run_id, owner)
        assert not [item for item in effects if item.kind == "review_submission"]
    finally:
        await container.shutdown()


async def test_ledger_rows_are_owner_scoped(tmp_path, principal):
    container = await boot(tmp_path)
    install(container, Authority())
    try:
        owner = await container.repository.local_principal()
        result = await container.service.run(work(), owner)
        assert await container.service.effects(result.run_id, owner)
        with pytest.raises(RfaError) as hidden:
            await container.service.effects(result.run_id, principal)
        assert hidden.value.code == "not_found"
        with pytest.raises(RfaError) as barrier:
            await container.service.revoke(result.run_id, principal)
        assert barrier.value.code == "not_found"
        assert await container.repository.run_barrier(result.run_id, owner) is None
    finally:
        await container.shutdown()
