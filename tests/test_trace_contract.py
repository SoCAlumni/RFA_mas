"""Actual local observations; all data and approval authority here are synthetic."""

from __future__ import annotations

import asyncio
import json
import stat
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.callbacks.manager import CallbackManager

from rfa_mas.adapters.local import LocalJsonlTrace
from rfa_mas.api.app import create_app
from rfa_mas.application.observations import ObservedPort
from rfa_mas.bootstrap import build_container
from rfa_mas.contracts import (
    Audience,
    DomainId,
    DraftTarget,
    ResultStatus,
    ResumeRequest,
    ReviewDecision,
    ReviewStatus,
    StructuredError,
    TaskResult,
    TrustedPrincipal,
    WorkRequest,
    WorkStatus,
    new_id,
)
from rfa_mas.errors import OutcomeUnknownError, RfaError
from rfa_mas.reference.app import create_reference_contract_app
from rfa_mas.security import SecretRedactor
from scripts.contract_baseline import offline_settings

CANARY = "PRIVATE_CANARY_Q7"


def work(**extra):
    return WorkRequest(
        **{
            "query": "TRIV3 공개 트랙",
            "domain_id": DomainId.TRIV3,
            "target": DraftTarget(audience=Audience.PUBLIC),
            **extra,
        }
    )


def exports(container):
    return sorted((container.settings.trace_dir / "rfa-observations-v1").glob("events-*.jsonl"))


async def test_actual_calls_aliases_allowlist_and_unknown_coverage(container, principal):
    request = work(request_id=CANARY, trace_id=CANARY, agent_id=CANARY, query=f"TRIV3 {CANARY}")
    result = await container.service.run(request, principal)
    assert result.status == WorkStatus.COMPLETED, result.errors
    assert result.request_id == result.trace_id == CANARY  # successful 1.0 correlation unchanged
    ledger = await container.service.observations.ledger(result.run_id, principal)
    raw = "".join(path.read_text() for path in exports(container))
    assert raw and CANARY not in raw and principal.user_id not in raw
    assert result.run_id not in raw and result.draft.content not in raw
    assert ledger.execution.run_id != result.run_id
    rows = ledger.observations
    assert [r.sequence for r in rows] == list(range(1, len(rows) + 1))
    assert len({r.observation_id for r in rows}) == len(rows)
    assert len({r.event.execution.agent_id for r in rows}) == 2
    assert all(r.event.input_tokens is r.event.output_tokens is None for r in rows)
    assert all(
        r.event.approval_id is r.event.policy_decision_id is r.event.publication_id is None
        for r in rows
    )
    assert rows[-1].event.draft_id is not None and rows[-1].event.draft_id != result.draft.draft_id
    assert any(r.event.versions.sources for r in rows)
    coverage = {item.boundary: item for item in ledger.coverage}
    for name in ("request", "model", "retrieval", "policy", "runtime", "approval"):
        assert coverage[name].state == "collected" and coverage[name].calls >= 1
    for name in ("tool", "publish", "internal_nodes", "test_sink"):
        assert coverage[name].state == "uncollected" and coverage[name].calls is None
    completed = [r for r in rows if r.transport == "returned"]
    assert completed and all(r.event.duration_ms >= 0 for r in completed)


class Authority:
    adapter_name = "synthetic-approval-original"
    simulated = True

    def __init__(self, decision=None):
        self.decision, self.submissions, self.queries = decision, 0, 0

    async def submit_draft(self, draft, **kwargs):
        self.submissions += 1
        self.decision = ReviewDecision(
            **{
                key: getattr(draft, key)
                for key in ("request_id", "trace_id", "run_id", "agent_id", "domain_id")
            },
            draft_id=draft.draft_id,
            draft_version=draft.version,
            content_hash=draft.content_hash,
            target=draft.target,
            decision=ReviewStatus.PENDING,
            safe_reason=CANARY,
            simulated=True,
            adapter=self.adapter_name,
        )
        return self.decision

    async def get_decision(self, draft_id):
        self.queries += 1
        assert draft_id == self.decision.draft_id
        return self.decision


