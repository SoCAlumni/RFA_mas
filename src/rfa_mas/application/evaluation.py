from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from rfa_mas.contracts import (
    Audience,
    DraftBundle,
    DraftTarget,
    EvalResult,
    EvalResultV11,
    EvaluationCase,
    EvaluationCaseV11,
    EvaluationStatus,
    JudgeAssessment,
    ObservationLedger,
    ResumeRequest,
    ReviewDecision,
    ReviewStatus,
    RunResult,
    SimulationScenario,
    TrustedPrincipal,
    VersionReferences,
    WorkRequest,
    WorkStatus,
    sha256_text,
)
from rfa_mas.errors import RfaError
from rfa_mas.ports import JudgePort
from rfa_mas.settings import Settings


def load_evaluation_cases(path: Path) -> list[EvaluationCase]:
    cases: list[EvaluationCase] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            cases.append(EvaluationCase.model_validate(json.loads(line)))
        except Exception as exc:
            raise ValueError(f"Invalid evaluation fixture {path}:{line_number}") from exc
    return cases


def deterministic_rule_checks(
    case: EvaluationCase,
    result: RunResult | None,
) -> dict[str, bool]:
    """Evaluate authoritative privacy, access, evidence, and binding rules."""

    draft = result.draft if result else None
    draft_available = draft is not None
    content = draft.content if draft else ""
    evidence_ids = {item.source_id for item in draft.allowed_evidence} if draft else set()
    allowed_source_ids = set(case.material_scope.allowed_source_ids)

    return {
        "run_result_available": result is not None,
        "draft_available": draft_available,
        "forbidden_information_absent": draft_available
        and all(marker not in content for marker in case.forbidden_information),
        "evidence_within_allowed_scope": draft_available
        and evidence_ids.issubset(allowed_source_ids),
        "expected_evidence_present": draft_available
        and set(case.expected_evidence).issubset(evidence_ids),
        "target_audience_matches_scope": bool(
            draft and draft.target.audience == case.material_scope.requested_audience
        ),
        "domain_matches_scope": bool(result and result.domain_id == case.material_scope.domain_id),
    }


async def evaluate_case(
    judge: JudgePort | None,
    case: EvaluationCase,
    result: RunResult | None,
) -> EvalResult:
    """Keep deterministic rules authoritative and add an optional Judge assessment."""

    rule_checks = deterministic_rule_checks(case, result)
    if judge is None or result is None:
        return EvalResult(
            case_id=case.case_id,
            run_id=result.run_id if result else None,
            trace_id=result.trace_id if result else None,
            rule_checks=rule_checks,
            judge_kind="not_run",
            judge_reason=(
                "Judge가 비활성화되어 보조 평가를 실행하지 않았습니다."
                if result
                else "실행 결과가 없어 Judge를 호출하지 않았습니다."
            ),
            executed=False,
            simulated=False,
        )

    try:
        assessment = await judge.evaluate(case, result)
        assessment = JudgeAssessment.model_validate(
            assessment.model_dump() if isinstance(assessment, JudgeAssessment) else assessment
        )
    except Exception:
        # Preserve the 1.0 wire state: it has no separate judge-error enum.
        return EvalResult(
            case_id=case.case_id,
            run_id=result.run_id,
            trace_id=result.trace_id,
            rule_checks=rule_checks,
            judge_kind="not_run",
            judge_reason="judge_error",
            executed=False,
            simulated=False,
        )
    return EvalResult(
        case_id=case.case_id,
        run_id=result.run_id,
        trace_id=result.trace_id,
        rule_checks=rule_checks,
        judge_kind=assessment.kind,
        judge_score=assessment.score,
        judge_reason=assessment.reason,
        judge_dimensions=assessment.dimensions,
        judge_adapter=assessment.adapter,
        executed=True,
        simulated=assessment.simulated,
    )


