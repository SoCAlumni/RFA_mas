"""Task-control checks use only temporary controls and synthetic Git worktrees.

These are management-system tests, not RFA product acceptance evidence. In particular,
the synthetic manual reports below exercise validation, not a claimed product result.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import ValidationError

from scripts.tasklib.cli import execute, parser
from scripts.tasklib.evidence import (
    check_report,
    record_attempt,
    source_manifest,
    start_attempt,
    valid_evidence,
)
from scripts.tasklib.schema import ControlError, Task, parse_yaml, relative_path
from scripts.tasklib.store import Store, dump, overlaps, spec_digest

TASKCTL = Path(__file__).resolve().parents[1] / "scripts/taskctl.py"
STAMP = "2026-09-26T00:00:00+00:00"
HANDOFF = """# Synthetic fixture handoff

Goal and AC: AC-1 observes the fixture change. No RFA product claim is made.
Current fact: the temporary source is inspected; the verification record is linked.
Decision: use a manual fixture assertion to test the control protocol.
Failure and effects: none outside this temporary directory.
Next first action: coordinator checks the target evidence before integrating.
"""


def git_call(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
        env={"PATH": os.environ.get("PATH", ""), "LANG": "C", "GIT_CONFIG_NOSYSTEM": "1"},
    )
    return result.stdout.strip()


def minimal_task(task_id: str, filename: str, contract_digest: str) -> dict:
    return {
        "schema_version": "1.0",
        "id": task_id,
        "title": f"Isolated control fixture {task_id}",
        "objective": "Verify observable control behavior without product side effects",
        "kind": "task",
        "source_tasks": [task_id],
        "requirement_refs": ["docs/requirements.md#control"],
        "priority": "P0",
        "status": "todo",
        "spec_state": "ready",
        "workstream": "execution",
        "execution_role": "worker",
        "estimated_effort": 0.5,
        "product_owner": "fixture",
        "execution_class": "isolated management test",
        "product_mode": "simulated_fixture_not_product_acceptance",
        "scope": {
            "included": ["Synthetic control test"],
            "excluded": ["Real RFA product work"],
            "owned_paths": [filename],
            "read_only_paths": [],
            "planned_paths": [filename],
            "shared_resources": [],
        },
        "depends_on": [],
        "contract_refs": [
            {
                "id": "fixture-contract",
                "version": "1",
                "digest": contract_digest,
                "role": "consumer",
            },
        ],
        "context": {
            "global_refs": ["docs/PROJECT_CONTEXT.md"],
            "read": [
                {
                    "path": "docs/requirements.md",
                    "section": "control",
                    "reason": "Read the synthetic control expectation",
                }
            ],
            "handoff": f"tasks/{task_id}/handoff.md",
            "dependency_results": [],
        },
        "deliverables": [
            {"path": filename, "availability": "planned", "description": "Synthetic source fixture"}
        ],
        "acceptance_criteria": [
            {
                "id": "AC-1",
                "given": "An isolated fixture source",
                "expect": "The change exists",
                "required": True,
            },
        ],
        "verification": [
            {
                "id": "V-1",
                "ac_ids": ["AC-1"],
                "type": "manual",
                "availability": "available",
                "prerequisites": ["Temporary fixture only"],
                "cwd": ".",
                "argv": [],
                "timeout_seconds": 30,
                "procedure": ["Inspect the synthetic fixture change"],
                "assertions": ["The isolated fixture change is present"],
                "runner": "manual",
            },
        ],
        "evidence_dir": f".agent/evidence/{task_id}",
        "integration": {"state": "pending", "target": "fixture-main"},
        "migration": {
            "original_status": "todo",
            "source_ref": "docs/requirements.md",
            "prior_claim": "None",
            "implementation_facts": "Synthetic fixture only",
            "previous_evidence": [],
            "checks_this_migration": [],
            "judgment": "No product completion is implied",
        },
        "updated_at": STAMP,
        "concise_result": "Not implemented",
        "next_action": "Claim the isolated fixture after checking readiness",
    }


@dataclass
class ControlFixture:
    root: Path
    target: Path
    first: Path
    second: Path
    handoff_file: Path

    def run(self, *arguments: str) -> dict:
        args = parser().parse_args(["--control-root", str(self.root), *map(str, arguments)])
        return execute(Store(self.root), args)

    def task(self, task_id: str = "DEV-001") -> Task:
        return Store(self.root).tasks()[task_id]

    def put(self, task: Task | dict) -> None:
        data = task.model_dump(mode="json") if isinstance(task, Task) else task
        path = self.root / "tasks" / data["id"] / "task.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dump(data))

    def claim(self, task_id="DEV-001", session="worker-a", source=None) -> dict:
        return self.run(
            "claim",
            task_id,
            "--session",
            session,
            "--source",
            source or self.first,
            "--expected-revision",
            str(self.task(task_id).revision),
        )

    def owned(self, command: str, *extra: str, task_id="DEV-001", session="worker-a") -> dict:
        task = self.task(task_id)
        return self.run(
            command,
            task_id,
            "--session",
            session,
            "--expected-revision",
            str(task.revision),
            "--generation",
            str(task.claim_generation),
            *extra,
        )

    def commit_change(self, source=None, filename="src/a.py", value="VALUE = 2\n") -> None:
        source = source or self.first
        (source / filename).write_text(value)
        git_call(source, "add", filename)
        git_call(source, "commit", "-m", "Synthetic fixture change")

    def report(self, task_id="DEV-001") -> dict:
        task = self.task(task_id)
        check = task.verification[0]
        return {
            "reviewer": "isolated-test-fixture-not-product-review",
            "started_at": STAMP,
            "finished_at": STAMP,
            "checks": [
                {
                    "id": check.id,
                    "result": "passed",
                    "assertions": check.assertions,
                    "observed": "Synthetic fixture observation",
                    "procedure_performed": True,
                }
            ],
        }

    def evidence(
        self, attempt="worker-1", *, stage="worker", task_id="DEV-001", session=None
    ) -> None:
        session = session or ("coordinator" if stage == "integration" else "worker-a")
        self.owned(
            "begin-evidence",
            "--attempt",
            attempt,
            "--stage",
            stage,
            session=session,
            task_id=task_id,
        )
        report = self.root.parent / f"{attempt}-report.json"
        report.write_text(json.dumps(self.report(task_id)))
        self.owned(
            "record-evidence",
            "--attempt",
            attempt,
            "--stage",
            stage,
            "--report",
            str(report),
            session=session,
            task_id=task_id,
        )

    def submit(self) -> None:
        self.owned("submit", "--handoff-file", str(self.handoff_file))


@pytest.fixture
def control(tmp_path: Path) -> Iterator[ControlFixture]:
    # Separate temp control and Git paths expose accidental writes to a source task copy.
    root, target = tmp_path / "control", tmp_path / "source"
    root.mkdir()
    target.mkdir()
    git_call(target, "init", "-b", "fixture-main")
    git_call(target, "config", "user.name", "Synthetic fixture")
    git_call(target, "config", "user.email", "fixture@example.invalid")
    (target / "src").mkdir()
    (target / "src/a.py").write_text("VALUE = 1\n")
    (target / "src/b.py").write_text("VALUE = 1\n")
    (target / ".gitignore").write_text(".env*\n!.env.example\n")
    git_call(target, "add", "src", ".gitignore")
    git_call(target, "commit", "-m", "Synthetic baseline")
    first, second = tmp_path / "worker-a", tmp_path / "worker-b"
    git_call(target, "worktree", "add", "-b", "worker-a", str(first))
    git_call(target, "worktree", "add", "-b", "worker-b", str(second))

    (root / ".agent").mkdir()
    (root / "tasks").mkdir()
    (root / "docs").mkdir()
    (root / "docs/PROJECT_CONTEXT.md").write_text(
        "Synthetic test context; no product acceptance.\n"
    )
    (root / "docs/requirements.md").write_text("# Control\nTemporary task-control behavior only.\n")
    contract = root / "docs/contract.json"
    contract.write_text('{"fixture_version": 1}\n')
    contract_hash = hashlib.sha256(contract.read_bytes()).hexdigest()
    (root / ".agent/control.json").write_text(
        json.dumps(
            {
                "repository_id": "isolated-fixture",
                "control_root": str(root.resolve()),
            }
        )
    )
    project = {
        "schema_version": "1.0",
        "repository_id": "isolated-fixture",
        "control_root": str(root.resolve()),
        "updated_at": STAMP,
        "coordinator_sessions": ["coordinator"],
        "max_workers": 2,
        "lease_seconds": 900,
        "cycle_limit": 3,
        "no_progress_limit": 2,
        "workstreams": {"execution": {}},
        "shared_resources": {"test-database": {}},
        "contracts": {
            "fixture-contract": {
                "path": "docs/contract.json",
                "version": "1",
                "digest": contract_hash,
                "state": "ready",
            }
        },
        "fingerprint_inputs": [],
        "migration": {"legacy_ids": ["DEV-001", "DEV-002"]},
        "final_acceptance": {"local": ["DEV-003"], "real": []},
        "integration_target": {
            "state": "registered",
            "worktree": str(target.resolve()),
            "branch": "fixture-main",
            "baseline_head": git_call(target, "rev-parse", "HEAD"),
            "git_common_dir": git_call(
                target, "rev-parse", "--path-format=absolute", "--git-common-dir"
            ),
        },
    }
    (root / "tasks/project.yaml").write_text(dump(project))
    handoff_file = tmp_path / "handoff-input.md"
    handoff_file.write_text(HANDOFF)
    fixture = ControlFixture(root, target, first, second, handoff_file)
    for task_id, filename in [
        ("DEV-001", "src/a.py"),
        ("DEV-002", "src/b.py"),
        ("DEV-003", "src/acceptance.py"),
    ]:
        task = minimal_task(task_id, filename, contract_hash)
        if task_id == "DEV-003":
            task["kind"] = "acceptance"
            task["depends_on"] = [{"id": "DEV-001", "required_output": "Integrated fixture"}]
        fixture.put(task)
        (root / "tasks" / task_id / "handoff.md").write_text(HANDOFF)
    fixture.run("refresh")
    try:
        yield fixture
    finally:
        # Bound temporary disk usage on small developer machines. These four paths
        # were created exclusively by this fixture, never a repository/user path.
        for directory in [root, target, first, second]:
            if directory.is_dir():
                shutil.rmtree(directory)


def test_duplicate_yaml_keys_are_rejected() -> None:
    with pytest.raises(ControlError, match="unique"):
        parse_yaml("outer:\n  id: first\n  id: second\n")


@pytest.mark.parametrize("path", ["../outside", "src/../../x", "/tmp/escape", "a\\b", "."])
def test_path_escape_rejected(path: str) -> None:
    with pytest.raises(ControlError, match="Unsafe"):
        relative_path(path)


def test_schema_rejects_uncovered_ac_unknown_verification_ac_and_unresolved_ready(control) -> None:
    original = control.task().model_dump(mode="json")
    for modification in [
        {"verification": []},
        {"verification": [{**original["verification"][0], "ac_ids": ["AC-missing"]}]},
        {"unresolved": ["The target identity is unknown"]},
    ]:
        with pytest.raises(ValidationError):
            Task.model_validate({**original, **modification})


@pytest.mark.parametrize(
    "mutation, message",
    [
        ("dependency", "Unknown/self dependency"),
        ("contract", "Unknown contract"),
        ("cycle", "Dependency cycle"),
        ("missing_context", "Unmarked missing context"),
        ("missing_existing_output", "Existing deliverable missing"),
        ("unmapped_legacy", "Unmapped legacy"),
    ],
)
def test_reference_and_migration_validation(control, mutation, message) -> None:
    task = control.task().model_dump(mode="json")
    if mutation == "dependency":
        task["depends_on"] = [{"id": "DEV-999", "required_output": "Missing"}]
    elif mutation == "contract":
        task["contract_refs"][0]["id"] = "unknown"
    elif mutation == "cycle":
        task["depends_on"] = [{"id": "DEV-003", "required_output": "Creates a cycle"}]
    elif mutation == "missing_context":
        task["context"]["read"][0]["path"] = "docs/missing.md"
    elif mutation == "missing_existing_output":
        task["deliverables"][0]["availability"] = "existing"
    else:
        project = parse_yaml((control.root / "tasks/project.yaml").read_text())
        project["migration"]["legacy_ids"].append("DEV-999")
        (control.root / "tasks/project.yaml").write_text(dump(project))
    control.put(task)
    with pytest.raises(ControlError, match=message):
        control.run("refresh")


def test_duplicate_or_misplaced_id_and_symlink_escape_rejected(control, tmp_path) -> None:
    original = control.task()
    misplaced = control.root / "tasks/DEV-004"
    misplaced.mkdir()
    (misplaced / "task.yaml").write_text(dump(original.model_dump(mode="json")))
    with pytest.raises(ControlError, match="Duplicate/misplaced"):
        Store(control.root).tasks()
    (misplaced / "task.yaml").unlink()
    (control.root / "docs/escape").symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(ControlError, match="escape|symlink"):
        Store(control.root).path("docs/escape/result.json")


def test_draft_spec_and_unregistered_baseline_cannot_be_claimed(control) -> None:
    task = control.task()
    task.spec_state = "draft"
    task.unresolved = ["Review the missing scope choice"]
    control.put(task)
    with pytest.raises(ControlError, match="spec unresolved"):
        control.claim()
    task.spec_state, task.unresolved = "ready", []
    control.put(task)
    project = parse_yaml((control.root / "tasks/project.yaml").read_text())
    project["integration_target"]["state"] = "unregistered"
    (control.root / "tasks/project.yaml").write_text(dump(project))
    row = control.run("ready")["tasks"][0]
    assert row["planning_ready"] is True
    assert row["executable"] is False
    with pytest.raises(ControlError, match="unregistered"):
        control.claim()


def concurrent_claims(control: ControlFixture, pairs: list[tuple[str, str, Path]]) -> list:
    processes = []
    for task_id, session, source in pairs:
        arguments = [
            sys.executable,
            str(TASKCTL),
            "--control-root",
            str(control.root),
            "claim",
            task_id,
            "--session",
            session,
            "--source",
            str(source),
            "--expected-revision",
            "1",
        ]
        processes.append(
            subprocess.Popen(
                arguments,
                cwd=source,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={"PATH": os.environ.get("PATH", ""), "LANG": "en_US.UTF-8"},
            )
        )
    outputs = [process.communicate(timeout=30) for process in processes]
    return [
        (process.returncode, stdout, stderr)
        for process, (stdout, stderr) in zip(processes, outputs, strict=True)
    ]


def test_two_processes_claim_same_task_exactly_one_succeeds(control) -> None:
    results = concurrent_claims(
        control,
        [
            ("DEV-001", "worker-a", control.first),
            ("DEV-001", "worker-b", control.second),
        ],
    )
    assert sorted(result[0] for result in results) == [0, 2], results
    task = control.task()
    assert task.status == "in_progress" and task.revision == 2
    assert task.claim.session_id in {"worker-a", "worker-b"}
    assert task.claim_generation == 1


@pytest.mark.parametrize("collision", ["path", "parent", "glob", "resource"])
def test_concurrent_different_tasks_reject_colliding_scope(control, collision) -> None:
    a, b = control.task(), control.task("DEV-002")
    if collision == "resource":
        a.scope.shared_resources = ["test-database"]
        b.scope.shared_resources = ["test-database"]
    else:
        a.scope.owned_paths = {"path": ["src/b.py"], "parent": ["src"], "glob": ["src/*.py"]}[
            collision
        ]
    control.put(a)
    control.put(b)
    results = concurrent_claims(
        control,
        [
            (a.id, "worker-a", control.first),
            (b.id, "worker-b", control.second),
        ],
    )
    assert sorted(result[0] for result in results) == [0, 2], results
    failed = next(result for result in results if result[0] == 2)
    assert "reserved:" in failed[2]


def test_independent_tasks_claim_from_distinct_worktrees(control) -> None:
    results = concurrent_claims(
        control,
        [
            ("DEV-001", "worker-a", control.first),
            ("DEV-002", "worker-b", control.second),
        ],
    )
    assert [result[0] for result in results] == [0, 0], results
    assert len([task for task in Store(control.root).tasks().values() if task.claim]) == 2


def test_ambiguous_glob_is_conservative_and_sibling_files_are_independent() -> None:
    assert overlaps("src/*/dto.py", "src/agents/a.py")
    assert overlaps("*/shared.py", "another/tree")
    assert not overlaps("src/a.py", "src/b.py")


def test_stale_revision_generation_and_foreign_owner_updates_rejected(control) -> None:
    control.claim()
    original = control.task()
    control.owned("heartbeat")
    with pytest.raises(ControlError, match="Stale revision"):
        control.run(
            "heartbeat",
            original.id,
            "--session",
            "worker-a",
            "--generation",
            "1",
            "--expected-revision",
            str(original.revision),
        )
    for generation, session in [(0, "worker-a"), (1, "other-session")]:
        with pytest.raises(ControlError, match="Stale/foreign"):
            control.run(
                "heartbeat",
                original.id,
                "--session",
                session,
                "--generation",
                str(generation),
                "--expected-revision",
                str(control.task().revision),
            )


def test_lease_expiry_preserves_scope_requires_inspection_and_fences_old_owner(control) -> None:
    control.claim()
    task = control.task()
    task.claim.lease_expires_at = "2000-01-01T00:00:00+00:00"
    control.put(task)
    other = control.task("DEV-002")
    other.scope.owned_paths = task.scope.owned_paths
    control.put(other)
    assert control.run("status")["tasks"][0]["recovery_required"] is True
    with pytest.raises(ControlError, match="Recovery required"):
        control.owned("heartbeat")
    with pytest.raises(ControlError, match="reserved"):
        control.claim("DEV-002", "worker-b", control.second)
    inspection = control.root.parent / "inspection.json"
    inspection.write_text("{}")
    recovery = [
        "recover",
        task.id,
        "--session",
        "coordinator",
        "--source",
        str(control.first),
        "--expected-revision",
        str(task.revision),
        "--new-session",
        "replacement",
        "--disposition",
        "resume",
        "--inspection-file",
        str(inspection),
        "--handoff-file",
        str(control.handoff_file),
        "--reason",
        "Inspected process and work",
    ]
    with pytest.raises(ControlError, match="inspection incomplete"):
        control.run(*recovery)
    inspection.write_text(
        json.dumps(
            {
                "old_process_stopped_or_fenced": True,
                "worktree_reviewed": True,
                "unintegrated_changes_reviewed": True,
                "side_effects_reconciled": True,
            }
        )
    )
    control.run(*recovery)
    recovered = control.task()
    assert recovered.claim_generation == 2 and recovered.claim.session_id == "replacement"
    with pytest.raises(ControlError, match="Stale/foreign"):
        control.run(
            "submit",
            task.id,
            "--session",
            "worker-a",
            "--generation",
            "1",
            "--expected-revision",
            str(recovered.revision),
            "--handoff-file",
            str(control.handoff_file),
        )


def test_heartbeat_changes_revision_not_spec_or_source_fingerprint(control) -> None:
    control.claim()
    before_task = control.task()
    before = source_manifest(Store(control.root), before_task, control.first)
    control.owned("heartbeat")
    after_task = control.task()
    after = source_manifest(Store(control.root), after_task, control.first)
    assert after_task.revision == before_task.revision + 1
    assert after_task.spec_revision == before_task.spec_revision
    assert spec_digest(before_task) == spec_digest(after_task)
    assert before["fingerprint"] == after["fingerprint"]


def test_source_manifest_dirty_untracked_and_secret_exclusions(control) -> None:
    task = control.task()
    task.scope.owned_paths = ["src", ".env.example", ".env.synthetic", "credentials.json"]
    first = source_manifest(Store(control.root), task, control.first)
    (control.first / "src/a.py").write_text("VALUE = 2\n")
    git_call(control.first, "add", "src/a.py")
    (control.first / "src/a.py").write_text("VALUE = 3\n")
    (control.first / "src/untracked.py").write_text("VALUE = 4\n")
    # Only synthetic secret files under the pytest tmp directory; never real .env.
    (control.first / ".env.synthetic").write_text("PRIVATE_FIXTURE=never-collect-this\n")
    (control.first / "credentials.json").write_text('{"private_fixture": "never-collect-this"}')
    (control.first / ".env.example").write_text("API_KEY=\n")
    after = source_manifest(Store(control.root), task, control.first)
    assert first["head"] == after["head"] and first["fingerprint"] != after["fingerprint"]
    assert after["files"]["src/a.py"]["staged"] is True
    assert after["files"]["src/a.py"]["unstaged"] is True
    assert after["files"]["src/a.py"]["index_blob"]
    assert after["files"]["src/untracked.py"]["untracked"] is True
    assert ".env.example" in after["files"]
    assert ".env.synthetic" not in after["files"] and "credentials.json" not in after["files"]
    assert "never-collect-this" not in json.dumps(after)


def test_spec_and_contract_changes_invalidate_fingerprint(control) -> None:
    store, task = Store(control.root), control.task()
    original = source_manifest(store, task, control.first)
    task.spec_revision += 1
    assert source_manifest(store, task, control.first)["fingerprint"] != original["fingerprint"]
    task.spec_revision -= 1
    (control.root / "docs/contract.json").write_text('{"fixture_version": 2}\n')
    assert source_manifest(store, task, control.first)["fingerprint"] != original["fingerprint"]
    assert store.contract_ready(task) is False


def test_evidence_immutable_and_source_change_during_attempt_rejected(control) -> None:
    store, task = Store(control.root), control.task()
    start_attempt(store, task, control.first, "attempt-1")
    with pytest.raises(ControlError, match="immutable"):
        start_attempt(store, task, control.first, "attempt-1")
    (control.first / "src/a.py").write_text("VALUE = 9\n")
    with pytest.raises(ControlError, match="changed since capture"):
        record_attempt(store, task, control.first, "attempt-1", control.report())
    assert not (control.root / task.evidence_dir / "attempt-1/result.json").exists()


@pytest.mark.parametrize(
    "counts",
    [
        {"collected": 0, "passed": 0},
        {"collected": 2, "passed": 1, "skipped": 1},
        {"collected": 1, "passed": 1, "errors": 1},
        {"collected": 1, "passed": 1, "deselected": 1},
    ],
)
def test_empty_skip_error_or_deselected_pytest_is_not_passed(control, counts) -> None:
    task = control.task()
    plan = task.verification[0]
    plan.type, plan.runner = "command", "pytest"
    plan.argv = ["python", "-m", "pytest", "tests/test_fixture.py"]
    report = control.report()
    report["checks"][0].update(
        {
            "argv": plan.argv,
            "cwd": ".",
            "exit_code": 0,
            "log_excerpt": "Synthetic test counts",
            "counts": counts,
        }
    )
    with pytest.raises(ControlError, match="not passed"):
        check_report(task, report)


def test_missing_manual_observation_and_ac_cannot_pass(control) -> None:
    report = control.report()
    del report["checks"][0]["observed"]
    with pytest.raises(ControlError, match="observation"):
        check_report(control.task(), report)
    report["checks"] = []
    assert check_report(control.task(), report) == "failed"


def test_worker_pass_submit_is_not_done_does_not_release_dependency_or_scope(control) -> None:
    control.claim()
    control.commit_change()
    control.evidence()
    assert control.task().verification_summary.state == "passed"
    assert control.task().status == "in_progress"
    control.submit()
    task = control.task()
    assert task.status == "verifying" and task.claim is None
    assert task.integration.state == "pending" and task.integration.reservation is True
    rows = {row["id"]: row for row in control.run("status")["tasks"]}
    assert "dependency:DEV-001" in rows["DEV-003"]["reasons"]
    other = control.task("DEV-002")
    other.scope.owned_paths = task.scope.owned_paths
    control.put(other)
    with pytest.raises(ControlError, match="reserved"):
        control.claim("DEV-002", "worker-b", control.second)
    with pytest.raises(ControlError, match="Target integration evidence"):
        control.owned("close", session="coordinator")


def test_failed_or_stale_worker_evidence_cannot_submit(control) -> None:
    control.claim()
    control.commit_change()
    with pytest.raises(ControlError, match="not passed"):
        control.submit()
    control.evidence()
    control.commit_change(value="VALUE = 3\n")
    with pytest.raises(ControlError, match="Stale verification"):
        control.submit()


def test_submit_rejects_old_generation_and_scope_escape(control) -> None:
    control.claim()
    control.commit_change()
    control.commit_change(filename="src/b.py", value="VALUE = 8\n")
    control.evidence()
    with pytest.raises(ControlError, match="out-of-scope"):
        control.submit()
    task = control.task()
    with pytest.raises(ControlError, match="Stale/foreign"):
        control.run(
            "submit",
            task.id,
            "--session",
            "worker-a",
            "--generation",
            "0",
            "--expected-revision",
            str(task.revision),
            "--handoff-file",
            str(control.handoff_file),
        )


def test_integrate_then_close_requires_target_evidence_and_releases_dependency(control) -> None:
    control.claim()
    control.commit_change()
    control.evidence()
    control.submit()
    git_call(control.target, "merge", "--ff-only", "worker-a")
    with pytest.raises(ControlError, match="Target integration evidence"):
        control.owned("integrate", session="coordinator")
    control.evidence("target-1", stage="integration")
    with pytest.raises(ControlError, match="Integrate and provide"):
        control.owned("close", session="coordinator")
    control.owned("integrate", session="coordinator")
    assert control.task().status == "verifying"
    control.owned("close", session="coordinator")
    completed = control.task()
    assert completed.status == "done" and completed.integration.state == "integrated"
    assert completed.integration.reservation is False
    assert Store(control.root).complete(completed) is True
    ready = {row["id"]: row for row in control.run("ready")["tasks"]}
    assert ready["DEV-003"]["executable"] is True
    control.run("validate")


def test_source_saved_survives_view_generation_failure_and_refresh_repairs(control, monkeypatch):
    original = Store.refresh

    def fail_view(_store, _tasks):
        raise OSError("Injected view failure after atomic source save")

    monkeypatch.setattr(Store, "refresh", fail_view)
    result = control.claim()
    assert result["source_saved"] is True and result["views"] == "repair_required"
    assert control.task().status == "in_progress" and control.task().revision == 2
    monkeypatch.setattr(Store, "refresh", original)
    with pytest.raises(ControlError, match="stale/tampered"):
        control.run("validate")
    assert control.run("refresh")["repaired"] is True
    assert control.run("validate")["valid"] is True


def test_derived_view_tampering_detected_and_refresh_has_no_noisy_diff(control):
    paths = [control.root / name for name in ["tasks/index.yaml", "tasks/state.yaml", "TASKS.md"]]
    original = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
    control.run("refresh")
    assert original == [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
    state = parse_yaml(paths[1].read_text())
    state["tasks"][0]["status"] = "done"
    paths[1].write_text(dump(state))
    with pytest.raises(ControlError, match="stale/tampered"):
        control.run("validate")
    assert control.run("status")["repaired_views"] is True
    assert paths[1].read_bytes() == original[1][0]
    assert control.task().status == "todo"


def test_control_root_mismatch_never_falls_back_and_other_worktree_sees_same_owner(control):
    control.claim()
    result = subprocess.run(
        [sys.executable, str(TASKCTL), "show", "DEV-001"],
        cwd=control.second,
        env={"PATH": os.environ.get("PATH", ""), "TASK_CONTROL_ROOT": str(control.root)},
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    assert json.loads(result.stdout)["claim"]["session_id"] == "worker-a"
    marker = control.root / ".agent/control.json"
    value = json.loads(marker.read_text())
    value["control_root"] = str(control.second)
    marker.write_text(json.dumps(value))
    with pytest.raises(ControlError, match="wrong control root"):
        Store(control.root)
    result = subprocess.run(
        [sys.executable, str(TASKCTL), "status"],
        cwd=control.second,
        env={"PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 2 and "required; no local fallback" in result.stderr
    assert not (control.second / "tasks/state.yaml").exists()


def test_single_task_context_pack_contains_direct_results_not_all_specs_or_logs(control):
    unrelated = control.task("DEV-002")
    unrelated.objective = "UNRELATED_FULL_SPEC_MUST_NOT_BE_IN_PACK"
    control.put(unrelated)
    pack = control.run("context-pack", "DEV-003")
    assert pack["task_id"] == "DEV-003" and pack["spec_revision"] == 1
    assert pack["contract_digest"] and pack["global_digests"] and pack["baseline"]
    assert pack["control_root"] == str(control.root.resolve())
    assert [item["id"] for item in pack["direct_predecessors"]] == ["DEV-001"]
    assert "Next first action" in pack["handoff"]
    assert "UNRELATED_FULL_SPEC_MUST_NOT_BE_IN_PACK" not in json.dumps(pack)
    assert "does not reset conversation context" in pack["state_instruction"]


def test_valid_evidence_rechecks_source_instead_of_trusting_passed_status(control):
    control.claim()
    control.commit_change()
    control.evidence()
    store, task = Store(control.root), control.task()
    assert valid_evidence(store, task, control.first)["result"] == "passed"
    (control.first / "src/a.py").write_text("VALUE = 99\n")
    with pytest.raises(ControlError, match="Stale verification"):
        valid_evidence(store, task, control.first)


def test_model_not_required_integration_without_reason_is_rejected(control):
    data = deepcopy(control.task().model_dump(mode="json"))
    data["integration"]["state"] = "not_required"
    with pytest.raises(ValidationError, match="reason"):
        Task.model_validate(data)


def test_group_task_cannot_be_claimed(control):
    task = control.task()
    task.kind = "group"
    control.put(task)
    with pytest.raises(ControlError, match="non-executable group"):
        control.claim()


def test_claim_rejects_target_checkout_wrong_repository_and_occupied_worktree(control, tmp_path):
    with pytest.raises(ControlError, match="separate feature worktree"):
        control.claim(source=control.target)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    git_call(unrelated, "init", "-b", "other")
    with pytest.raises(ControlError, match="another repository"):
        control.claim(source=unrelated)
    control.claim()
    with pytest.raises(ControlError, match="One active executor"):
        control.claim("DEV-002", "worker-b", control.first)


def test_new_claim_requires_current_integrated_head_not_an_old_feature_branch(control):
    control.commit_change(source=control.target)
    with pytest.raises(ControlError, match="baseline|current|latest|integration|HEAD"):
        control.claim()
    git_call(control.first, "merge", "--ff-only", "fixture-main")
    control.claim()
    assert control.task().claim.baseline_head == git_call(control.target, "rev-parse", "HEAD")


def test_coordinator_only_shared_task_cannot_be_claimed_by_worker(control):
    task = control.task()
    task.execution_role = "coordinator"
    control.put(task)
    with pytest.raises(ControlError, match="Coordinator session"):
        control.claim()
    control.claim(session="coordinator", source=control.target)
    assert control.task().claim.session_id == "coordinator"


def test_report_unknown_check_missing_assertion_and_secret_payload_rejected(control):
    store, task = Store(control.root), control.task()
    report = control.report()
    report["checks"][0]["id"] = "missing-check"
    with pytest.raises(ControlError, match="Unknown/duplicate verification"):
        check_report(task, report)
    report = control.report()
    report["checks"][0]["assertions"] = []
    with pytest.raises(ControlError, match="planned assertions"):
        check_report(task, report)
    report = control.report()
    report["checks"][0]["observed"] = "Bearer synthetic-only-never-ingest"
    start_attempt(store, task, control.first, "redaction")
    with pytest.raises(ControlError, match="Potential secret"):
        record_attempt(store, task, control.first, "redaction", report)
    assert not (control.root / task.evidence_dir / "redaction/result.json").exists()


def test_atomic_write_failure_preserves_previous_source(control, monkeypatch):
    original = (control.root / "tasks/DEV-001/task.yaml").read_bytes()

    def fail_replace(_source, _target):
        raise OSError("Injected failure before source replace")

    monkeypatch.setattr("scripts.tasklib.store.os.replace", fail_replace)
    with pytest.raises(OSError, match="before source replace"):
        control.claim()
    assert (control.root / "tasks/DEV-001/task.yaml").read_bytes() == original
    assert not list((control.root / "tasks/DEV-001").glob(".taskctl-*"))


def test_release_records_blocker_and_preserves_unintegrated_scope(control):
    control.claim()
    control.commit_change()
    control.owned(
        "release",
        "--handoff-file",
        str(control.handoff_file),
        "--reason",
        "Source changes need explicit coordinator review",
        "--resolve-when",
        "Inspect and integrate or discard the exact changes",
    )
    task = control.task()
    assert task.status == "blocked" and task.claim is None and task.integration.reservation
    assert task.blocker and task.blocker.resolve_when
    other = control.task("DEV-002")
    other.scope.owned_paths = task.scope.owned_paths
    control.put(other)
    with pytest.raises(ControlError, match="reserved"):
        control.claim("DEV-002", "worker-b", control.second)


def test_edit_spec_is_coordinator_only_and_increments_spec_revision(control):
    patch = control.root.parent / "spec-change.yaml"
    patch.write_text(dump({"objective": "New reviewed fixture goal"}))
    with pytest.raises(ControlError, match="Coordinator session"):
        control.run(
            "edit-spec",
            "DEV-001",
            "--session",
            "worker-a",
            "--expected-revision",
            "1",
            "--patch",
            str(patch),
            "--reason",
            "Fixture change",
        )
    control.run(
        "edit-spec",
        "DEV-001",
        "--session",
        "coordinator",
        "--expected-revision",
        "1",
        "--patch",
        str(patch),
        "--reason",
        "Fixture change",
    )
    assert control.task().spec_revision == 2 and control.task().revision == 2
    patch.write_text(dump({"status": "done"}))
    with pytest.raises(ControlError, match="cannot set operational state"):
        control.run(
            "edit-spec",
            "DEV-001",
            "--session",
            "coordinator",
            "--expected-revision",
            "2",
            "--patch",
            str(patch),
            "--reason",
            "Attempt invalid completion",
        )


def test_begin_evidence_retry_budget_requires_new_coordinator_approach(control):
    control.claim()
    task = control.task()
    task.attempts.cycles = 3
    control.put(task)
    with pytest.raises(ControlError, match="Retry budget exhausted"):
        control.owned("begin-evidence", "--attempt", "should-not-start")
    assert not (control.root / task.evidence_dir / "should-not-start").exists()


@pytest.mark.parametrize("changed", ["source", "contract", "specification"])
def test_completed_task_reconciles_stale_evidence_and_reblocks_dependency(control, changed):
    control.claim()
    control.commit_change()
    control.evidence()
    control.submit()
    git_call(control.target, "merge", "--ff-only", "worker-a")
    control.evidence("target-1", stage="integration")
    control.owned("integrate", session="coordinator")
    control.owned("close", session="coordinator")
    if changed == "source":
        (control.target / "src/a.py").write_text("VALUE = 10\n")
    elif changed == "contract":
        (control.root / "docs/contract.json").write_text('{"fixture_version": 2}\n')
    else:
        task = control.task()
        task.acceptance_criteria[0].expect = "A newly required observable result"
        task.spec_revision += 1
        control.put(task)
    rows = {row["id"]: row for row in control.run("status")["tasks"]}
    assert rows["DEV-001"]["status"] == "verifying"
    assert rows["DEV-001"]["verification"] == "stale"
    assert "dependency:DEV-001" in rows["DEV-003"]["reasons"]
    assert control.task().integration.reservation is True


def test_unrelated_target_change_does_not_invalidate_completed_task(control):
    control.claim()
    control.commit_change()
    control.evidence()
    control.submit()
    git_call(control.target, "merge", "--ff-only", "worker-a")
    control.evidence("target-1", stage="integration")
    control.owned("integrate", session="coordinator")
    control.owned("close", session="coordinator")
    (control.target / "src/b.py").write_text("UNRELATED = 9\n")
    rows = {row["id"]: row for row in control.run("status")["tasks"]}
    assert rows["DEV-001"]["status"] == "done"
    assert rows["DEV-001"]["verification"] == "passed"


def completed_consumer(control):
    """Establish real temporary submission and target evidence, not a done flag."""
    control.claim()
    control.commit_change()
    control.evidence()
    control.submit()
    git_call(control.target, "merge", "--ff-only", "worker-a")
    control.evidence("target-1", stage="integration")
    control.owned("integrate", session="coordinator")
    control.owned("close", session="coordinator")
    return control.task().model_copy(deep=True)


def completed_then_changed(control):
    """Real temp Git history: integrated A, followed by legitimate A and unrelated B."""
    original = completed_consumer(control)
    control.commit_change(control.target, "src/a.py", "VALUE = 3\n")
    control.commit_change(control.target, "src/b.py", "VALUE = 4\n")
    control.run("status")
    assert control.task().status == "verifying"
    assert control.task().integration.reservation
    return original


def prepare_contract_update(control, *, change_bytes=True, version="2"):
    path = control.root / "docs/contract.json"
    if change_bytes:
        path.write_text(json.dumps({"fixture_version": version}) + "\n")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    # Synthetic management evidence only: not a real schema/provider verification.
    (control.root / "docs/contract-proof.json").write_text(
        json.dumps({"schema_checked": True, "fixtures_checked": True, "contract_digest": digest})
    )
    return digest


def publish_fixture_contract(control, *, version="2"):
    return control.run(
        "publish-contract",
        "--session",
        "coordinator",
        "--contract",
        "fixture-contract",
        "--version",
        version,
        "--expected-project-digest",
        control.run("project-digest")["project_digest"],
        "--evidence",
        "docs/contract-proof.json",
        "--reason",
        "Temporary consumer contract lifecycle regression",
        "--compatibility",
        "Synthetic fixture only",
        "--migration",
        "Explicit baseline acceptance and new required AC verification",
    )


def accept_fixture_contract(control, changes=None, *, task_id="DEV-001"):
    task = control.task(task_id)
    registry = Store(control.root).project["contracts"]["fixture-contract"]
    refs = [ref.model_dump(mode="json") for ref in task.contract_refs]
    refs[0].update(version=registry["version"], digest=registry["digest"])
    patch = {"contract_refs": refs, "spec_state": "ready", "unresolved": []}
    patch.update(changes or {})
    path = control.root.parent / "contract-acceptance.yaml"
    path.write_text(dump(patch))
    return control.run(
        "edit-spec",
        task_id,
        "--session",
        "coordinator",
        "--expected-revision",
        str(task.revision),
        "--patch",
        str(path),
        "--reason",
        "Review and accept the published fixture baseline only",
    )


@pytest.mark.parametrize("change_bytes", [True, False])
def test_integrated_contract_publication_acceptance_retains_reservation_and_reverification(
    control, change_bytes
):
    original = completed_consumer(control)
    original_evidence = (control.root / original.integration.evidence).read_bytes()
    digest = prepare_contract_update(control, change_bytes=change_bytes)
    publish_fixture_contract(control)
    published = control.task()
    assert published.status == "verifying" and published.claim is None
    assert published.verification_summary.state == "stale"
    assert published.spec_state == "draft" and published.integration.reservation
    assert published.contract_refs == original.contract_refs
    assert not Store(control.root).complete(published)
    with pytest.raises(ControlError):
        control.owned("close", session="coordinator")
    accept_fixture_contract(control)
    accepted = control.task()
    assert accepted.contract_refs[0].version == "2" and accepted.contract_refs[0].digest == digest
    assert accepted.spec_state == "ready" and accepted.unresolved == []
    assert accepted.integration == original.integration.model_copy(update={"reservation": True})
    assert accepted.verification_summary.state == "stale" and accepted.status == "verifying"
    assert accepted.latest_evidence_file == original.latest_evidence_file
    assert (control.root / original.integration.evidence).read_bytes() == original_evidence
    with pytest.raises(ControlError):
        control.owned("close", session="coordinator")
    rows = {row["id"]: row for row in control.run("status")["tasks"]}
    assert "dependency:DEV-001" in rows["DEV-003"]["reasons"]

    inspected_recover(control)
    with pytest.raises(ControlError, match="not passed"):
        control.submit()
    control.evidence("accepted-worker")
    control.submit()
    with pytest.raises(ControlError, match="Target integration evidence required"):
        control.owned("close", session="coordinator")
    control.evidence("accepted-target", stage="integration")
    control.owned("integrate", session="coordinator")
    control.owned("close", session="coordinator")
    assert Store(control.root).complete(control.task())
    rows = {row["id"]: row for row in control.run("status")["tasks"]}
    assert "dependency:DEV-001" not in rows["DEV-003"]["reasons"]
    assert (control.root / original.integration.evidence).read_bytes() == original_evidence


@pytest.mark.parametrize("stage", ["active", "pending", "fake_integrated"])
def test_contract_publication_keeps_active_unintegrated_and_unbound_reservations_blocked(
    control, stage
):
    control.claim()
    if stage != "active":
        control.commit_change()
        control.evidence()
        control.submit()
        if stage == "fake_integrated":
            task = control.task()
            task.integration.state = "integrated"
            task.verification_summary.state = "stale"
            control.put(task)
    prepare_contract_update(control)
    original_project = (control.root / "tasks/project.yaml").read_bytes()
    with pytest.raises(ControlError):
        publish_fixture_contract(control)
    with pytest.raises(ControlError):
        accept_fixture_contract(control)
    assert (control.root / "tasks/project.yaml").read_bytes() == original_project


@pytest.mark.parametrize(
    "damage", ["evidence_result", "manifest_head", "target_branch", "unrelated_history"]
)
@pytest.mark.parametrize("operation", ["publish", "accept"])
def test_reserved_contract_change_requires_historical_binding_and_canonical_branch(
    control, damage, operation
):
    original = completed_consumer(control)
    prepare_contract_update(control)
    if operation == "accept":
        publish_fixture_contract(control)
    control.run("status")
    if damage == "target_branch":
        git_call(control.target, "switch", "-c", "wrong-target")
    else:
        evidence_path = control.root / original.integration.evidence
        evidence = json.loads(evidence_path.read_text())
        if damage == "evidence_result":
            evidence["result"] = "failed"
            evidence_path.write_text(json.dumps(evidence))
        else:
            manifest_path = control.root / evidence["source_manifest"]
            manifest = json.loads(manifest_path.read_text())
            if damage == "manifest_head":
                manifest["head"] = git_call(control.target, "rev-parse", "HEAD^")
            else:
                tree = git_call(control.target, "rev-parse", "HEAD^{tree}")
                unrelated = git_call(control.target, "commit-tree", tree, "-m", "Unrelated fixture")
                manifest["head"] = unrelated
                task = control.task()
                task.integration.result["head"] = unrelated
                control.put(task)
            manifest_path.write_text(json.dumps(manifest))
    original_project = (control.root / "tasks/project.yaml").read_bytes()
    with pytest.raises(ControlError):
        (publish_fixture_contract if operation == "publish" else accept_fixture_contract)(control)
    assert (control.root / "tasks/project.yaml").read_bytes() == original_project
    assert control.task().integration.reservation


@pytest.mark.parametrize(
    "mutation",
    ["scope", "ac", "dependency", "role", "drop_ref", "add_ref", "digest", "version", "unresolved"],
)
def test_reserved_contract_acceptance_cannot_rewrite_scope_authority_or_unrelated_blockers(
    control, mutation
):
    completed_consumer(control)
    prepare_contract_update(control)
    publish_fixture_contract(control)
    task = control.task()
    data = task.model_dump(mode="json")
    refs = deepcopy(data["contract_refs"])
    refs[0].update(
        version="2", digest=Store(control.root).project["contracts"]["fixture-contract"]["digest"]
    )
    changes = {}
    if mutation == "scope":
        changes["scope"] = data["scope"] | {"owned_paths": ["src/b.py"]}
    elif mutation == "ac":
        changes["acceptance_criteria"] = data["acceptance_criteria"]
        changes["acceptance_criteria"][0]["expect"] = "Bypass the original requirement"
    elif mutation == "dependency":
        changes["depends_on"] = [{"id": "DEV-002", "required_output": "New requirement"}]
    elif mutation == "unresolved":
        task.unresolved.append("Unresolved independent authority decision")
        control.put(task)
    else:
        if mutation == "role":
            refs[0]["role"] = "provider"
        elif mutation == "drop_ref":
            refs = []
        elif mutation == "add_ref":
            refs.append(deepcopy(refs[0]))
        elif mutation == "digest":
            refs[0]["digest"] = "a" * 64
        elif mutation == "version":
            refs[0]["version"] = "unpublished"
        changes["contract_refs"] = refs
    before = (control.root / "tasks/DEV-001/task.yaml").read_bytes()
    with pytest.raises(ControlError):
        accept_fixture_contract(control, changes)
    assert (control.root / "tasks/DEV-001/task.yaml").read_bytes() == before


def test_contract_acceptance_preserves_unrelated_blocker_in_draft(control):
    completed_consumer(control)
    prepare_contract_update(control)
    publish_fixture_contract(control)
    task = control.task()
    task.unresolved.append("Independent reviewed question")
    control.put(task)
    with pytest.raises(ControlError, match="unresolved ready"):
        accept_fixture_contract(control, {"unresolved": ["Independent reviewed question"]})
    accept_fixture_contract(
        control, {"spec_state": "draft", "unresolved": ["Independent reviewed question"]}
    )
    assert control.task().unresolved == ["Independent reviewed question"]
    assert control.task().spec_state == "draft" and control.task().integration.reservation
    with pytest.raises(ControlError, match="Reconcile specification/contract"):
        inspected_recover(control)


def test_latest_contract_acceptance_clears_only_publisher_recorded_intermediate_notices(control):
    original = completed_consumer(control)
    for version in ("2", "3"):
        prepare_contract_update(control, version=version)
        publish_fixture_contract(control, version=version)
    notices = control.task().unresolved
    assert len(notices) == 2
    task = control.task()
    lookalike = "Re-read fixture-contract 99; coordinator edit-spec must accept new digest"
    task.unresolved.append(lookalike)
    control.put(task)
    with pytest.raises(ControlError, match="unrelated unresolved"):
        accept_fixture_contract(control)
    accept_fixture_contract(control, {"spec_state": "draft", "unresolved": [lookalike]})
    accepted = control.task()
    assert accepted.contract_refs[0].version == "3"
    assert accepted.unresolved == [lookalike]
    assert accepted.integration == original.integration.model_copy(update={"reservation": True})


def test_repeated_publication_can_accept_latest_ready_and_recover(control):
    completed_consumer(control)
    for version in ("2", "3"):
        prepare_contract_update(control, version=version)
        publish_fixture_contract(control, version=version)
    accept_fixture_contract(control)
    accepted = control.task()
    assert accepted.spec_state == "ready" and accepted.unresolved == []
    assert accepted.contract_refs[0].version == "3" and accepted.integration.reservation
    inspected_recover(control)
    assert control.task().status == "in_progress"
    assert control.task().verification_summary.state == "stale"


def test_accepting_contract_ref_without_clearing_its_notice_is_atomic_rejection(control):
    completed_consumer(control)
    prepare_contract_update(control)
    publish_fixture_contract(control)
    before = (control.root / "tasks/DEV-001/task.yaml").read_bytes()
    with pytest.raises(ControlError, match="same specification change"):
        accept_fixture_contract(
            control, {"spec_state": "draft", "unresolved": control.task().unresolved}
        )
    assert (control.root / "tasks/DEV-001/task.yaml").read_bytes() == before
    accept_fixture_contract(control)
    assert control.task().spec_state == "ready"


def inspected_recover(
    control,
    *,
    revalidate=True,
    source=None,
    session="coordinator",
    task_id="DEV-001",
    new_session="worker-a",
):
    inspection = control.root.parent / "revalidation-inspection.json"
    inspection.write_text(
        json.dumps(
            {
                "old_process_stopped_or_fenced": True,
                "worktree_reviewed": True,
                "unintegrated_changes_reviewed": True,
                "side_effects_reconciled": True,
                "integrated_revalidation": revalidate,
            }
        )
    )
    return control.run(
        "recover",
        task_id,
        "--session",
        session,
        "--source",
        str(source or control.first),
        "--expected-revision",
        str(control.task(task_id).revision),
        "--new-session",
        new_session,
        "--disposition",
        "resume",
        "--inspection-file",
        str(inspection),
        "--handoff-file",
        str(control.handoff_file),
        "--reason",
        "Reviewed subsequent integrated source; reverify current artifacts",
    )


@pytest.mark.parametrize("overlap", ["paths", "resources"])
def test_overlapping_integrated_reservations_reverify_serially_without_releasing_history(
    control, overlap
):
    if overlap == "resources":
        first = control.task()
        first.scope.shared_resources = ["test-database"]
        control.put(first)
    completed_consumer(control)
    second = control.task("DEV-002")
    if overlap == "paths":
        second.scope.owned_paths.append("src/a.py")
    else:
        second.scope.shared_resources = ["test-database"]
    control.put(second)
    git_call(control.second, "merge", "--ff-only", "fixture-main")
    control.claim("DEV-002", "worker-b", control.second)
    # B's independent change preserves A's verified files; both really reach done.
    control.commit_change(control.second, "src/b.py")
    control.evidence("overlap-b-worker", task_id="DEV-002", session="worker-b")
    control.owned(
        "submit",
        "--handoff-file",
        str(control.handoff_file),
        task_id="DEV-002",
        session="worker-b",
    )
    git_call(control.target, "merge", "--ff-only", "worker-b")
    control.evidence("overlap-b-target", task_id="DEV-002", stage="integration")
    control.owned("integrate", task_id="DEV-002", session="coordinator")
    control.owned("close", task_id="DEV-002", session="coordinator")
    assert control.task().status == control.task("DEV-002").status == "done"

    prepare_contract_update(control)
    publish_fixture_contract(control)
    for task_id in ("DEV-001", "DEV-002"):
        accept_fixture_contract(control, task_id=task_id)
        assert control.task(task_id).integration.reservation
    git_call(control.first, "merge", "--ff-only", "fixture-main")
    second_before = control.task("DEV-002")
    evidence_path = control.root / second_before.integration.evidence
    evidence_bytes = evidence_path.read_bytes()
    with pytest.raises(ControlError, match="Recovery conflicts"):
        inspected_recover(control, revalidate=False)
    corrupted = json.loads(evidence_bytes)
    corrupted["result"] = "failed"
    evidence_path.write_text(json.dumps(corrupted))
    with pytest.raises(ControlError, match="Recovery conflicts"):
        inspected_recover(control)
    evidence_path.write_bytes(evidence_bytes)  # Restore only injected temporary fixture corruption.
    inspected_recover(control)
    assert control.task("DEV-002") == second_before

    def recover_second():
        return inspected_recover(
            control, source=control.second, task_id="DEV-002", new_session="worker-b"
        )

    with pytest.raises(ControlError, match="Recovery conflicts"):
        recover_second()
    first_active = control.task()
    expired = first_active.model_copy(deep=True)
    expired.claim.lease_expires_at = "2000-01-01T00:00:00+00:00"
    control.put(expired)
    with pytest.raises(ControlError, match="Recovery conflicts"):
        recover_second()
    control.put(first_active)
    control.evidence("overlap-a-reverified")
    control.submit()
    with pytest.raises(ControlError, match="Recovery conflicts"):
        recover_second()  # A passed but is not yet integrated; its pending reservation blocks B.
    control.evidence("overlap-a-target", stage="integration")
    control.owned("integrate", session="coordinator")
    control.owned("close", session="coordinator")
    recover_second()
    assert control.task("DEV-002").status == "in_progress"
    assert control.task("DEV-002").verification_summary.state == "stale"
    assert evidence_path.read_bytes() == evidence_bytes


@pytest.mark.parametrize("owned_fix", [False, True])
def test_integrated_revalidation_uses_current_baseline_retains_history_and_new_gates(
    control, owned_fix
):
    original = completed_then_changed(control)
    old_evidence = (control.root / original.integration.evidence).read_bytes()
    git_call(control.first, "merge", "--ff-only", "fixture-main")
    baseline = git_call(control.target, "rev-parse", "HEAD")
    inspected_recover(control)
    recovered = control.task()
    assert (
        recovered.claim.baseline_head
        == baseline
        != original.integration.submission["baseline_head"]
    )
    approach = recovered.attempts.approaches[-1]
    assert approach["kind"] == "integrated_revalidation"
    assert approach["integration"] == original.integration.model_dump(mode="json") | {
        "reservation": True
    }
    assert approach["latest_evidence_file"] == original.latest_evidence_file
    assert recovered.integration.result is None and recovered.integration.evidence is None
    assert recovered.verification_summary.state == "stale"
    with pytest.raises(ControlError, match="not passed"):
        control.submit()
    with pytest.raises(ControlError, match="Stale/foreign"):
        control.run(
            "submit",
            "DEV-001",
            "--session",
            "worker-a",
            "--generation",
            "1",
            "--expected-revision",
            str(recovered.revision),
            "--handoff-file",
            str(control.handoff_file),
        )
    if owned_fix:
        control.commit_change(value="VALUE = 5\n")
    control.evidence("worker-revalidation")
    control.submit()
    submitted = control.task()
    assert submitted.integration.submission["baseline_head"] == baseline
    assert set(submitted.integration.submission["changed_files"]) == {"src/a.py"}
    assert not Store(control.root).readiness(control.task("DEV-003"), Store(control.root).tasks())[
        "executable"
    ]
    with pytest.raises(ControlError, match="Target integration evidence"):
        control.owned("close", session="coordinator")
    if owned_fix:
        git_call(control.target, "merge", "--ff-only", "worker-a")
    control.evidence("target-revalidation", stage="integration")
    with pytest.raises(ControlError, match="Integrate"):
        control.owned("close", session="coordinator")
    control.owned("integrate", session="coordinator")
    control.owned("close", session="coordinator")
    assert Store(control.root).complete(control.task())
    assert Store(control.root).readiness(control.task("DEV-003"), Store(control.root).tasks())[
        "executable"
    ]
    assert control.task().latest_evidence_file != original.latest_evidence_file
    assert (control.root / original.integration.evidence).read_bytes() == old_evidence


@pytest.mark.parametrize("invalid", ["old_branch", "dirty", "wrong_session", "missing_evidence"])
def test_revalidation_baseline_reset_rejects_invalid_inputs(control, invalid):
    completed_then_changed(control)
    if invalid != "old_branch":
        git_call(control.first, "merge", "--ff-only", "fixture-main")
    if invalid == "dirty":
        (control.first / "src/b.py").write_text("DIRTY = True\n")
    if invalid == "missing_evidence":
        task = control.task()
        task.integration.evidence = None
        control.put(task)
    before = control.task().model_dump(mode="json")
    with pytest.raises(ControlError):
        inspected_recover(
            control, session="worker-a" if invalid == "wrong_session" else "coordinator"
        )
    assert control.task().model_dump(mode="json") == before


@pytest.mark.parametrize("released", [False, True])
def test_pending_recovery_keeps_original_baseline_and_cannot_launder_scope(control, released):
    control.claim()
    baseline = control.task().claim.baseline_head
    control.commit_change(filename="src/b.py")  # Unintegrated, out-of-scope worker edit.
    if released:
        control.owned(
            "release",
            "--handoff-file",
            str(control.handoff_file),
            "--reason",
            "Inspect pending work",
        )
    else:
        task = control.task()
        task.claim.lease_expires_at = "2000-01-01T00:00:00+00:00"
        control.put(task)
    with pytest.raises(ControlError):
        inspected_recover(control)
    inspected_recover(control, revalidate=False)
    assert control.task().claim.baseline_head == baseline
    control.evidence("pending-recovered")
    with pytest.raises(ControlError, match="out-of-scope"):
        control.submit()


def test_revalidation_does_not_waive_dirty_or_out_of_scope_submission(control):
    completed_then_changed(control)
    git_call(control.first, "merge", "--ff-only", "fixture-main")
    inspected_recover(control)
    (control.first / "src/b.py").write_text("DIRTY = True\n")
    control.evidence("revalidation-dirty")
    with pytest.raises(ControlError, match="clean feature"):
        control.submit()
    git_call(control.first, "add", "src/b.py")
    git_call(control.first, "commit", "-m", "Out-of-scope revalidation change")
    with pytest.raises(ControlError, match="out-of-scope"):
        control.submit()


def test_all_planned_checks_required_even_when_they_reference_the_same_ac(control):
    task = control.task()
    second = task.verification[0].model_copy(deep=True)
    second.id = "V-2"
    second.assertions = ["The separate safety check also passed"]
    task.verification.append(second)
    assert check_report(task, control.report()) == "failed"

def _race_integration(control, attempt="target-race"):
    """Integration evidence where another session commits an unrelated file mid-attempt."""
    control.claim()
    control.commit_change()
    control.evidence()
    control.submit()
    git_call(control.target, "merge", "--ff-only", "worker-a")
    control.owned(
        "begin-evidence", "--attempt", attempt, "--stage", "integration", session="coordinator"
    )
    captured = git_call(control.target, "rev-parse", "HEAD")
    control.commit_change(control.target, "src/b.py", "UNRELATED = 9\n")
    report = control.root.parent / f"{attempt}-report.json"
    report.write_text(json.dumps(control.report()))
    control.owned(
        "record-evidence",
        "--attempt",
        attempt,
        "--stage",
        "integration",
        "--report",
        str(report),
        session="coordinator",
    )
    return captured


def test_concurrent_target_commit_binds_captured_head_and_revalidation_accepts_it(control):
    captured = _race_integration(control)
    assert control.task().integration.result["head"] == captured
    assert captured != git_call(control.target, "rev-parse", "HEAD")
    control.owned("integrate", session="coordinator")
    control.owned("close", session="coordinator")
    control.commit_change(control.target, "src/a.py", "VALUE = 3\n")
    control.run("status")
    assert control.task().status == "verifying"
    git_call(control.first, "merge", "--ff-only", "fixture-main")
    inspected_recover(control)
    assert control.task().attempts.approaches[-1]["kind"] == "integrated_revalidation"


@pytest.mark.parametrize("legacy", ["descendant", "unrelated"])
def test_legacy_advanced_result_head_is_tolerated_only_as_descendant(control, legacy):
    captured = _race_integration(control)
    control.owned("integrate", session="coordinator")
    control.owned("close", session="coordinator")
    task = control.task()
    if legacy == "descendant":
        # Pre-OPS-003 records stored the (later) target head at record time.
        task.integration.result["head"] = git_call(control.target, "rev-parse", "HEAD")
    else:
        git_call(control.target, "checkout", "-q", "-b", "side", captured + "~1")
        control.commit_change(control.target, "src/b.py", "SIDE = 1\n")
        side = git_call(control.target, "rev-parse", "HEAD")
        git_call(control.target, "checkout", "-q", "fixture-main")
        task.integration.result["head"] = side
    control.put(task)
    control.commit_change(control.target, "src/a.py", "VALUE = 3\n")
    control.run("status")
    git_call(control.first, "merge", "--ff-only", "fixture-main")
    if legacy == "descendant":
        inspected_recover(control)
        assert control.task().claim is not None
    else:
        before = control.task().model_dump(mode="json")
        with pytest.raises(ControlError):
            inspected_recover(control)
        assert control.task().model_dump(mode="json") == before