def install_authority(container, authority):
    container.service._dependencies = replace(
        container.service._dependencies,
        response=ObservedPort(authority, container.service.observations, "approval", mode="mock"),
    )
    container.service.start(container.checkpoints.saver)


async def test_restart_resume_alias_sequence_and_native_ambient_guard(tmp_path, monkeypatch):
    created = []

    def fake_tracer(*args, **kwargs):
        created.append(True)
        return BaseCallbackHandler()

    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setattr("langchain_core.tracers.langchain.LangChainTracer", fake_tracer)
    CallbackManager.configure()  # positive control: fake tracer is reachable without guard
    assert len(created) == 1
    created.clear()
    first = build_container(offline_settings(tmp_path))
    await first.startup()
    try:
        owner = await first.repository.local_principal()
        authority = Authority()
        install_authority(first, authority)
        result = await first.service.run(work(), owner)
        assert result.status == WorkStatus.WAITING_APPROVAL
        old = await first.service.observations.ledger(result.run_id, owner)
        assert any(
            r.transport == "returned" and r.event.status == "waiting" for r in old.observations
        )
        decision = authority.decision.model_copy(update={"decision": ReviewStatus.APPROVED})
    finally:
        await first.shutdown()
    second = build_container(offline_settings(tmp_path))
    await second.startup()
    try:
        authority = Authority(decision)
        install_authority(second, authority)
        resumed = await second.service.resume(result.run_id, ResumeRequest(event_id="wake"), owner)
        assert resumed.status == WorkStatus.COMPLETED
        ledger = await second.service.observations.ledger(result.run_id, owner)
        assert ledger.execution == old.execution
        assert ledger.observations[: len(old.observations)] == old.observations
        assert ledger.observations[-1].sequence > old.observations[-1].sequence
        assert authority.submissions == 0 and authority.queries == 1
        assert not created  # actual native run + native checkpoint resume, not SDK-only
        assert CANARY not in "".join(p.read_text() for p in exports(second))
    finally:
        await second.shutdown()


async def test_observation_authorization_and_forgeries(container, principal):
    result = await container.service.run(work(), principal)
    observer = container.service.observations
    ledger = await observer.ledger(result.run_id, principal)
    attacker = TrustedPrincipal(user_id="outsider", authenticated=True)
    with pytest.raises(RfaError):
        await observer.ledger(result.run_id, attacker)
    row = ledger.observations[0]
    with pytest.raises(RfaError):
        await container.trace.emit_event(row.event)
    with pytest.raises(RfaError):
        await container.trace.emit_observation(row.model_copy(update={"provider_ref": CANARY}))
    with pytest.raises(RfaError):
        await container.repository.append_observation(
            result.run_id,
            principal,
            row.event.model_copy(
                update={"execution": row.event.execution.model_copy(update={"trace_id": CANARY})}
            ),
            origin="port",
            provider_ref=row.provider_ref,
        )
    with pytest.raises(ValueError):
        await observer.record_test_sink(received="yes")


async def test_actual_sink_is_not_tool_or_publication_coverage(container, principal):
    result = await container.service.run(work(), principal)
    observer = container.service.observations
    received = []

    async def local_sink(payload):
        received.append(payload)  # actual synthetic write; payload is never exported
        await observer.record_test_sink(received=True)

    async with observer.scope(result.run_id, principal):
        await local_sink({"synthetic_private": CANARY})
    ledger = await observer.ledger(result.run_id, principal)
    coverage = {item.boundary: item for item in ledger.coverage}
    assert len(received) == coverage["test_sink"].calls == 1
    assert coverage["test_sink"].state == "collected"
    assert coverage["tool"].calls is coverage["publish"].calls is None
    assert CANARY not in ledger.model_dump_json()