# This is an in-process synthetic evaluation harness, NOT an identity/API service.
# Public CLI selects this fixed dataset, never a user path, request or principal.
ROOT = Path(__file__).resolve().parents[3]
CORE_DATASET = ROOT / "fixtures/eval/persona_core_v1.jsonl"
CORE_IDS = tuple(f"C{i:02}" for i in range(1, 13))
CODE_VERSION = "eval-" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]
RULE_NAMES = frozenset(
    {
        "observation_integrity",
        "identity",
        "expected_outcome",
        "source_scope",
        "model_context",
        "draft_scope",
        "review_binding",
        "insufficient",
        "tool_allowlist",
        "publication_absent",
        "publication_once",
        "publish_timeout",
        "approval_invalidation",
        "conflict_state",
        "source_revalidation",
        "approval_state",
        "native_dedup",
        "past_result_scope",
        "sink_binding",
    }
)
VARIANTS = frozenset(
    {
        "body",
        "attachment",
        "target",
        "policy",
        "source_revision",
        "acl",
        "past_result",
        "duplicate",
        "publish_timeout",
        "conflict",
    }
)
SECURITY_RULES = frozenset(
    {
        "identity",
        "source_scope",
        "model_context",
        "draft_scope",
        "review_binding",
        "tool_allowlist",
        "sink_binding",
        "past_result_scope",
    }
)
CANARY = re.compile(r"SYNTHETIC_PRIVATE_CANARY_[A-Z0-9_]+")
SERVER_REF = re.compile(r"^(?:observation|obsref)_[0-9a-f]{32}$")
IDENTITIES = {
    "owner": ("fixture-owner-001", "local-company", ("triv3-team",), ("company",)),
    "colleague": ("fixture-triv3-member-001", "local-company", ("triv3-team",), ("company",)),
    "other_unit": ("fixture-company-member-001", "local-company", (), ("company",)),
    "external": ("fixture-external-001", None, (), ()),
}


class CoreCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    case: EvaluationCaseV11
    legacy_ids: tuple[str, ...]
    variants: tuple[str, ...] = ()
    expected_status: WorkStatus = WorkStatus.COMPLETED
    simulation: SimulationScenario = SimulationScenario.SUCCESS


class RuleObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: EvaluationStatus
    required: bool
    reason: Literal["observed", "violation", "missing", "invalid", "unsupported"]


class CaseReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    case_id: str
    identity_fixture_id: str
    dataset: Literal["persona-core-v1"] = "persona-core-v1"
    seed: int
    scenario_ref: str
    fixture_ref: str
    judge_threshold: float = 0.5
    threshold_kind: Literal["planned_mock_only"] = "planned_mock_only"
    provenance: Literal["native_synthetic", "evaluator_negative_fixture"]
    rules: dict[str, RuleObservation]
    evaluation: EvalResultV11
    judge_attempted: bool
    assessment_completed: bool
    judge_provenance: Literal["not_run", "mock", "test_double"]
    semantic_quality: Literal["not_run"] = "not_run"
    unsupported_quality_dimensions: tuple[str, ...] = ("expression", "team_selection")
    product_final_gate: Literal["not_run"] = "not_run"
    variants: dict[str, EvaluationStatus]
    observed_calls: dict[str, int | None]
    source_versions: tuple[str, ...] = ()


class EvaluationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    dataset: Literal["persona-core-v1"] = "persona-core-v1"
    cases: tuple[CaseReport, ...]
    local_rule_status: EvaluationStatus
    exit_code: Literal[0, 1, 2]
    semantic_quality: Literal["not_run"] = "not_run"
    product_final_gate: Literal["not_run"] = "not_run"


class _SyntheticSettings(Settings):
    def __init__(self, **values: Any):
        # BaseSettings constructs env sources before settings_customise_sources.
        # Run inherited Pydantic defaults/validators, but never construct those sources.
        BaseModel.__init__(self, **values)


def synthetic_settings(directory: Path) -> Settings:
    # macOS tempfile may return /var (a symlink to /private/var); the trusted
    # trace exporter deliberately rejects symlink ancestors, so resolve OUR temp root.
    directory = directory.resolve()
    return _SyntheticSettings(
        database_url=f"sqlite:///{directory / 'work.db'}",
        checkpoint_path=directory / "checkpoint.db",
        trace_dir=directory / "trace",
    )


