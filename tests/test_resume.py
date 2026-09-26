from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
import subprocess
import sys
from dataclasses import replace

import httpx
import pytest

from rfa_mas.adapters.checkpoints import SqliteCheckpoints
from rfa_mas.adapters.local import SqliteWorkRepository, _sqlite_setup_guard
from rfa_mas.api.app import create_app, resolve_principal
from rfa_mas.bootstrap import build_container
from rfa_mas.contracts import (
    Audience,
    DirectWorkRequest,
    DomainId,
    DraftTarget,
    ResumeRequest,
    ReviewDecision,
    ReviewStatus,
    TrustedPrincipal,
    WorkStatus,
)
from rfa_mas.errors import RfaError
from scripts.contract_baseline import offline_settings


class ReviewAuthority:
    """Test-owned simulated approval ORIGINAL, separate from core/checkpoint state."""

    adapter_name = "fixture-review-authority"
    simulated = True

    def __init__(self, decision: ReviewDecision | None = None):
        self.decision = decision
        self.submissions = 0
        self.queries = 0

    async def submit_draft(self, draft, **kwargs):
        self.submissions += 1
        self.decision = ReviewDecision(
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
            safe_reason="Synthetic review pending",
            simulated=True,
            adapter=self.adapter_name,
        )
        return self.decision

    async def get_decision(self, draft_id):
        self.queries += 1
        assert self.decision is None or self.decision.draft_id == draft_id
        return self.decision


def install_authority(container, authority):
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


async def pending(container, principal=None, **overrides):
    authority = ReviewAuthority()
    install_authority(container, authority)
    principal = principal or await container.repository.local_principal()
    result = await container.service.run(work(**overrides), principal)
    assert result.status == WorkStatus.WAITING_APPROVAL, result.errors
    record = await container.repository.get_owned_run(result.run_id, principal)
    return authority, principal, result, record


async def test_real_sqlite_interrupt_resumes_in_fresh_process_without_resubmit(tmp_path):
    container = build_container(offline_settings(tmp_path))
    await container.startup()
    authority, owner, result, record = await pending(container)
    snapshot = await container.service._graph.aget_state(
        container.service._config(record.thread_id)
    )
    assert snapshot.next == ("await_review",) and snapshot.interrupts
    assert "principal" not in snapshot.values
    decision = authority.decision.model_copy(update={"decision": ReviewStatus.APPROVED})
    await container.shutdown()
    # JSON argument is strictly synthetic authoritative fixture data, not a key.
    script = """
import asyncio, json, sys
from pathlib import Path
from scripts.contract_baseline import offline_settings
from rfa_mas.bootstrap import build_container
from rfa_mas.contracts import ReviewDecision, ResumeRequest
from dataclasses import replace
class Authority:
    def __init__(self): self.queries = 0
    async def submit_draft(self, *args, **kwargs):
        raise AssertionError('resume must not submit')
    async def get_decision(self, draft_id):
        self.queries += 1
        decision = ReviewDecision.model_validate_json(sys.argv[3])
        assert decision.draft_id == draft_id
        return decision
async def main():
    instance = build_container(offline_settings(Path(sys.argv[1])))
    await instance.startup()
    try:
        authority = Authority()
        instance.service._dependencies = replace(instance.service._dependencies, response=authority)
        instance.service.start(instance.checkpoints.saver)
        async def forbidden(*args, **kwargs): raise AssertionError('no worker/model replay')
        instance.runtime.run = forbidden
        instance.model.generate = forbidden
        owner = await instance.repository.local_principal()
        wakeup = ResumeRequest(event_id='fresh-process')
        result = await instance.service.resume(sys.argv[2], wakeup, owner)
        replay = await instance.service.resume(sys.argv[2], wakeup, owner)
        detail = await instance.repository.get_owned_run(result.run_id, owner)
        print(json.dumps({'status': result.status.value, 'run': result.run_id,
                          'queries': authority.queries, 'replay_equal': result == replay,
                          'thread': detail.thread_id, 'owner': owner.user_id}))
    finally: await instance.shutdown()
asyncio.run(main())
"""
    process = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", script, str(tmp_path), result.run_id, decision.model_dump_json()],
        capture_output=True,
        text=True,
        timeout=20,
        env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert process.returncode == 0, process.stderr
    observed = json.loads(process.stdout)
    assert observed == {
        "status": "completed",
        "run": result.run_id,
        "queries": 1,
        "replay_equal": True,
        "thread": record.thread_id,
        "owner": owner.user_id,
    }
    assert authority.submissions == 1