async def test_failure_and_policy_version_are_observed_without_provider_text(container, principal):
    async def fail(request):
        raise TimeoutError(CANARY)

    container.model.generate = fail
    result = await container.service.run(
        work(request_id=CANARY, trace_id=CANARY, agent_id=CANARY), principal
    )
    assert result.status == WorkStatus.FAILED
    assert CANARY not in result.model_dump_json()
    ledger = await container.service.observations.ledger(result.run_id, principal)
    rows = [r for r in ledger.observations if r.event.event == "model" and r.transport == "raised"]
    assert len(rows) == 1 and rows[0].event.reason_code == "timeout"
    assert rows[0].event.duration_ms >= 0
    old_policy = ledger.observations[-1].event.versions.policy
    container.policy.policy_version = "synthetic-policy-v2-" + CANARY
    async with container.service.observations.scope(result.run_id, principal):
        record = await container.service.observations.record(
            "policy", "denied", transport="returned"
        )
    assert record.event.versions.policy != old_policy
    assert CANARY not in record.model_dump_json()


async def test_returned_graph_error_projection_preserves_owned_run(container, principal):
    class ErrorGraph:
        async def ainvoke(self, *args, **kwargs):
            return {
                "status": WorkStatus.FAILED,
                "error": StructuredError(
                    code=CANARY,
                    message=CANARY,
                    request_id=CANARY,
                    trace_id=CANARY,
                    run_id=CANARY,
                ),
            }

    container.service._graph = ErrorGraph()
    request = work(request_id=CANARY, trace_id=CANARY, agent_id=CANARY)
    result = await container.service.run(request, principal)
    assert result.run_id == request.run_id
    assert result.errors[0].code == "internal_error"
    assert CANARY not in result.model_dump_json()
    assert await container.service.get(result.run_id, principal) == result


async def test_api_validation_path_header_and_exception_canaries(container):
    app = create_app(container=container)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        missing = await client.get(f"/v1/work/{CANARY}", headers={"X-Request-Id": CANARY})
        extra = await client.post("/v1/work", json={"query": "public", CANARY: CANARY})
        invalid = await client.post("/v1/work", json={"query": {CANARY: CANARY}})
    assert missing.status_code == 404
    assert extra.status_code == invalid.status_code == 422
    assert extra.json() == {
        "detail": [{"loc": ["body"], "msg": "Invalid request", "type": "value_error"}]
    }
    assert CANARY not in missing.text + extra.text + invalid.text


async def test_atomic_parallel_sequences_and_owned_file_retention(container, principal, tmp_path):
    result = await container.service.run(work(), principal)
    observer = container.service.observations
    async with observer.scope(result.run_id, principal):
        records = await asyncio.gather(*(observer.record("policy", "succeeded") for _ in range(4)))
    assert len({r.sequence for r in records}) == 4
    before = await observer.ledger(result.run_id, principal)
    directory = tmp_path / "retention"
    directory.mkdir()
    legacy = directory / "events.jsonl"
    legacy.write_text("synthetic legacy private content")
    evidence = directory / ".agent" / "evidence"
    evidence.mkdir(parents=True)
    (evidence / "preserved").write_text("development evidence")
    trace = LocalJsonlTrace(
        directory, SecretRedactor(), repository=container.repository, retention_days=2
    )
    old = datetime.now(UTC) - timedelta(days=4)
    trace._append_owned(records[0], old)
    owned = directory / "rfa-observations-v1"
    old_file = next(owned.glob("events-*.jsonl"))
    foreign = owned / "unregistered.jsonl"
    foreign.write_text("preserve")
    trace._append_owned(records[1], datetime.now(UTC))
    assert not old_file.exists()
    assert legacy.read_text() == "synthetic legacy private content"
    assert foreign.read_text() == "preserve" and (evidence / "preserved").exists()
    assert stat.S_IMODE(owned.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in owned.glob("events-*.jsonl"))
    assert await observer.ledger(result.run_id, principal) == before  # TTL is FILES only