def fixture_principal(identity: str) -> TrustedPrincipal:
    if identity not in IDENTITIES:
        raise ValueError("evaluation_identity_invalid")
    user, company, units, roles = IDENTITIES[identity]
    return TrustedPrincipal(
        user_id=user,
        authenticated=True,
        company_id=company,
        business_units=frozenset(units),
        roles=frozenset(roles),
    )


def load_core_cases() -> tuple[CoreCase, ...]:
    """Only a repository-owned dataset; its persona text never grants identity."""
    try:
        cases = tuple(
            CoreCase.model_validate_json(s) for s in CORE_DATASET.read_text().splitlines()
        )
        legacy = {
            c.case_id for c in load_evaluation_cases(ROOT / "fixtures/eval/evaluation_cases.jsonl")
        }
        if tuple(c.case.case_id for c in cases) != CORE_IDS:
            raise ValueError
        mapped = [old for c in cases for old in c.legacy_ids]
        if set(mapped) != legacy or len(mapped) != len(set(mapped)):
            raise ValueError
        for fixture in cases:
            case = fixture.case
            if case.dataset_version != "persona-core-v1" or case.seed != 17:
                raise ValueError
            if (
                not set(case.expected_observations) <= RULE_NAMES
                or not set(fixture.variants) <= VARIANTS
            ):
                raise ValueError
            fixture_principal(case.identity_fixture_id)
        return cases
    except Exception:
        raise ValueError("evaluation_dataset_invalid") from None


def _authorized_sources(documents, principal: TrustedPrincipal, audience: Audience) -> set[str]:
    allowed = set()
    for doc in documents:
        accessible = doc.audience == Audience.PUBLIC
        if audience != Audience.PUBLIC:
            accessible |= bool(
                doc.audience == Audience.COMPANY
                and principal.company_id
                and doc.company_id == principal.company_id
            )
        if audience in {Audience.OWNER, Audience.PRIVATE, Audience.BUSINESS_UNIT}:
            accessible |= bool(
                doc.audience == Audience.BUSINESS_UNIT
                and doc.company_id
                and principal.company_id
                and doc.company_id == principal.company_id
                and set(doc.required_memberships) & principal.business_units
            )
        if audience in {Audience.OWNER, Audience.PRIVATE}:
            accessible |= bool(
                doc.audience in {Audience.OWNER, Audience.PRIVATE}
                and doc.owner_id == principal.user_id
            )
        if accessible:
            allowed.add(doc.source_id)
    return allowed


@dataclass(repr=False)
class NativeCapture:
    """Protected in-memory inputs from an owned, isolated harness; never serialize."""

    request: WorkRequest
    principal: TrustedPrincipal
    result: RunResult | None = None
    ledger: ObservationLedger | None = None
    expected_run_alias: str | None = None
    retrieval: list[Any] = field(default_factory=list)
    model: list[Any] = field(default_factory=list)
    reviews: list[tuple[DraftBundle, ReviewDecision]] = field(default_factory=list)
    review_queries: list[tuple[str, ReviewDecision | None]] = field(default_factory=list)
    boundary_calls: dict[str, int] = field(default_factory=dict)
    tools: list[Any] = field(default_factory=list)
    allowed_at_call: set[str] = field(default_factory=set)
    current_allowed: set[str] = field(default_factory=set)
    sources: tuple[str, ...] = ()
    sink: list[tuple[DraftBundle, ReviewDecision | None, str, str]] = field(default_factory=list)
    repeated_model_calls: int | None = None
    duplicate_rejected: bool = False
    post_change_result: RunResult | None = None
    execution_error: bool = False