async def test_pending_refresh_never_approves_and_never_submits_again(container):
    authority, owner, result, record = await pending(container)
    for _ in range(2):
        resumed = await container.service.resume(
            result.run_id, ResumeRequest(event_id="same"), owner
        )
        assert resumed.status == WorkStatus.WAITING_APPROVAL
    assert authority.submissions == 1 and authority.queries == 2
    with pytest.raises(RfaError) as error:
        await container.service.run(work(session_id=record.session_id), owner)
    assert error.value.code == "thread_busy"
    authority.decision = authority.decision.model_copy(update={"decision": ReviewStatus.REJECTED})
    rejected = await container.service.resume(
        result.run_id, ResumeRequest(event_id="reject"), owner
    )
    assert rejected.status == WorkStatus.FAILED


async def test_unknown_authority_stays_pending_after_container_restart(tmp_path):
    first = build_container(offline_settings(tmp_path))
    await first.startup()
    _, owner, result, record = await pending(first)
    await first.shutdown()
    second = build_container(offline_settings(tmp_path))
    await second.startup()
    try:
        resumed = await second.service.resume(
            result.run_id, ResumeRequest(event_id="unknown"), owner
        )
        assert resumed.status == WorkStatus.WAITING_APPROVAL
        with pytest.raises(RfaError) as error:
            await second.service.run(work(session_id=record.session_id), owner)
        assert error.value.code == "thread_busy"
    finally:
        await second.shutdown()


async def test_review_query_timeout_stays_resumable_and_later_clears_error(container):
    authority, owner, result, _ = await pending(container)
    original = authority.get_decision

    async def timeout(draft_id):
        raise RfaError("upstream_timeout", "synthetic safe timeout", retryable=True)

    authority.get_decision = timeout
    timed_out = await container.service.resume(
        result.run_id, ResumeRequest(event_id="timeout"), owner
    )
    assert timed_out.status == WorkStatus.WAITING_APPROVAL
    assert timed_out.errors[0].code == "review_query_pending"
    authority.get_decision = original
    authority.decision = authority.decision.model_copy(update={"decision": ReviewStatus.APPROVED})
    complete = await container.service.resume(result.run_id, ResumeRequest(event_id="retry"), owner)
    assert complete.status == WorkStatus.COMPLETED and not complete.errors
    assert authority.submissions == 1 and authority.queries == 1


async def test_resume_authorizes_before_checkpoint_lookup_and_rejects_approval_claims(container):
    authority, owner, result, _ = await pending(container)
    app = create_app(container=container)
    identity = TrustedPrincipal(user_id="other-owner", authenticated=True)

    async def current():
        return identity

    app.dependency_overrides[resolve_principal] = current
    original = container.service._graph.aget_state
    lookups = []

    async def spy(config, **kwargs):
        lookups.append(config)
        return await original(config, **kwargs)

    container.service._graph.aget_state = spy
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        existing = await api.post(f"/v1/runs/{result.run_id}/resume", json={"event_id": "probe"})
        absent = await api.post("/v1/runs/absent/resume", json={"event_id": "probe"})
        assert existing.status_code == absent.status_code == 404
        assert existing.json() == absent.json() and not lookups
        identity = owner
        for extra in ({"approved": True}, {"thread_id": "forged"}, {"principal": {}}):
            rejected = await api.post(
                f"/v1/runs/{result.run_id}/resume", json={"event_id": "probe", **extra}
            )
            assert rejected.status_code == 422
        assert not lookups and authority.queries == 0