@pytest.mark.parametrize("dangling", [False, True])
async def test_manifest_symlink_is_never_replaced(container, principal, tmp_path, dangling):
    result = await container.service.run(work(), principal)
    row = (await container.service.observations.ledger(result.run_id, principal)).observations[0]
    directory = tmp_path / "unsafe" / "rfa-observations-v1"
    directory.mkdir(parents=True)
    target = tmp_path / "target"
    if not dangling:
        target.write_text("preserve")
    link = directory / "owned.json"
    link.symlink_to(target)
    trace = LocalJsonlTrace(directory.parent, SecretRedactor(), repository=container.repository)
    with pytest.raises(RfaError):
        await trace.emit_observation(row)
    assert link.is_symlink()
    if not dangling:
        assert target.read_text() == "preserve"


async def test_reserved_file_creation_crash_recovers_without_adopting_unrelated_files(
    container, principal, tmp_path, monkeypatch
):
    result = await container.service.run(work(), principal)
    row = (await container.service.observations.ledger(result.run_id, principal)).observations[0]
    trace = LocalJsonlTrace(tmp_path / "crash", SecretRedactor(), repository=container.repository)
    original = trace._private_open

    def crash(path, flags):
        if path.name.startswith("events-"):
            raise OSError("synthetic crash after durable manifest reservation")
        return original(path, flags)

    monkeypatch.setattr(trace, "_private_open", crash)
    with pytest.raises(RfaError):
        await trace.emit_observation(row)
    manifest = trace.trace_dir / "rfa-observations-v1" / "owned.json"
    reserved = json.loads(manifest.read_text())["files"]
    assert len(reserved) == 1 and not (manifest.parent / reserved[0]).exists()
    monkeypatch.setattr(trace, "_private_open", original)
    await trace.emit_observation(row)
    assert (
        json.loads((manifest.parent / reserved[0]).read_text())["observation_id"]
        == row.observation_id
    )


@pytest.mark.parametrize(
    "status, expected, reason",
    [
        (ResultStatus.SUCCEEDED, "succeeded", None),
        (ResultStatus.FAILED, "failed", None),
        (ResultStatus.DENIED, "denied", "denied"),
        (ResultStatus.TIMED_OUT, "failed", "timeout"),
        (ResultStatus.OUTCOME_UNKNOWN, "outcome_unknown", "timeout"),
    ],
)
async def test_returned_runtime_outcomes_are_not_transport_success(
    container, principal, status, expected, reason
):
    calls = []

    async def returned(spec, task):
        calls.append(task.run_id)
        return TaskResult(
            **{
                k: getattr(task, k)
                for k in ("request_id", "trace_id", "run_id", "agent_id", "domain_id")
            },
            status=status,
            output={},
            simulated=True,
            adapter=CANARY,
        )

    container.runtime.run = returned
    result = await container.service.run(work(), principal)
    ledger = await container.service.observations.ledger(result.run_id, principal)
    rows = [
        r for r in ledger.observations if r.event.event == "runtime" and r.transport == "returned"
    ]
    assert len(rows) == len(calls) == 1
    assert rows[0].event.status == expected and rows[0].event.reason_code == reason
    assert rows[0].event.call_count == 1
    assert CANARY not in ledger.model_dump_json()


async def test_raised_outcome_unknown_is_not_failed_or_replayed(container, principal):
    calls = []

    async def unknown(*args, **kwargs):
        calls.append(True)
        raise OutcomeUnknownError(CANARY)

    container.response.submit_draft = unknown
    result = await container.service.run(work(), principal)
    ledger = await container.service.observations.ledger(result.run_id, principal)
    rows = [r for r in ledger.observations if r.transport == "raised"]
    assert len(calls) == len(rows) == 1
    assert rows[0].event.status == "outcome_unknown" and rows[0].event.reason_code == "timeout"
    assert CANARY not in ledger.model_dump_json()


async def test_inferred_domain_is_bound_from_runtime_spec_with_provenance(container, principal):
    result = await container.service.run(work(domain_id=None), principal)
    assert result.status == WorkStatus.COMPLETED, result.errors
    ledger = await container.service.observations.ledger(result.run_id, principal)
    assert ledger.execution.domain_id == DomainId.TRIV3
    retrieval = [r for r in ledger.observations if r.event.event == "retrieval"]
    assert any(r.event.versions.sources for r in retrieval)
    assert all(r.event.execution.domain_id == DomainId.TRIV3 for r in retrieval)
    with pytest.raises(RfaError):
        await container.repository.observation_alias(
            result.run_id, principal, "domain", DomainId.QUANTIZATION_RESEARCH.value
        )