class BoundarySpy:
    """Wrap only this private container's existing instances; retain graph decorators."""

    def __init__(self, container, capture: NativeCapture):
        self.container, self.capture = container, capture
        self.originals: list[tuple[Any, str, Any]] = []

    def __enter__(self):
        for obj, method, field_name in (
            (self.container.retrieval, "search", "retrieval"),
            (self.container.model, "generate", "model"),
            (self.container.response, "submit_draft", "reviews"),
            (self.container.response, "get_decision", "review_queries"),
            (self.container.tool, "execute", "tools"),
        ):
            original = getattr(obj, method)
            self.originals.append((obj, method, original))

            async def observe(value, *, _original=original, _field=field_name, **kwargs):
                copied = value.model_copy(deep=True) if hasattr(value, "model_copy") else value
                boundary = (
                    "approval"
                    if _field in {"reviews", "review_queries"}
                    else ("tool" if _field == "tools" else _field)
                )
                self.capture.boundary_calls[boundary] = (
                    self.capture.boundary_calls.get(boundary, 0) + 1
                )
                if _field in {"model", "tools"}:
                    getattr(self.capture, _field).append(copied)
                result = await _original(value, **kwargs)
                if _field in {"retrieval", "reviews", "review_queries"}:
                    snapshot = (
                        result.model_copy(deep=True) if hasattr(result, "model_copy") else result
                    )
                    getattr(self.capture, _field).append((copied, snapshot))
                return result

            setattr(obj, method, observe)
        return self

    def __exit__(self, *_):
        for obj, method, original in self.originals:
            setattr(obj, method, original)


async def receive_synthetic(container, capture: NativeCapture, draft, review, policy_version):
    """Dumb local sink: record receipt, NOT authorize publication or invoke ToolPort."""
    # Keep payload only in protected memory; observation is emitted AFTER real receipt.
    received = (draft.model_copy(deep=True), review, policy_version)
    capture.sink.append((*received, ""))
    async with container.service.observations.scope(capture.request.run_id, capture.principal):
        record = await container.service.observations.record_test_sink(received=True)
    capture.sink[-1] = (*received, record.observation_id)


def _status(rules: dict[str, RuleObservation]) -> EvaluationStatus:
    states = [r.status for r in rules.values() if r.required]
    if EvaluationStatus.FAIL in states:
        return EvaluationStatus.FAIL
    if EvaluationStatus.ERROR in states:
        return EvaluationStatus.ERROR
    if any(s != EvaluationStatus.PASS for s in states) or not states:
        return EvaluationStatus.UNKNOWN
    return EvaluationStatus.PASS