@pytest.mark.parametrize("change", ["delete", "revision", "content", "audience", "policy"])
async def test_changed_source_fails_closed_without_exposing_old_draft(container, change):
    authority, owner, result, _ = await pending(container)
    reference = result.draft.allowed_evidence[0]
    documents = await container.repository.list_documents(DomainId.TRIV3.value)
    document = next(item for item in documents if item.source_id == reference.source_id)
    if change == "delete":
        with sqlite3.connect(container.repository.path) as connection:
            connection.execute(
                "DELETE FROM kb_documents WHERE source_id = ?", (document.source_id,)
            )
    else:
        updates = {
            "revision": {"source_revision": "new-revision"},
            "content": {"content": "SYNTHETIC_SOURCE_REPLACEMENT"},
            "audience": {"audience": Audience.PRIVATE, "owner_id": "someone-else"},
            "policy": {"policy_version": "new-policy"},
        }[change]
        if change == "revision":
            # A second opaque legacy revision is ambiguous, never sorted into a head.
            await container.repository.upsert_documents([document.model_copy(update=updates)])
        else:
            # Explicit test-only corruption of the historical source bypasses storage
            # invariants to exercise resume's independent integrity/ACL checks.
            with sqlite3.connect(container.repository.path) as connection:
                connection.execute(
                    "UPDATE kb_documents SET document_json=? "
                    "WHERE source_id=? AND source_revision=?",
                    (
                        document.model_copy(update=updates).model_dump_json(),
                        document.source_id,
                        document.source_revision,
                    ),
                )
    authority.decision = authority.decision.model_copy(update={"decision": ReviewStatus.APPROVED})
    resumed = await container.service.resume(
        result.run_id, ResumeRequest(event_id="changed"), owner
    )
    assert resumed.status == WorkStatus.FAILED
    assert resumed.draft is None and resumed.review is None
    assert document.content not in resumed.model_dump_json()
    assert authority.queries == 0 and authority.submissions == 1


@pytest.mark.parametrize("change", ["membership", "other_company", "missing_company"])
async def test_fresh_principal_and_company_scoped_bu_required_on_resume(container, change):
    owner = TrustedPrincipal(
        user_id="fixture-owner-001",
        authenticated=True,
        company_id="company-a",
        business_units=frozenset({"triv3-team"}),
    )
    documents = await container.repository.list_documents(DomainId.TRIV3.value)
    # Use a single synthetic BU source so generated draft necessarily binds it.
    with sqlite3.connect(container.repository.path) as connection:
        connection.execute("DELETE FROM kb_documents")
    document = next(item for item in documents if item.audience == Audience.BUSINESS_UNIT)
    document = document.model_copy(update={"company_id": "company-a", "content": "TRIV3 BU CANARY"})
    # Test-only historical fixture construction, not a second product write API.
    with sqlite3.connect(container.repository.path) as connection:
        connection.execute(
            "INSERT INTO kb_documents VALUES (?, ?, ?, ?, ?)",
            (
                document.source_id,
                document.source_revision,
                document.domain_id.value,
                document.model_dump_json(),
                "2026-09-26T00:00:00+00:00",
            ),
        )
    authority, _, result, _ = await pending(
        container, owner, target=DraftTarget(audience=Audience.BUSINESS_UNIT)
    )
    if change == "membership":
        fresh = owner.model_copy(update={"business_units": frozenset()})
    elif change == "other_company":
        fresh = owner.model_copy(update={"company_id": "company-b"})
    else:
        fresh = owner
        # Test-only corruption verifies missing company metadata still fails closed.
        with sqlite3.connect(container.repository.path) as connection:
            connection.execute(
                "UPDATE kb_documents SET document_json=? WHERE source_id=?",
                (
                    document.model_copy(update={"company_id": None}).model_dump_json(),
                    document.source_id,
                ),
            )
    authority.decision = authority.decision.model_copy(update={"decision": ReviewStatus.APPROVED})
    resumed = await container.service.resume(
        result.run_id, ResumeRequest(event_id="revoked"), fresh
    )
    assert resumed.status == WorkStatus.FAILED and resumed.draft is None
    assert "TRIV3 BU CANARY" not in resumed.model_dump_json()
    assert authority.queries == 0