async def test_export_failure_before_call_is_incomplete_not_an_actual_invocation(
    container, principal
):
    request = work()
    await container.repository.create_owned_run(request, principal, session_id=None, task_id=None)
    observer = container.service.observations
    calls = []

    async def fake_model(request):
        calls.append(True)

    async def fail_export(record):
        raise RfaError("configuration_error", "synthetic file failure")

    container.model.generate = fake_model
    container.trace.emit_observation = fail_export
    port = ObservedPort(container.model, observer, "model", mode="mock")
    async with observer.scope(request.run_id, principal):
        with pytest.raises(RfaError):
            await port.generate(None)
    ledger = await observer.ledger(request.run_id, principal)
    model = next(item for item in ledger.coverage if item.boundary == "model")
    assert not calls and model.state == "incomplete" and model.calls is None
    assert all(r.event.call_count == 0 for r in ledger.observations)


async def test_error_projection_keeps_partial_content_binding_not_nested_canaries(
    container, principal
):
    original = await container.service.run(work(), principal)
    request = work(request_id=CANARY, trace_id=CANARY, agent_id=CANARY)

    class PartialErrorGraph:
        async def ainvoke(self, *args, **kwargs):
            draft_id = new_id("draft")
            binding = dict(
                request_id=CANARY,
                trace_id=CANARY,
                agent_id=CANARY,
                run_id=request.run_id,
                draft_id=draft_id,
            )
            return {
                "status": WorkStatus.FAILED,
                "domain_id": DomainId.TRIV3,
                "draft": original.draft.model_copy(update=binding),
                "review": original.review.model_copy(update={**binding, "safe_reason": CANARY}),
                "error": StructuredError(code=CANARY, message=CANARY),
            }

    container.service._graph = PartialErrorGraph()
    result = await container.service.run(request, principal)
    assert CANARY not in result.model_dump_json()
    for field in ("content", "content_hash", "version", "target", "allowed_evidence"):
        assert getattr(result.draft, field) == getattr(original.draft, field)
    assert await container.service.get(result.run_id, principal) == result
    assert (
        await container.service.resume(result.run_id, ResumeRequest(event_id="terminal"), principal)
        == result
    )
    record = await container.repository.get_owned_run(result.run_id, principal)
    # Protected immutable draft retains correlation; projections do not mutate it.
    assert record.result.draft.request_id == CANARY
    app = create_app(container=container)
    from rfa_mas.api.app import resolve_principal

    async def identity():
        return principal

    app.dependency_overrides[resolve_principal] = identity
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        status = await client.get(f"/v1/runs/{result.run_id}")
        detail = await client.get(f"/v1/sessions/{record.session_id}")
    assert status.status_code == detail.status_code == 200
    assert CANARY not in status.text
    # Owner message history is intentionally readable; only result/error DTO projection is asserted.
    assert CANARY not in json.dumps(detail.json()["runs"])


async def test_reference_http_origin_is_distinct_without_claiming_real_service(tmp_path):
    instance = build_container(
        offline_settings(
            tmp_path,
            response_backend="http",
            response_base_url="http://127.0.0.1:8011",
            response_api_token="synthetic-reference-auth",
        )
    )
    await instance.startup()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_reference_contract_app()),
            base_url="http://127.0.0.1:8011",
        ) as client:
            instance.response._client.client = client
            owner = await instance.repository.local_principal()
            result = await instance.service.run(work(), owner)
        assert result.status == WorkStatus.COMPLETED, result.errors
        ledger = await instance.service.observations.ledger(result.run_id, owner)
        records = [r for r in ledger.observations if r.event.event == "approval"]
        assert records and all(r.provider_kind == "reference_http" for r in records)
        assert all(r.event.mode == "local" and r.event.sandbox_id is None for r in records)
        assert all(r.event.approval_id is None for r in records)
    finally:
        await instance.shutdown()