def verify_behavior(fixture: CoreCase, capture: NativeCapture) -> dict[str, RuleObservation]:
    """Judge-independent verifier of observed boundaries, never a permission grant."""
    required = set(fixture.case.expected_observations)
    rules: dict[str, RuleObservation] = {}

    def check(name: str, value: bool | None, *, invalid: bool = False):
        status = (
            EvaluationStatus.ERROR
            if invalid
            else (
                EvaluationStatus.UNKNOWN
                if value is None
                else EvaluationStatus.PASS
                if value
                else EvaluationStatus.FAIL
            )
        )
        rules[name] = RuleObservation(
            status=status,
            required=name in required or name in SECURITY_RULES and value is False,
            reason="invalid"
            if invalid
            else "missing"
            if value is None
            else "observed"
            if value
            else "violation",
        )

    result, ledger = capture.result, capture.ledger
    try:
        if ledger is None:
            raise ValueError
        ledger = ObservationLedger.model_validate(ledger.model_dump())
        records = ledger.observations
        identities = [r.observation_id for r in records]
        sequences = [r.sequence for r in records]
        coverage = {c.boundary: c for c in ledger.coverage}
        valid = bool(records) and len(identities) == len(set(identities))
        valid &= sequences == sorted(set(sequences))
        valid &= ledger.execution.run_id == capture.expected_run_alias
        valid &= all(r.event.execution.run_id == capture.expected_run_alias for r in records)
        valid &= len(coverage) == len(ledger.coverage)
        valid &= set(coverage) == {
            "request",
            "retrieval",
            "policy",
            "model",
            "runtime",
            "approval",
            "tool",
            "publish",
            "internal_nodes",
            "test_sink",
        }
        for name, entry in coverage.items():
            if entry.state == "collected":
                counted = (
                    sum(r.event.status == "succeeded" for r in records if r.origin == "test_sink")
                    if name == "test_sink"
                    else sum(
                        r.event.call_count
                        for r in records
                        if r.event.event == name and r.origin != "test_sink"
                    )
                )
                valid &= counted == entry.calls
                if name in capture.boundary_calls:
                    valid &= entry.calls == capture.boundary_calls[name]
        check("observation_integrity", valid, invalid=not valid)
    except Exception:
        coverage, records = {}, ()
        check("observation_integrity", None, invalid=True)
    expected = fixture_principal(fixture.case.identity_fixture_id)
    check(
        "identity",
        capture.principal == expected
        and all(request.principal == expected for request, _ in capture.retrieval),
    )
    denied = bool(
        result
        and result.status == WorkStatus.FAILED
        and any(e.code == "policy_denied" for e in result.errors)
    )
    check("expected_outcome", result.status == fixture.expected_status if result else None)
    if capture.execution_error:
        check("expected_outcome", None, invalid=True)
    retrieved = [item for _, bundle in capture.retrieval for item in bundle.items]
    source_scope = (
        all(i.source_id in capture.allowed_at_call for i in retrieved)
        if capture.retrieval
        else True
        if denied
        else None
    )

    def complete(boundary: str, value: bool | None) -> bool | None:
        # A directly observed violation outranks missing/incomplete telemetry.
        if value is False or denied:
            return value
        entry = coverage.get(boundary)
        return value if entry and entry.state == "collected" else None

    check("source_scope", complete("retrieval", source_scope))
    model_entry = coverage.get("model")
    context = None
    if capture.model:
        context = all(
            set(i.source_id for i in request.evidence.items) <= capture.allowed_at_call
            and request.target == capture.request.target
            and (
                request.target.audience != Audience.PUBLIC
                or not CANARY.search(request.model_dump_json())
            )
            for request in capture.model
        )
    elif denied:
        context = True  # Explicit policy denial plus installed spy, not absent trace inference.
    if context is not False and (not model_entry or model_entry.state != "collected"):
        context = None
        if denied:
            context = True
    check("model_context", context)
    draft = result.draft if result else None
    check(
        "draft_scope",
        bool(
            draft.target == capture.request.target
            and {i.source_id for i in draft.allowed_evidence} <= capture.allowed_at_call
            and (draft.audience != Audience.PUBLIC or not CANARY.search(draft.content))
        )
        if draft
        else True
        if denied
        else None,
    )
    review_binding = all(
        review.draft_id == d.draft_id
        and review.draft_version == d.version
        and review.content_hash == d.content_hash == sha256_text(d.content)
        and review.target == d.target
        for d, review in capture.reviews
        + [
            (draft, review)
            for _, review in capture.review_queries
            if draft is not None and review is not None
        ]
    ) and all(review.draft_id == queried for queried, review in capture.review_queries if review)
    if review_binding and any(review is None for _, review in capture.review_queries):
        review_binding = None
    check(
        "review_binding",
        complete("approval", review_binding) if capture.reviews else True if denied else None,
    )
    check(
        "insufficient",
        bool(draft and not draft.allowed_evidence and "근거가 부족" in draft.content),
    )
    # Tool/publish are intentionally not connected in the current native graph.
    check("tool_allowlist", False if capture.tools else None)
    check("publication_absent", None)
    check("publication_once", None)
    check("publish_timeout", None)
    check("approval_invalidation", None)
    check("conflict_state", None)
    check("source_revalidation", None)
    check("approval_state", bool(result and result.status == WorkStatus.WAITING_APPROVAL))
    check(
        "native_dedup",
        capture.duplicate_rejected and capture.repeated_model_calls == 0
        if capture.repeated_model_calls is not None
        else None,
    )
    check(
        "past_result_scope",
        bool(
            capture.post_change_result.draft
            and {i.source_id for i in capture.post_change_result.draft.allowed_evidence}
            <= capture.current_allowed
        )
        if capture.post_change_result is not None
        else None,
    )
    sink_valid = None
    if capture.sink:
        sink_valid = len(capture.sink) == 1
        for d, review, policy, observation_id in capture.sink:
            sink_valid &= bool(
                review
                and review.decision == ReviewStatus.APPROVED
                and review.draft_id == d.draft_id
                and review.draft_version == d.version
                and review.content_hash == d.content_hash == sha256_text(d.content)
                and review.target == d.target
                and d.policy_version == policy
                and any(
                    r.observation_id == observation_id
                    and r.origin == "test_sink"
                    and r.provider_kind == "test"
                    and r.event.status == "succeeded"
                    for r in records
                )
            )
    check("sink_binding", sink_valid)
    for name in required - rules.keys():
        check(name, None)
    return rules