async def test_policy_change_and_wrong_approval_binding_cannot_complete(container):
    authority, owner, result, _ = await pending(container)
    authority.decision = authority.decision.model_copy(
        update={"decision": ReviewStatus.APPROVED, "content_hash": "0" * 64}
    )
    mismatch = await container.service.resume(
        result.run_id, ResumeRequest(event_id="wrong-hash"), owner
    )
    assert mismatch.status == WorkStatus.FAILED
    assert mismatch.errors[0].code == "approval_binding_mismatch"
    authority, owner, result, _ = await pending(container)
    container.policy.policy_version = "policy-v2"
    changed = await container.service.resume(result.run_id, ResumeRequest(event_id="policy"), owner)
    assert changed.status == WorkStatus.FAILED and changed.draft is None
    assert authority.queries == 0


async def test_same_host_thread_lock_rejects_other_instance_and_process(tmp_path):
    first, second = [SqliteCheckpoints(tmp_path / "checkpoints.sqlite") for _ in range(2)]
    await first.start()
    await second.start()
    try:
        with first.guard("same-thread"):
            with pytest.raises(RfaError) as error, second.guard("same-thread"):
                pytest.fail("concurrent guard must fail")
            assert error.value.code == "thread_busy"
            with second.guard("different-thread"):
                pass
            script = """
import sys
from pathlib import Path
from rfa_mas.adapters.checkpoints import SqliteCheckpoints
from rfa_mas.errors import RfaError
try:
    with SqliteCheckpoints(Path(sys.argv[1])).guard('same-thread'): print('unsafe')
except RfaError as exc: print(exc.code)
"""
            process = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-c", script, str(first.path)],
                capture_output=True,
                text=True,
                timeout=10,
                env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            assert process.returncode == 0 and process.stdout.strip() == "thread_busy"
        with second.guard("same-thread"):
            pass
    finally:
        await first.close()
        await second.close()


async def test_concurrent_invocation_is_rejected_before_second_worker(container):
    owner = await container.repository.local_principal()
    session = await container.service.sessions.create(owner)
    original = container.runtime.run
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def hold(spec, request):
        calls.append(request.run_id)
        started.set()
        await release.wait()
        return await original(spec, request)

    container.runtime.run = hold
    first = asyncio.create_task(container.service.run(work(session_id=session.session_id), owner))
    try:
        await asyncio.wait_for(started.wait(), 5)
        with pytest.raises(RfaError) as error:
            await container.service.run(work(session_id=session.session_id), owner)
        assert error.value.code == "thread_busy" and len(calls) == 1
    finally:
        release.set()
        await first
    assert len((await container.service.sessions.get(session.session_id, owner)).runs) == 1


async def test_new_turn_clears_previous_failure_and_draft_state(container):
    owner = await container.repository.local_principal()
    session = await container.service.sessions.create(owner)
    bad = await container.service.run(
        work(session_id=session.session_id, domain_id=None, query="hello"), owner
    )
    assert bad.status == WorkStatus.FAILED and bad.draft is None
    good = await container.service.run(work(session_id=session.session_id), owner)
    assert good.status == WorkStatus.COMPLETED and good.draft is not None
    another_bad = await container.service.run(
        work(session_id=session.session_id, domain_id=None, query="hello"), owner
    )
    assert another_bad.status == WorkStatus.FAILED
    assert another_bad.draft is None and another_bad.review is None


