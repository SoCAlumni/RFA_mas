#!/usr/bin/env python3
"""Read-only exporter/checker for the existing, provisional RFA contracts.

The product Pydantic models remain authoritative. This script does not add APIs,
read dotenv/process settings, contact providers, or write a baseline implicitly.
Review ``export``/``fixtures`` output before replacing the derived JSON files.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.json_schema import models_json_schema

import rfa_mas.contracts as contracts
from rfa_mas.adapters.mock import MockTool
from rfa_mas.api.app import create_app
from rfa_mas.application.state_machine import ALLOWED_TRANSITIONS
from rfa_mas.bootstrap import build_container
from rfa_mas.contracts import RunResult, ToolRequest, ToolResult, TrustedPrincipal, WorkRequest
from rfa_mas.contracts.common import ContractModel
from rfa_mas.ports import interfaces
from rfa_mas.reference.app import (
    ReviewSubmission,
    RuntimeSubmission,
    create_reference_contract_app,
)
from rfa_mas.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "docs/contracts/baseline.json"
FIXTURES = ROOT / "fixtures/contracts/reference_cases.json"
CONTRACT_SOURCES = (
    "src/rfa_mas/contracts/__init__.py",
    "src/rfa_mas/contracts/common.py",
    "src/rfa_mas/contracts/models.py",
    "src/rfa_mas/ports/interfaces.py",
    "src/rfa_mas/application/state_machine.py",
    "src/rfa_mas/api/app.py",
    "src/rfa_mas/reference/app.py",
)


class OfflineSettings(Settings):
    """Use explicit/default values only, even if the caller exports real secrets."""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[Settings],
        init_settings: Any,
        env_settings: Any,
        dotenv_settings: Any,
        file_secret_settings: Any,
    ) -> tuple[Any, ...]:
        return (init_settings,)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def serialize(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def offline_settings(directory: Path, **overrides: Any) -> OfflineSettings:
    # Frozen 1.0 reference specimens were produced by the explicit mock retriever
    # fixture (simulation scenarios). The product default is now the local lexical
    # reader, so the fixture keeps that selection explicit instead of implicit.
    overrides.setdefault("retriever_backend", "mock")
    return OfflineSettings(
        _env_file=None,
        database_url=f"sqlite:///{directory / 'contract.db'}",
        trace_dir=directory / "traces",
        **overrides,
    )


def build_baseline() -> dict[str, Any]:
    """Re-derive the frozen 1.0 surface using its historical source provenance.

    Additive 1.1 implementation changes do not rewrite the immutable 1.0 artifact.
    Schemas, routes and original method signatures are still compared exactly;
    current source hashes and extended methods belong to build_extended().
    """
    recorded = json.loads(BASELINE.read_text(encoding="utf-8"))
    public_models = {
        name: value
        for name in contracts.__all__
        if name not in contracts.EXTENDED_MODEL_NAMES
        if inspect.isclass(value := getattr(contracts, name)) and issubclass(value, ContractModel)
    }
    public_models.update(ReviewSubmission=ReviewSubmission, RuntimeSubmission=RuntimeSubmission)
    _, schemas = models_json_schema(
        [
            (model, mode)
            for _, model in sorted(public_models.items())
            for mode in ("validation", "serialization")
        ],
        title="Existing RFA 1.0 contract models (derived)",
    )
    port_signatures = {
        name: {
            method_name: str(inspect.signature(method))
            for method_name, method in sorted(vars(port).items())
            if method_name in recorded["ports"].get(name, {}) and inspect.isfunction(method)
        }
        for name, port in sorted(vars(interfaces).items())
        if (
            inspect.isclass(port)
            and name.endswith("Port")
            and port.__module__ == interfaces.__name__
            # Additive 1.1 ports (e.g. SchedulerPort) belong to build_extended() only.
            and name in recorded["ports"]
        )
    }
    with TemporaryDirectory(prefix="rfa-contract-baseline-") as temporary:
        api = create_app(settings=offline_settings(Path(temporary))).openapi()
        reference = create_reference_contract_app().openapi()
    # Additive session endpoints belong to the extended contract. Keep comparing
    # each original operation/schema exactly (never substitute recorded content).
    original_api = recorded["openapi"]["core"]
    api["paths"] = {path: api["paths"][path] for path in original_api["paths"]}
    api["components"]["schemas"] = {
        name: api["components"]["schemas"][name] for name in original_api["components"]["schemas"]
    }
    payload = {
        "baseline_id": "rfa-existing-v1",
        "schema_version": contracts.SCHEMA_VERSION,
        "status": "repository-local-provisional",
        "derived": True,
        "source_of_truth": "src/rfa_mas/contracts (Pydantic); API OpenAPI is generated",
        "source_files": recorded["source_files"],
        "fixture_file": "fixtures/contracts/reference_cases.json",
        "fixture_digest": digest(json.loads(FIXTURES.read_text(encoding="utf-8"))),
        "public_model_names": sorted(public_models),
        "json_schema": schemas,
        "openapi": {"core": api, "teammate_reference": reference},
        "ports": port_signatures,
        "work_state_transitions": {
            state.value: sorted(target.value for target in targets)
            for state, targets in sorted(ALLOWED_TRANSITIONS.items())
        },
        "ownership": {
            "approval_authority": "Seunghee service; local/reference decisions are simulations",
            "runtime_authority": "Dayoung service; LocalRuntime is not an OS sandbox",
            "policy_service_owner": "unresolved; deterministic LocalPolicy and PolicyPort retained",
            "contract_maintenance": (
                "coordinator; consumers must not independently edit shared DTOs"
            ),
        },
        "limits": [
            "This baseline records implemented DTOs only; it is not teammate API agreement.",
            "Session, TeamInstance, TeamSpec, scheduler and durable effect ledger are planned.",
            "WorkRequest.run_id is not a development-task or development-attempt identifier.",
            "Current GET /v1/work/{run_id} does not enforce per-owner lookup authorization.",
            "Reference callbacks, authenticated approval authority and durable receipts "
            "are planned.",
            "RuntimePort currently exposes run/status/cancel, not team prepare/start/cleanup.",
            "Reference HTTP tests use ASGI in-process transport, not a live teammate deployment.",
            "Existing output payload dictionaries are not strongly typed team/worker schemas.",
        ],
    }
    return {**payload, "digest": digest(payload)}


EXTENDED = ROOT / "docs/contracts/extended.json"
EXTENDED_FIXTURES = ROOT / "fixtures/contracts/trace_eval_cases.json"


class TraceEvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    category: Literal[
        "normal",
        "policy_denied",
        "approval_missing",
        "draft_changed",
        "acl_changed",
        "duplicate",
        "outcome_unknown",
    ]
    execution: contracts.ExecutionContext
    policy: contracts.PolicyDecisionV11
    draft: contracts.DraftBundleV11 | None = None
    approval: contracts.ApprovalReference | None = None
    receipt: contracts.PublicationReceipt | None = None
    trace: contracts.TraceEvent
    evaluation: contracts.EvalResultV11
    expected_action: Literal["none", "review", "query", "deny"]

    @model_validator(mode="after")
    def check_links(self) -> TraceEvalCase:
        for key in ("request_id", "trace_id", "run_id", "agent_id", "domain_id"):
            if getattr(self.execution, key) != getattr(self.policy, key):
                raise ValueError("policy/run reference mismatch")
        if self.trace.execution != self.execution:
            raise ValueError("trace/run reference mismatch")
        if self.evaluation.run_id != self.execution.run_id:
            raise ValueError("evaluation/run reference mismatch")
        if (
            self.evaluation.trace_id != self.execution.trace_id
            or self.evaluation.case_id != self.id
        ):
            raise ValueError("evaluation trace/case reference mismatch")
        if self.trace.policy_decision_id != self.policy.decision_id:
            raise ValueError("trace/policy decision reference mismatch")
        if self.draft:
            for key in ("request_id", "trace_id", "run_id", "agent_id", "domain_id"):
                if getattr(self.draft, key) != getattr(self.execution, key):
                    raise ValueError("draft/execution reference mismatch")
            if self.draft.policy_decision_id != self.policy.decision_id:
                raise ValueError("draft/policy decision reference mismatch")
        if self.receipt and (
            not self.approval
            or not self.draft
            or self.receipt.binding != self.approval.binding
            or self.receipt.binding != self.draft.binding()
            or self.receipt.run_id != self.execution.run_id
            or self.receipt.approval_id != self.approval.approval_id
        ):
            raise ValueError("receipt/approval/draft binding mismatch")
        if self.receipt and (
            not self.policy.allowed or not self.approval.matches(self.draft, self.trace.timestamp)
        ):
            raise ValueError("receipt requires a currently matching approved draft")
        if not self.policy.simulated or self.trace.mode != "mock" or self.evaluation.mode != "mock":
            raise ValueError("reference fixture must not claim real execution")
        if (
            (self.draft and not self.draft.simulated)
            or (self.approval and self.approval.mode != "mock")
            or (self.receipt and self.receipt.mode != "mock")
        ):
            raise ValueError("reference fixture must preserve mock mode in every stage")
        if self.trace.draft_id != (self.draft.draft_id if self.draft else None):
            raise ValueError("trace references a missing or different draft")
        if self.trace.approval_id != (self.approval.approval_id if self.approval else None):
            raise ValueError("trace references a missing or different approval")
        if self.trace.publication_id != (self.receipt.publication_id if self.receipt else None):
            raise ValueError("trace references a missing or different receipt")
        if self.category == "policy_denied" and (self.policy.allowed or self.draft):
            raise ValueError("denied fixture must stop before draft")
        if self.category == "approval_missing" and (self.approval or self.receipt):
            raise ValueError("missing approval must not invent a receipt")
        if self.category in {"draft_changed", "acl_changed"} and (
            not self.approval
            or not self.draft
            or self.approval.matches(self.draft, self.trace.timestamp)
        ):
            raise ValueError("changed binding must invalidate approval")
        expected = {
            "normal": "none",
            "policy_denied": "deny",
            "approval_missing": "review",
            "draft_changed": "review",
            "acl_changed": "review",
            "duplicate": "none",
            "outcome_unknown": "query",
        }[self.category]
        if self.expected_action != expected:
            raise ValueError("fixture expected action contradicts category")
        if self.category in {"normal", "duplicate"} and (
            not self.receipt
            or self.receipt.status != "succeeded"
            or self.receipt.next_action != "none"
        ):
            raise ValueError("normal/replay fixture requires the existing successful receipt")
        if self.category == "outcome_unknown" and (
            not self.receipt
            or self.receipt.status != "outcome_unknown"
            or self.receipt.next_action != "query"
        ):
            raise ValueError("unknown fixture requires uncertain receipt and query")
        return self


def load_extended_cases() -> list[TraceEvalCase]:
    data = json.loads(EXTENDED_FIXTURES.read_text(encoding="utf-8"))
    if data["schema_version"] != "1.1" or data["synthetic"] is not True:
        raise ValueError("extended fixtures must be synthetic 1.1")
    cases = [TraceEvalCase.model_validate(case) for case in data["cases"]]
    if {case.category for case in cases} != {
        "normal",
        "policy_denied",
        "approval_missing",
        "draft_changed",
        "acl_changed",
        "duplicate",
        "outcome_unknown",
    }:
        raise ValueError("extended fixtures must cover all seven cases")
    if len({case.id for case in cases}) != len(cases):
        raise ValueError("duplicate extended fixture ID")
    return cases


def build_extended() -> dict[str, Any]:
    models = [getattr(contracts, name) for name in contracts.EXTENDED_MODEL_NAMES]
    _, schemas = models_json_schema(
        [(model, mode) for model in models for mode in ("validation", "serialization")],
        title="RFA 1.1 additive repository-local reference contracts",
    )
    ports = {
        name: {
            method_name: str(inspect.signature(method))
            for method_name, method in sorted(vars(port).items())
            if not method_name.startswith("_") and inspect.isfunction(method)
        }
        for name, port in sorted(vars(interfaces).items())
        if inspect.isclass(port)
        and name.endswith("Port")
        and port.__module__ == interfaces.__name__
    }
    with TemporaryDirectory(prefix="rfa-contract-extended-") as temporary:
        api = create_app(settings=offline_settings(Path(temporary))).openapi()
    payload = {
        "baseline_id": "rfa-extended-v1.1",
        "schema_version": "1.1",
        "status": "repository-local-provisional",
        "derived": True,
        "source_of_truth": "src/rfa_mas/contracts (Pydantic)",
        "legacy_baseline_digest": json.loads(BASELINE.read_text())["digest"],
        "source_files": {
            path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
            for path in (*CONTRACT_SOURCES, "scripts/contract_baseline.py")
        },
        "public_model_names": list(contracts.EXTENDED_MODEL_NAMES),
        "json_schema": schemas,
        "ports": ports,
        "fixture_file": "fixtures/contracts/trace_eval_cases.json",
        "fixture_digest": digest(json.loads(EXTENDED_FIXTURES.read_text())),
        "implemented_http_routes": {
            "core": api,
            "teammate_reference": create_reference_contract_app().openapi(),
        },
        "limits": [
            "No new HTTP route is implemented by a DTO/protocol declaration.",
            "1.0 source hashes record historical provenance; 1.1 source hashes are current.",
            "1.1 methods require explicit adapter capability/version support; no silent fallback.",
            "IDs/hashes/body principal/capabilities are not authentication or authorization proof.",
            "Approval/publication: Seunghee; runtime identity: Dayoung; policy owner unresolved.",
            "Fixture validation is structural, not product or live integration execution.",
        ],
    }
    return {**payload, "digest": digest(payload)}


def export_extended_fixtures() -> dict[str, Any]:
    from datetime import UTC, datetime, timedelta

    c = contracts
    at = datetime(2026, 9, 26, tzinfo=UTC)
    ctx = c.ExecutionContext(
        request_id="req-fixture",
        trace_id="trace-fixture",
        run_id="run-fixture",
        agent_id="assistant-supervisor",
        session_id="session-fixture",
        domain_id="triv3",
        task_id="task-fixture",
        team_id="team-fixture",
    )
    versions = c.VersionReferences(
        code="fixture-v1", policy="policy-v1", dataset="contract-v1", evaluator="rules-v1"
    )
    source = c.SourceRevisionRef(
        source_id="public-faq",
        source_revision="r1",
        location=c.SourceLocation(uri="fixture:public-faq", line_start=1),
        audience="public",
        content_hash=c.sha256_text("public fact"),
        acl_revision="acl1",
        policy_version="policy-v1",
    )
    policy = c.PolicyDecisionV11(
        **{
            k: getattr(ctx, k)
            for k in ("request_id", "trace_id", "run_id", "agent_id", "domain_id")
        },
        allowed=True,
        code="allowed",
        safe_reason="synthetic policy",
        policy_version="policy-v1",
        simulated=True,
        adapter="reference_mock",
        decision_id="policy-fixture",
        decision="allow",
        action="share",
        subject_id="fixture-owner",
        resource_id="public-faq",
        recipient="public",
        issued_at=at,
        expires_at=at + timedelta(hours=1),
        source_refs=(source,),
    )

    def draft_for(content="Public synthetic FAQ.", ref=source, version=1):
        old = c.DraftBundle(
            **{
                k: getattr(ctx, k)
                for k in ("request_id", "trace_id", "run_id", "agent_id", "domain_id")
            },
            draft_id="draft-fixture",
            version=version,
            content=content,
            content_hash=c.sha256_text(content),
            target=c.DraftTarget(audience="public"),
            audience="public",
            policy_version="policy-v1",
            allowed_evidence=(
                c.EvidenceRef(
                    **ref.model_dump(exclude={"schema_version", "acl_revision", "policy_version"})
                ),
            ),
            simulated=True,
            adapter="reference_mock",
        )
        data = {
            **old.model_dump(),
            "target": old.target,
            "schema_version": "1.1",
            "attachments": (),
            "sources": (ref,),
            "policy_decision_id": policy.decision_id,
        }
        provisional = c.DraftBundleV11.model_construct(**data)
        data["payload_hash"] = provisional.calculated_payload_hash()
        return c.DraftBundleV11.model_validate(data)

    original = draft_for()
    approval = c.ApprovalReference(
        approval_id="approval-fixture",
        approver_id="fixture-owner",
        binding=original.binding(),
        decision="approved",
        issued_at=at,
        expires_at=at + timedelta(hours=1),
        mode="mock",
        authority="reference_mock",
    )
    receipt = c.PublicationReceipt(
        publication_id="publication-fixture",
        run_id=ctx.run_id,
        idempotency_key="publication-fixture-once",
        binding=original.binding(),
        approval_id=approval.approval_id,
        status="succeeded",
        external_result_ref="local-sink/record-1",
        mode="mock",
        next_action="none",
    )
    cases = []
    for category in (
        "normal",
        "policy_denied",
        "approval_missing",
        "draft_changed",
        "acl_changed",
        "duplicate",
        "outcome_unknown",
    ):
        d, a, r, p, action = original, approval, receipt, policy, "none"
        if category == "policy_denied":
            p = policy.model_copy(update={"allowed": False, "decision": "deny", "code": "denied"})
            d = a = r = None
            action = "deny"
        elif category == "approval_missing":
            a = r = None
            action = "review"
        elif category in {"draft_changed", "acl_changed"}:
            d = (
                draft_for("Revised public FAQ.", version=2)
                if category == "draft_changed"
                else draft_for(ref=source.model_copy(update={"acl_revision": "acl2"}))
            )
            r, action = None, "review"
        elif category == "outcome_unknown":
            r = receipt.model_copy(
                update={
                    "status": c.PublicationStatus.OUTCOME_UNKNOWN,
                    "external_result_ref": None,
                    "next_action": "query",
                }
            )
            action = "query"
        trace = c.TraceEvent(
            execution=ctx,
            event="publish" if r else "policy",
            status="outcome_unknown"
            if action == "query"
            else "denied"
            if action == "deny"
            else "waiting"
            if action == "review"
            else "succeeded",
            mode="mock",
            versions=versions,
            timestamp=at,
            policy_decision_id=p.decision_id,
            draft_id=d.draft_id if d else None,
            approval_id=a.approval_id if a else None,
            publication_id=r.publication_id if r else None,
        )
        evaluation = c.EvalResultV11(
            case_id=f"contract-{category}",
            run_id=ctx.run_id,
            trace_id=ctx.trace_id,
            rule_checks={},
            rule_status="not_run",
            judge_status="not_run",
            judge_kind="not_run",
            executed=False,
            simulated=False,
            evaluator="rules",
            versions=versions,
            mode="mock",
        )
        case = TraceEvalCase(
            id=f"contract-{category}",
            category=category,
            execution=ctx,
            policy=p,
            draft=d,
            approval=a,
            receipt=r,
            trace=trace,
            evaluation=evaluation,
            expected_action=action,
        )
        cases.append(case.model_dump(mode="json"))
    return {
        "schema_version": "1.1",
        "synthetic": True,
        "status": "repository-local-provisional",
        "cases": cases,
    }


class ReferenceCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    category: Literal["normal", "permission_denied", "insufficient", "partial_failure", "timeout"]
    kind: Literal["work", "tool"]
    request: dict[str, Any]
    response: dict[str, Any]
    principal: TrustedPrincipal | None = None
    settings: dict[str, int] = Field(default_factory=dict)
    assertions: dict[str, Any]

    @model_validator(mode="after")
    def validate_product_payloads(self) -> ReferenceCase:
        if set(self.settings) - {"max_graph_steps"}:
            raise ValueError("fixtures may only override the graph step budget")
        if self.kind == "work":
            request = WorkRequest.model_validate(self.request)
            response = RunResult.model_validate(self.response)
            if self.principal is None:
                raise ValueError("work fixtures require a synthetic trusted principal")
        else:
            request = ToolRequest.model_validate(self.request)
            response = ToolResult.model_validate(self.response)
        for name in ("request_id", "trace_id", "run_id", "agent_id"):
            if getattr(request, name) != getattr(response, name):
                raise ValueError(f"fixture response {name} binding mismatch")
        if not response.simulated:
            raise ValueError("reference case responses must be labelled simulated")
        if project_response(response) != self.assertions:
            raise ValueError("fixture response does not satisfy its declared assertions")
        return self


def project_response(response: RunResult | ToolResult) -> dict[str, Any]:
    """Observable assertions, excluding generated IDs/times and prose snapshots."""
    if isinstance(response, ToolResult):
        return {
            "status": response.status.value,
            "error_code": response.error.code if response.error else None,
            "retryable": response.error.retryable if response.error else None,
            "simulated": response.simulated,
        }
    return {
        "status": response.status.value,
        "error_codes": [error.code for error in response.errors],
        "draft_present": response.draft is not None,
        "review_decision": response.review.decision.value if response.review else None,
        "publication_status": response.publication_status.value,
        "evidence_audiences": sorted(
            {item.audience.value for item in response.draft.allowed_evidence}
        )
        if response.draft
        else [],
        "simulated": response.simulated,
    }


def load_cases(path: Path = FIXTURES) -> list[ReferenceCase]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("synthetic") is not True or data.get("schema_version") != "1.0":
        raise ValueError("only versioned synthetic contract fixtures are accepted")
    cases = [ReferenceCase.model_validate(case) for case in data["cases"]]
    if len({case.id for case in cases}) != len(cases):
        raise ValueError("duplicate reference fixture id")
    if {case.category for case in cases} != {
        "normal",
        "permission_denied",
        "insufficient",
        "partial_failure",
        "timeout",
    }:
        raise ValueError("reference cases must cover all five contract outcome categories")
    return cases


async def execute_case(case: ReferenceCase, directory: Path) -> RunResult | ToolResult:
    """Execute local/mock logic only; callers provide an isolated temporary directory."""
    if case.kind == "tool":
        return await MockTool().execute(ToolRequest.model_validate(case.request))
    container = build_container(offline_settings(directory, **case.settings))
    await container.startup()
    try:
        assert case.principal is not None
        return await container.service.run(WorkRequest.model_validate(case.request), case.principal)
    finally:
        await container.shutdown()


async def export_fixture_cases() -> dict[str, Any]:
    """Reproducible synthetic specimens from existing local behavior, never live success."""
    principal = TrustedPrincipal(
        user_id="fixture-owner-001",
        authenticated=True,
        company_id="local-company",
        business_units=frozenset({"triv3-team"}),
        roles=frozenset({"company"}),
    )
    work_outcomes = [
        ("normal", "success", {}, "completed", [], True, "approved", ["public"]),
        ("permission_denied", "success", {}, "failed", ["policy_denied"], False, None, []),
        (
            "insufficient",
            "insufficient_evidence",
            {},
            "waiting_approval",
            [],
            True,
            "revision_requested",
            [],
        ),
        (
            "partial_failure",
            "success",
            {"max_graph_steps": 5},
            "failed",
            ["budget_exceeded"],
            True,
            None,
            ["public"],
        ),
        ("timeout", "timeout", {}, "failed", ["retrieval_timeout"], False, None, []),
    ]
    cases = []
    for category, scenario, settings, status, errors, has_draft, review, audiences in work_outcomes:
        case_id = f"work-{category}"
        request = WorkRequest(
            request_id=f"req-{case_id}",
            trace_id=f"trace-{case_id}",
            run_id=f"run-{case_id}",
            domain_id="triv3",
            query="TRIV3 공개 근거를 요약해 주세요.",
            target={"audience": "owner" if category == "permission_denied" else "public"},
            simulation_scenario=scenario,
        )
        assertions = {
            "status": status,
            "error_codes": errors,
            "draft_present": has_draft,
            "review_decision": review,
            "publication_status": "not_requested",
            "evidence_audiences": audiences,
            "simulated": True,
        }
        # A temporary unvalidated wrapper lets this exporter obtain the specimen once;
        # final output is validated against both product DTOs and independent assertions.
        pending = ReferenceCase.model_construct(
            id=case_id,
            category=category,
            kind="work",
            request=request.model_dump(mode="json"),
            principal=(
                TrustedPrincipal(user_id="synthetic-outsider", authenticated=False)
                if category == "permission_denied"
                else principal
            ),
            settings=settings,
            response={},
            assertions=assertions,
        )
        with TemporaryDirectory(prefix="rfa-contract-fixture-") as temporary:
            actual = await execute_case(pending, Path(temporary))
        response = actual.model_dump(mode="json")
        response["created_at"] = response["updated_at"] = "2026-09-26T00:00:00Z"
        if response["draft"]:
            response["draft"]["draft_id"] = f"draft-{case_id}"
        if response["review"]:
            response["review"]["draft_id"] = f"draft-{case_id}"
        pending.response = response
        cases.append(ReferenceCase.model_validate(pending.model_dump()).model_dump(mode="json"))
    for category, effect, simulate, status, code, retryable in (
        ("normal", "read", "", "succeeded", None, None),
        ("permission_denied", "write", "", "denied", "external_writes_disabled", False),
        ("timeout", "read", "timeout", "timed_out", "read_timeout", True),
        ("timeout", "write", "timeout", "outcome_unknown", "outcome_unknown", False),
    ):
        case_id = f"tool-{category}-{effect}"
        request = ToolRequest(
            request_id=f"req-{case_id}",
            trace_id=f"trace-{case_id}",
            run_id=f"run-{case_id}",
            agent_id="contract-fixture-agent",
            domain_id="triv3",
            idempotency_key=case_id,
            tool_name="lookup" if effect == "read" else "publish",
            effect=effect,
            arguments={"simulate": simulate, "term": "synthetic"},
        )
        response = await MockTool().execute(request)
        case = ReferenceCase(
            id=case_id,
            category=category,
            kind="tool",
            request=request.model_dump(mode="json"),
            response=response.model_dump(mode="json"),
            assertions={
                "status": status,
                "error_code": code,
                "retryable": retryable,
                "simulated": True,
            },
        )
        cases.append(case.model_dump(mode="json"))
    return {
        "schema_version": "1.0",
        "synthetic": True,
        "status": "repository-local-provisional",
        "limits": [
            "No live provider, teammate service, approval authority or sandbox was exercised.",
            "Partial failure is the existing graph budget stop after DRAFT creation, "
            "not team recovery.",
            "The simulated tool write timeout performs no external write.",
            "Fixed specimen timestamps are normalization, not execution evidence timestamps.",
        ],
        "cases": cases,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=("check", "export", "fixtures", "write-extended", "check-extended"),
        nargs="?",
        default="check",
    )
    args = parser.parse_args()
    if args.action == "write-extended":
        EXTENDED_FIXTURES.write_text(serialize(export_extended_fixtures()), encoding="utf-8")
        load_extended_cases()
        EXTENDED.write_text(serialize(build_extended()), encoding="utf-8")
        print(
            "Generated provisional 1.1 schemas and seven synthetic fixture shapes; "
            "not execution evidence."
        )
        return
    if args.action == "check-extended":
        if json.loads(EXTENDED.read_text()) != build_extended():
            raise SystemExit("Extended schema stale; review then write-extended.")
        print(f"extended contract: {len(load_extended_cases())} fixture shapes valid")
        return
    if args.action == "fixtures":
        print(serialize(asyncio.run(export_fixture_cases())), end="")
        return
    baseline = build_baseline()
    if args.action == "export":
        print(serialize(baseline), end="")
        return
    recorded = json.loads(BASELINE.read_text(encoding="utf-8"))
    if recorded != baseline:
        raise SystemExit("Contract baseline is stale; review and export the new derived baseline.")
    cases = load_cases()
    print(
        f"contract baseline {baseline['digest']}: "
        f"schemas/OpenAPI and {len(cases)} fixture shapes valid"
    )


if __name__ == "__main__":
    main()