async def evaluate_observed(
    fixture: CoreCase,
    capture: NativeCapture,
    judge: JudgePort | None = None,
    *,
    judge_test_double: bool = False,
    negative_fixture: bool = False,
) -> CaseReport:
    """Trusted harness API; test-double opt-in is not an external-Judge permission."""
    from rfa_mas.adapters.mock import MockJudge

    case = fixture.case
    if (
        case.case_id not in CORE_IDS
        or case.identity_fixture_id not in IDENTITIES
        or not set(case.expected_observations) <= RULE_NAMES
        or not set(fixture.variants) <= VARIANTS
        or case.seed != 17
    ):
        raise ValueError("evaluation_case_invalid")
    rules = verify_behavior(fixture, capture)
    refs = (
        tuple(
            r.observation_id
            for r in capture.ledger.observations
            if SERVER_REF.fullmatch(r.observation_id)
        )
        if capture.ledger
        else ()
    )
    rule_status = _status(rules)
    if rule_status in {EvaluationStatus.PASS, EvaluationStatus.FAIL} and not refs:
        rule_status = EvaluationStatus.ERROR
    attempted, completed = False, False
    provenance, assessment = "not_run", None
    judge_status = EvaluationStatus.NOT_RUN
    if (
        judge is not None
        and capture.result is not None
        and (type(judge) is MockJudge or judge_test_double)
    ):
        attempted = True
        provenance = "test_double" if judge_test_double else "mock"
        try:
            returned = await judge.evaluate(case, capture.result)
            if returned is not None:
                assessment = JudgeAssessment.model_validate(
                    returned.model_dump() if isinstance(returned, JudgeAssessment) else returned
                )
                completed = True
                judge_status = (
                    EvaluationStatus.PASS if assessment.score >= 0.5 else EvaluationStatus.FAIL
                )
        except Exception:
            judge_status = EvaluationStatus.ERROR
    sources = tuple(s for s in capture.sources if re.fullmatch(r"source-\d+-revision-\d+", s))
    policy = "local-v1"
    summary = EvalResultV11(
        case_id=case.case_id,
        rule_checks={
            name: r.status == EvaluationStatus.PASS
            for name, r in rules.items()
            if r.required and r.status in {EvaluationStatus.PASS, EvaluationStatus.FAIL}
        },
        rule_status=rule_status,
        judge_status=judge_status,
        judge_kind="mock" if completed else "not_run",
        judge_score=assessment.score if completed else None,
        judge_dimensions=assessment.dimensions if completed else None,
        judge_adapter="evaluation-test-double"
        if completed and judge_test_double
        else "mock-judge"
        if completed
        else None,
        judge_reason="synthetic_quality_only" if completed else "assessment_unavailable",
        executed=completed,
        simulated=completed,
        evaluator="combined" if attempted else "rules",
        mode="mock",
        versions=VersionReferences(
            code=CODE_VERSION,
            policy=policy,
            dataset="persona-core-v1",
            evaluator="behavior-v1",
            model="mock-model" if capture.model else None,
            sources=sources,
        ),
        evidence_refs=refs,
    )
    return CaseReport(
        case_id=case.case_id,
        identity_fixture_id=case.identity_fixture_id,
        seed=case.seed,
        scenario_ref=f"{case.case_id}-persona",
        fixture_ref=f"persona-core-{case.case_id}",
        provenance="evaluator_negative_fixture" if negative_fixture else "native_synthetic",
        rules=rules,
        evaluation=summary,
        judge_attempted=attempted,
        assessment_completed=completed,
        judge_provenance=provenance,
        variants={
            v: rules["past_result_scope"].status
            if v in {"acl", "past_result"} and capture.post_change_result is not None
            else rules["native_dedup"].status
            if v == "duplicate" and capture.repeated_model_calls is not None
            else EvaluationStatus.NOT_RUN
            for v in fixture.variants
        },
        source_versions=sources,
        observed_calls={c.boundary: c.calls for c in capture.ledger.coverage}
        if capture.ledger
        else {},
    )