async def test_restart_seed_marker_preserves_mutation_and_all_deletions(tmp_path):
    first = build_container(offline_settings(tmp_path))
    await first.startup()
    from rfa_mas.contracts import KnowledgeWrite

    owner = await first.repository.local_principal()
    original = KnowledgeWrite(
        domain_id=DomainId.TRIV3,
        provenance={"provider": "note", "namespace": "seed-regression", "external_id": "one"},
        provider_revision="initial",
        title="Synthetic restart source",
        content="Private revision",
        acl={"audience": "public"},
        synthetic=True,
    )
    receipt = await first.knowledge.write(original, owner)
    changed = (
        await first.knowledge.write(
            original.model_copy(
                update={
                    "provider_revision": "revoked",
                    "expected_revision": receipt.document.source_revision,
                    "acl": original.acl.model_copy(update={"audience": Audience.PRIVATE}),
                }
            ),
            owner,
        )
    ).document
    await first.shutdown()
    second = build_container(offline_settings(tmp_path))
    await second.startup()
    try:
        found = await second.repository.list_documents(DomainId.TRIV3.value)
        assert next(item for item in found if item.source_id == changed.source_id) == changed
        with sqlite3.connect(second.repository.path) as connection:
            connection.execute("DELETE FROM kb_documents")
    finally:
        await second.shutdown()
    third = build_container(offline_settings(tmp_path))
    await third.startup()
    try:
        assert await third.repository.list_documents(DomainId.TRIV3.value) == []
        with sqlite3.connect(third.repository.path) as connection:
            assert connection.execute("SELECT count(*) FROM installation_seeds").fetchone()[0] == 1
    finally:
        await third.shutdown()


async def test_upgrade_of_empty_legacy_store_does_not_restore_deleted_fixtures(tmp_path):
    settings = offline_settings(tmp_path)
    # The original schema existed but all knowledge had already been deleted.
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute("""CREATE TABLE runs (
            run_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, trace_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
            request_json TEXT NOT NULL, result_json TEXT, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL)""")
    instance = build_container(settings)
    await instance.startup()
    try:
        assert await instance.repository.list_documents(DomainId.TRIV3.value) == []
    finally:
        await instance.shutdown()


async def test_two_fresh_container_startups_preserve_single_fixture_seed(tmp_path):
    first, second = [build_container(offline_settings(tmp_path)) for _ in range(2)]
    try:
        await asyncio.gather(first.startup(), second.startup())
        first_docs = await first.repository.list_documents(DomainId.TRIV3.value)
        assert first_docs and first_docs == await second.repository.list_documents(
            DomainId.TRIV3.value
        )
        with sqlite3.connect(first.repository.path) as connection:
            assert connection.execute("SELECT count(*) FROM installation_seeds").fetchone()[0] == 1
    finally:
        await first.shutdown()
        await second.shutdown()


