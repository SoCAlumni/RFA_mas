from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

TASK_ID = re.compile(r"^[A-Z][A-Z0-9]*-[0-9]{3}[A-Z]?$")
Status = Literal["todo", "in_progress", "verifying", "done", "blocked", "deferred", "cancelled"]


class ControlError(Exception):
    """Safe, operator-facing failure without input payloads or secrets."""


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader: UniqueLoader, node: Any, deep: bool = False) -> dict:
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ControlError("YAML mapping keys must be unique strings")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def parse_yaml(value: str) -> dict:
    try:
        result = yaml.load(value, Loader=UniqueLoader)
    except yaml.YAMLError as exc:
        raise ControlError("Invalid YAML") from exc
    if not isinstance(result, dict):
        raise ControlError("Expected a YAML mapping")
    return result


def relative_path(value: str, *, glob: bool = False) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or value in {".", "/"}
        or (not glob and any(char in value for char in "*?["))
    ):
        raise ControlError("Unsafe relative path")
    return value


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Dependency(Model):
    id: str
    condition: Literal["integrated_done"] = "integrated_done"
    required_output: str = Field(min_length=1)


class ContractRef(Model):
    id: str
    version: str
    digest: str | None
    role: Literal["provider", "consumer"]


class Scope(Model):
    included: list[str]
    excluded: list[str]
    owned_paths: list[str]
    read_only_paths: list[str]
    planned_paths: list[str]
    shared_resources: list[str]


class ReadRef(Model):
    path: str
    section: str
    reason: str


class Context(Model):
    global_refs: list[str]
    read: list[ReadRef]
    handoff: str
    dependency_results: list[str]


class Deliverable(Model):
    path: str
    availability: Literal["existing", "planned"]
    description: str


class AC(Model):
    id: str
    given: str = Field(min_length=1)
    expect: str = Field(min_length=1)
    required: bool = True


class Verification(Model):
    id: str
    ac_ids: list[str]
    type: Literal["command", "manual", "API", "UI"]
    availability: Literal["available", "planned"]
    prerequisites: list[str]
    cwd: str = "."
    argv: list[str] = Field(default_factory=list)
    timeout_seconds: int = Field(default=120, ge=1, le=3600)
    procedure: list[str] = Field(default_factory=list)
    assertions: list[str]
    # Only explicit pytest/ruff runners are executable. Manual plans are never auto-passed.
    runner: Literal["pytest", "ruff", "manual"] = "manual"


class VerificationSummary(Model):
    state: Literal["not_run", "passed", "failed", "stale"] = "not_run"
    spec_revision: int | None = None
    spec_digest: str | None = None
    contract_digest: str | None = None
    source_fingerprint: str | None = None
    evidence: str | None = None


class Claim(Model):
    session_id: str
    worktree: str
    branch: str
    generation: int = Field(ge=1)
    claimed_at: str
    heartbeat_at: str
    lease_expires_at: str
    baseline_head: str
    recovery_required: bool = False
    context_digest: str | None = None


class Attempts(Model):
    count: int = 0
    cycles: int = 0
    no_progress_count: int = 0
    latest_attempt: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    approaches: list[dict[str, Any]] = Field(default_factory=list)


class Integration(Model):
    state: Literal["not_required", "pending", "integrated", "conflict"] = "pending"
    target: str
    reason: str | None = None
    reservation: bool = False
    submission_generation: int | None = None
    submission: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    evidence: str | None = None


class Blocker(Model):
    cause: str
    evidence: list[str]
    resolve_when: str
    affects: list[str]


class Migration(Model):
    original_status: str
    source_ref: str
    prior_claim: str
    implementation_facts: str
    previous_evidence: list[str]
    checks_this_migration: list[str]
    judgment: str
    initial_spec_revision: int = 1