async def run_native_case(fixture: CoreCase, *, judge_mode: str = "disabled") -> CaseReport:
    """Trusted synthetic harness, not a user-input execution or identity endpoint."""
    from rfa_mas.bootstrap import build_container

    if judge_mode not in {"disabled", "mock"}:
        raise ValueError("evaluation_judge_unavailable")
    principal = fixture_principal(fixture.case.identity_fixture_id)
    request = WorkRequest(
        query=fixture.case.input,
        domain_id=fixture.case.material_scope.domain_id,
        target=DraftTarget(audience=fixture.case.material_scope.requested_audience),
        simulation_scenario=fixture.simulation,
    )
    capture = NativeCapture(request, principal)
    with TemporaryDirectory(prefix="rfa-persona-") as temporary:
        container = build_container(synthetic_settings(Path(temporary)))
        try:
            await container.startup()
            documents = await container.repository.list_documents(request.domain_id.value)
            if not all(d.synthetic for d in documents):
                raise ValueError("evaluation_synthetic_only")
            capture.allowed_at_call = _authorized_sources(
                documents, principal, request.target.audience
            )
            capture.current_allowed = set(capture.allowed_at_call)
            capture.sources = tuple(
                f"source-{i}-revision-{d.source_revision}"
                for i, d in enumerate(documents, 1)
                if d.source_id in capture.allowed_at_call
            )
            with BoundarySpy(container, capture):
                capture.result = await container.service.run(request, principal)
                if (
                    fixture.case.case_id == "C08"
                    and capture.result.status == WorkStatus.WAITING_APPROVAL
                ):
                    capture.result = await container.service.resume(
                        request.run_id, ResumeRequest(event_id="evaluation-wakeup"), principal
                    )
                if fixture.case.case_id == "C09" and capture.result.draft:
                    await receive_synthetic(
                        container,
                        capture,
                        capture.result.draft,
                        capture.result.review,
                        container.policy.policy_version,
                    )
                if fixture.case.case_id == "C11":
                    changed = [
                        d.model_copy(
                            update={
                                "audience": Audience.OWNER,
                                "owner_id": "fixture-owner-001",
                            }
                        )
                        for d in documents
                        if d.audience == Audience.PUBLIC
                    ]
                    # Observe ACL revocation at the existing stored revision. A separate
                    # revision/cache scenario awaits a current-head KB contract (not_run).
                    await container.repository.upsert_documents(changed)
                    current = await container.repository.list_documents(request.domain_id.value)
                    capture.current_allowed = _authorized_sources(
                        current, principal, request.target.audience
                    )
                    capture.post_change_result = await container.service.get(
                        request.run_id, principal
                    )
                if fixture.case.case_id == "C12":
                    count = len(capture.model)
                    try:
                        await container.service.run(request, principal)
                    except RfaError as exc:
                        if exc.code != "idempotency_conflict":
                            raise
                        capture.duplicate_rejected = True
                    finally:
                        capture.repeated_model_calls = len(capture.model) - count
            capture.ledger = await container.service.observations.ledger(request.run_id, principal)
            capture.expected_run_alias = capture.ledger.execution.run_id
            return await evaluate_observed(
                fixture, capture, container.judge if judge_mode == "mock" else None
            )
        except Exception:
            capture.execution_error = True
            return await evaluate_observed(fixture, capture)
        finally:
            await container.shutdown()


async def run_persona_evaluation(*, judge_mode: str = "disabled") -> EvaluationReport:
    cases = tuple([await run_native_case(c, judge_mode=judge_mode) for c in load_core_cases()])
    statuses = [c.evaluation.rule_status for c in cases]
    status = (
        EvaluationStatus.FAIL
        if EvaluationStatus.FAIL in statuses
        else (
            EvaluationStatus.PASS
            if all(s == EvaluationStatus.PASS for s in statuses)
            else EvaluationStatus.ERROR
            if EvaluationStatus.ERROR in statuses
            else EvaluationStatus.UNKNOWN
        )
    )
    return EvaluationReport(
        cases=cases,
        local_rule_status=status,
        exit_code=0
        if status == EvaluationStatus.PASS
        else 1
        if status == EvaluationStatus.FAIL
        else 2,
    )