async def test_repository_wal_activation_is_setup_only_and_checks_actual_mode(
    tmp_path, monkeypatch
):
    original_connect = sqlite3.connect
    statements = []

    def traced_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    repository = SqliteWorkRepository(tmp_path / "repository.sqlite")
    await repository.initialize()
    assert sum(sql == "PRAGMA journal_mode=WAL" for sql in statements) == 1
    statements.clear()
    owner = await repository.local_principal()
    await repository.create_session(owner)
    assert not any("journal_mode" in sql for sql in statements)
    with original_connect(repository.path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    class RefusedWal(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == "PRAGMA journal_mode=WAL":
                return super().execute("SELECT 'delete'")
            return super().execute(sql, *args, **kwargs)

    monkeypatch.setattr(
        sqlite3,
        "connect",
        lambda *args, **kwargs: original_connect(*args, factory=RefusedWal, **kwargs),
    )
    refused = SqliteWorkRepository(tmp_path / "refused.sqlite")
    with pytest.raises(RfaError) as error:
        await refused.initialize()
    assert error.value.code == "configuration_error"
    with original_connect(refused.path) as connection:
        assert not connection.execute("SELECT name FROM sqlite_master WHERE name='runs'").fetchall()
    with _sqlite_setup_guard(refused.path, blocking=False):
        pass  # Refused mode cannot leak the setup reservation.


@pytest.mark.parametrize("target", ["repository", "checkpoint"])
async def test_two_process_startups_coordinate_before_wal_and_keep_one_seed(tmp_path, target):
    settings = offline_settings(tmp_path)
    path = settings.database_path if target == "repository" else settings.resolved_checkpoint_path
    script = r"""
import asyncio, fcntl, json, sys
from pathlib import Path
from rfa_mas.bootstrap import build_container
from scripts.contract_baseline import offline_settings
original = fcntl.flock
def observed(fd, operation):
    if operation & fcntl.LOCK_EX:
        try:
            original(fd, operation | fcntl.LOCK_NB)
        except BlockingIOError:
            print("contended", flush=True)
            if operation & fcntl.LOCK_NB:
                raise
            original(fd, operation)
    else:
        original(fd, operation)
fcntl.flock = observed
async def main():
    instance = build_container(offline_settings(Path(sys.argv[1])))
    try:
        await instance.startup()
        owner = await instance.repository.local_principal()
        print(json.dumps({"ready": instance.ready, "owner": owner.user_id}), flush=True)
    finally:
        await instance.shutdown()
asyncio.run(main())
"""
    processes = []
    try:
        with _sqlite_setup_guard(path):
            for _ in range(2):
                processes.append(
                    await asyncio.to_thread(
                        subprocess.Popen,
                        [sys.executable, "-c", script, str(tmp_path)],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
                    )
                )
            # An observed failed nonblocking acquisition proves both children
            # reached the actual shared boundary, not merely a timing assertion.
            for process in processes:
                line = await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), 10)
                assert line.strip() == "contended"
        outputs = await asyncio.gather(
            *(asyncio.to_thread(process.communicate, timeout=10) for process in processes)
        )
        owners = []
        for process, (output, stderr) in zip(processes, outputs, strict=True):
            assert process.returncode == 0, stderr
            result = json.loads(output.strip().splitlines()[-1])
            assert result["ready"] is True
            owners.append(result["owner"])
        assert owners[0] == owners[1]
        with sqlite3.connect(settings.database_path) as connection:
            assert connection.execute("SELECT count(*) FROM installation_seeds").fetchone()[0] == 1
            assert connection.execute("SELECT count(*) FROM local_identity").fetchone()[0] == 1
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        with sqlite3.connect(settings.resolved_checkpoint_path) as connection:
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            await asyncio.to_thread(process.communicate, timeout=10)


async def test_checkpoint_setup_wait_is_bounded_and_cancel_safe(tmp_path, monkeypatch):
    checkpoints = SqliteCheckpoints(tmp_path / "checkpoint.sqlite")
    with _sqlite_setup_guard(checkpoints.path):
        monkeypatch.setattr(checkpoints, "_setup_timeout_seconds", 0.0)
        with pytest.raises(RfaError) as error:
            await checkpoints.start()
        assert error.value.code == "storage_busy" and checkpoints.saver is None
        monkeypatch.setattr(checkpoints, "_setup_timeout_seconds", 5.0)
        pending_start = asyncio.create_task(checkpoints.start())
        await asyncio.sleep(0)  # Let acquisition encounter the held lock; no real-time wait.
        pending_start.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending_start
    await checkpoints.start()
    # Setup lock is not held for the lifetime of the open saver.
    with _sqlite_setup_guard(checkpoints.path, blocking=False):
        pass
    await checkpoints.close()


async def test_checkpoint_refused_wal_releases_setup_and_connection(tmp_path, monkeypatch):
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    async def unavailable_wal(self, config):
        return None  # Simulate a saver/provider which did not actually activate WAL.

    monkeypatch.setattr(AsyncSqliteSaver, "aget_tuple", unavailable_wal)
    checkpoints = SqliteCheckpoints(tmp_path / "checkpoint.sqlite")
    with pytest.raises(RfaError) as error:
        await checkpoints.start()
    assert error.value.code == "configuration_error"
    assert checkpoints.saver is None and checkpoints._stack is None
    with _sqlite_setup_guard(checkpoints.path, blocking=False):
        pass