class Task(Model):
    schema_version: Literal["1.0"] = "1.0"
    id: str
    title: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    kind: Literal["task", "acceptance", "control", "group", "milestone"] = "task"
    source_tasks: list[str]
    requirement_refs: list[str]
    priority: Literal["P0", "P1", "P2"]
    status: Status
    spec_state: Literal["draft", "ready"]
    unresolved: list[str] = Field(default_factory=list)
    workstream: str
    execution_role: Literal["worker", "coordinator"] = "worker"
    estimated_effort: float = Field(ge=0)
    product_owner: str
    execution_class: str
    product_mode: str
    scope: Scope
    depends_on: list[Dependency]
    contract_refs: list[ContractRef]
    context: Context
    deliverables: list[Deliverable]
    acceptance_criteria: list[AC]
    verification: list[Verification]
    evidence_dir: str
    latest_evidence_file: str | None = None
    verification_summary: VerificationSummary = Field(default_factory=VerificationSummary)
    integration: Integration
    revision: int = Field(default=1, ge=1)
    spec_revision: int = Field(default=1, ge=1)
    claim_generation: int = 0
    claim: Claim | None = None
    attempts: Attempts = Field(default_factory=Attempts)
    blocker: Blocker | None = None
    migration: Migration
    updated_at: str
    concise_result: str
    next_action: str = Field(min_length=1)

    @model_validator(mode="after")
    def invariants(self) -> Task:
        if not TASK_ID.fullmatch(self.id):
            raise ValueError("invalid development task ID")
        if self.spec_state == "ready" and self.unresolved:
            raise ValueError("ready specification has unresolved decisions")
        if not self.scope.owned_paths or not self.deliverables or not self.requirement_refs:
            raise ValueError("scope, deliverables, and requirements are required")
        ac_ids = [item.id for item in self.acceptance_criteria]
        ver_ids = [item.id for item in self.verification]
        if not ac_ids or len(ac_ids) != len(set(ac_ids)) or len(ver_ids) != len(set(ver_ids)):
            raise ValueError("empty or duplicate AC/verification IDs")
        covered = set()
        for check in self.verification:
            if not check.ac_ids or not set(check.ac_ids) <= set(ac_ids) or not check.assertions:
                raise ValueError("verification must reference ACs and explicit assertions")
            if check.type == "command" and not check.argv:
                raise ValueError("command verification requires argv")
            if check.type != "command" and not check.procedure:
                raise ValueError("manual/API/UI verification requires a procedure")
            if check.cwd != ".":
                relative_path(check.cwd)
            covered.update(check.ac_ids)
        if any(item.required and item.id not in covered for item in self.acceptance_criteria):
            raise ValueError("required AC has no verification")
        for path in self.scope.owned_paths + self.scope.read_only_paths + self.scope.planned_paths:
            relative_path(path, glob=True)
        for path in self.context.global_refs + [self.context.handoff, self.evidence_dir]:
            relative_path(path)
        for ref in self.context.read:
            relative_path(ref.path)
        for artifact in self.deliverables:
            relative_path(artifact.path, glob=True)
        for path in (self.latest_evidence_file, self.verification_summary.evidence):
            if path:
                relative_path(path)
        if self.evidence_dir != f".agent/evidence/{self.id}":
            raise ValueError("evidence directory is not task-scoped")
        if self.context.handoff != f"tasks/{self.id}/handoff.md":
            raise ValueError("handoff is not task-scoped")
        if self.integration.state == "not_required" and not self.integration.reason:
            raise ValueError("not_required integration needs a reason")
        if self.status == "blocked" and self.blocker is None:
            raise ValueError("blocked task needs evidence and an unblock condition")
        if self.claim and self.claim.generation != self.claim_generation:
            raise ValueError("claim generation mismatch")
        return self


SPEC_FIELDS = frozenset(
    {
        "title",
        "objective",
        "kind",
        "source_tasks",
        "requirement_refs",
        "priority",
        "spec_state",
        "unresolved",
        "workstream",
        "execution_role",
        "estimated_effort",
        "product_owner",
        "execution_class",
        "product_mode",
        "scope",
        "depends_on",
        "contract_refs",
        "context",
        "deliverables",
        "acceptance_criteria",
        "verification",
    }
)

TRANSITIONS = {
    "todo": {"in_progress", "blocked", "deferred", "cancelled"},
    "in_progress": {"verifying", "blocked", "todo", "cancelled"},
    "verifying": {"in_progress", "done", "blocked", "cancelled"},
    "blocked": {"todo", "in_progress", "cancelled", "deferred"},
    "deferred": {"todo", "cancelled"},
    "done": {"verifying"},
    "cancelled": set(),
}