def test_setup_lock_rejects_symlink_and_nonregular_files(tmp_path):
    path = tmp_path / "repository.sqlite"
    lock = tmp_path / "repository.sqlite.setup.lock"
    target = tmp_path / "untouched"
    target.write_text("synthetic unchanged")
    lock.symlink_to(target)
    with pytest.raises(OSError), _sqlite_setup_guard(path):
        pytest.fail("symlink lock accepted")
    assert target.read_text() == "synthetic unchanged"
    lock.unlink()
    os.mkfifo(lock)
    with pytest.raises(RfaError, match="일반 파일"), _sqlite_setup_guard(path):
        pytest.fail("FIFO lock accepted")


def test_repository_setup_wait_is_bounded_without_replaying_sql(tmp_path):
    path = tmp_path / "repository.sqlite"
    with _sqlite_setup_guard(path):
        with pytest.raises(RfaError) as error, _sqlite_setup_guard(path, timeout_seconds=0):
            pytest.fail("held initialization lock accepted")
        assert error.value.code == "storage_busy"
    with _sqlite_setup_guard(path, blocking=False):
        pass


@pytest.mark.parametrize("outcome", ["cancel", "error"])
async def test_checkpoint_failed_setup_drains_connection_before_releasing_lock(
    tmp_path, monkeypatch, outcome
):
    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    checkpoints = SqliteCheckpoints(tmp_path / "checkpoint.sqlite")
    entered = asyncio.Event()
    close_observations = []
    original_get = AsyncSqliteSaver.aget_tuple
    original_close = aiosqlite.Connection.close

    async def observed_setup(self, config):
        await original_get(self, config)  # Start and actually initialize the SQLite worker.
        entered.set()
        if outcome == "error":
            raise RuntimeError("synthetic setup failure")
        await asyncio.Event().wait()

    async def observed_close(self):
        # The setup lock must remain held until queued SQLite operations drain.
        with pytest.raises(BlockingIOError), _sqlite_setup_guard(checkpoints.path, blocking=False):
            pytest.fail("setup reservation released before close")
        await original_close(self)
        close_observations.append(True)

    monkeypatch.setattr(AsyncSqliteSaver, "aget_tuple", observed_setup)
    monkeypatch.setattr(aiosqlite.Connection, "close", observed_close)
    task = asyncio.create_task(checkpoints.start())
    await asyncio.wait_for(entered.wait(), 5)
    if outcome == "cancel":
        task.cancel()
    with pytest.raises(asyncio.CancelledError if outcome == "cancel" else RuntimeError):
        await task
    assert close_observations == [True]
    assert checkpoints.saver is None and checkpoints._stack is None
    with _sqlite_setup_guard(checkpoints.path, blocking=False):
        pass


async def test_credentials_and_historical_principal_are_not_checkpointed(tmp_path):
    marker = "SYNTHETIC_CREDENTIAL_DO_NOT_PERSIST_7139"
    instance = build_container(
        offline_settings(tmp_path, app_api_key=marker, nvidia_api_key=marker)
    )
    await instance.startup()
    try:
        _, _, _, record = await pending(instance)
        checkpoint = await instance.checkpoints.saver.aget_tuple(
            instance.service._config(record.thread_id)
        )
        assert "principal" not in checkpoint.checkpoint["channel_values"]
        assert marker not in repr(checkpoint)
        assert "authorization" not in repr(checkpoint).lower()
        assert "business_units" not in repr(checkpoint)
        assert "roles" not in repr(checkpoint)
        assert instance.checkpoints.saver.serde.pickle_fallback is False
        assert instance.checkpoints.saver.serde._allowed_msgpack_modules is not True
        for path in tmp_path.glob("*.checkpoints.sqlite*"):
            if path.is_file():
                assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        await instance.shutdown()
    for path in tmp_path.glob("*.checkpoints.sqlite*"):
        if path.is_file():
            assert marker.encode() not in path.read_bytes()


def test_checkpoint_default_isolated_per_application_database(tmp_path):
    first, second = offline_settings(tmp_path / "a"), offline_settings(tmp_path / "b")
    assert first.resolved_checkpoint_path != second.resolved_checkpoint_path
    assert first.resolved_checkpoint_path.parent == first.database_path.parent
    assert offline_settings(tmp_path, checkpoint_path="").checkpoint_path is None
