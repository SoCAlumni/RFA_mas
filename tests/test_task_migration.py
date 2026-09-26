"""Read-only real migration audit; mutating protocol checks use the temp fixture."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import pytest
from test_taskctl import control as control

from scripts.tasklib.cli import read_input
from scripts.tasklib.schema import ControlError
from scripts.tasklib.store import Store, conflict, digest, dump

ROOT = Path(__file__).resolve().parents[1]


def records():
    # An explicit canonical root supports read-only audits from source worktrees.
    # Store still rejects copied/wrong roots; do not silently retry with ROOT.
    store = Store(os.environ.get("TASK_CONTROL_ROOT", ROOT))
    with store.lock():
        return store, store.tasks()


def legacy_cards(text):
    matches = list(re.finditer(r"^### (P[012]-\d{3}[A-Z]?) — (.+)$", text, re.M))
    result = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        result[match[1]] = text[match.end() : end].split("\n## ")[0]
    return result


def test_all_legacy_ids_states_and_verbatim_archive_are_preserved():
    store, tasks = records()
    source = store.path(store.project["migration"]["source"]).read_bytes()
    assert hashlib.sha256(source).hexdigest() == store.project["migration"]["source_sha256"]
    text = source.decode()
    cards = legacy_cards(text)
    foundations = re.findall(r"^\| (P0-\d{3}) \| P0 \| done \|", text, re.M)
    assert len(cards) == 45 and len(foundations) == 13
    assert set(cards) | set(foundations) == set(store.project["migration"]["legacy_ids"])
    for task_id, body in cards.items():
        original = re.search(r"상태: (\w+)", body)[1]
        assert tasks[task_id].migration.original_status == original
    for task_id in foundations:
        assert tasks[task_id].migration.original_status == "done"
        assert tasks[task_id].migration.previous_evidence
    assert len(re.findall(r"^\| V-\d{3} \|", text, re.M)) == 12


def test_each_original_execution_ac_goal_and_contract_impact_preserved():
    store, tasks = records()
    cards = legacy_cards(store.path(store.project["migration"]["source"]).read_text())
    for task_id, body in cards.items():
        task = tasks[task_id]
        assert task_id in task.migration.source_ref
        # Preserve the audit without preventing a later reviewed specification revision.
        if task.spec_revision > task.migration.initial_spec_revision:
            assert task.spec_revision > 1
            continue
        goal = re.search(r"^- 목표/가치: (.+)$", body, re.M)[1]
        criteria = re.search(r"^- AC: (.+)$", body, re.M)[1]
        impact = re.search(r"^- 산출물/계약 영향: (.+)$", body, re.M)[1]
        assert task.objective == goal
        assert [a.expect for a in task.acceptance_criteria] == [
            value.strip() for value in re.split("[①②③④]", criteria) if value.strip()
        ]
        assert impact in task.scope.included
        assert task.source_tasks == [task_id]


def test_task_schema_paths_dag_and_contract_references_are_valid():
    store, tasks = records()
    store.validate(tasks)
    assert len(tasks) >= 60
    assert {"OPS-000", "OPS-001"} <= set(tasks)
    assert all(t.context.handoff == f"tasks/{t.id}/handoff.md" for t in tasks.values())


def test_schedule_effort_and_final_acceptance_are_separate():
    store, tasks = records()
    # Initial E2E-expanded plan: D1/D2=8h, D3=11h, plus technical 4.5h = 31.5h.
    # Previous reviewed total34h added P0-017/P0-019 +0.5h and P1-006D +1.5h.
    # Current P1-006 1->2h and P0-020 1.5->3h add 2.5h; gates are unchanged.
    for day, expected in {"D1": 8.5, "D2": 10, "D3": 12, "D4": 8}.items():
        assert sum(tasks[t].estimated_effort for t in store.project["milestones"][day]) == expected
    serial_days = ("D1", "D2", "D3", "TECH_D1", "TECH_D2", "TECH_D3")
    assert (
        sum(
            tasks[t].estimated_effort
            for day in serial_days
            for t in store.project["milestones"][day]
        )
        == 36.5
    )
    assert tasks["P0-026"].estimated_effort == 4  # Approved E2E expansion: 1h + 3h.
    assert sum(tasks[t].estimated_effort for t in store.project["milestones"]["LLMOps"]) == 3.5
    assert store.project["final_acceptance"]["local_product"] == ["P0-026"]
    assert set(store.project["final_acceptance"]["real_technology"]) == {
        "P1-002A",
        "P1-003A",
        "P1-007A",
        "P1-007B",
    }
    assert all(
        tasks[t].kind != "control" for t in store.project["final_acceptance"]["local_product"]
    )


def test_extended_contract_consumers_cannot_claim_and_selector_is_independent():
    store, tasks = records()
    if store.project["integration_target"]["state"] == "unregistered":
        assert not store.readiness(tasks["P0-014"], tasks)["executable"]
    for task in tasks.values():
        if store.project["contracts"]["RFA-EXTENDED"]["state"] == "planned" and any(
            r.id == "RFA-EXTENDED" and r.role == "consumer" for r in task.contract_refs
        ):
            assert task.spec_state == "draft" and task.unresolved
            assert not store.readiness(task, tasks)["executable"]
    assert [d.id for d in tasks["P0-018"].depends_on] == ["P0-014"]
    assert tasks["P0-018"].scope.owned_paths == [
        "src/rfa_mas/application/team_selector.py",
        "fixtures/teams/templates.json",
        "tests/test_team_selector.py",
    ]


def test_generated_views_are_consistent_and_no_monolithic_state_original():
    store, _ = records()
    # Heartbeats may run during this read-only audit; compare one locked snapshot.
    with store.lock():
        tasks = store.tasks()
        assert store.views_current(tasks)
        assert store.expected_views(tasks) == store.expected_views(tasks)
        assert not store.path("tasks.yaml").exists()
        assert "statuses" not in store.project
        assert "status" not in store.project
        assert "직접 수정하지" in store.path("TASKS.md").read_text()


def test_read_only_audit_uses_explicit_control_root_without_accepting_copy(
    control, tmp_path, monkeypatch
):
    import shutil

    monkeypatch.setenv("TASK_CONTROL_ROOT", str(control.root))
    store, tasks = records()
    assert store.root == control.root and "DEV-001" in tasks
    copied = tmp_path / "copied-control"
    shutil.copytree(control.root, copied)
    monkeypatch.setenv("TASK_CONTROL_ROOT", str(copied))
    with pytest.raises(ControlError, match="Copied/wrong control root"):
        records()


def test_initial_resume_handoffs_are_factual_and_other_tasks_use_template():
    store, tasks = records()
    assert store.path("docs/templates/HANDOFF.md").is_file()
    handoff = store.path(tasks["P0-014"].context.handoff).read_text()
    assert len(handoff) > 80
    for role in ["coordinator", "worker", "resume"]:
        prompt = store.path(f".agent/prompts/{role}.md").read_text()
        assert "TASK_CONTROL_ROOT" in prompt and "taskctl.py" in prompt


def test_technical_specs_keep_nat_independent_and_parallel_scopes_disjoint():
    _, tasks = records()

    def ancestors(task_id):
        direct = {d.id for d in tasks[task_id].depends_on}
        return direct | {a for d in direct for a in ancestors(d)}

    for task_id in ["P1-006", "P1-006E", "P1-001B", "P0-026"]:
        assert not ({"P0-027", "P0-028"} & ancestors(task_id))
    parallel = [tasks[t] for t in ["P0-028", "P1-006", "P1-001B"]]
    for index, task in enumerate(parallel):
        assert task.execution_role == "worker"
        for other in parallel[index + 1 :]:
            assert not conflict(task, other)
    assert "P1-001B" in ancestors("P1-005")
    assert "P1-006D" in ancestors("P0-020")
    assert "P1-006E" in ancestors("P0-026")
    assert "P0-016" in ancestors("P0-028")


def test_technical_scope_budget_gates_and_unrun_evidence_are_explicit():
    store, tasks = records()
    incremental = ["P0-027", "P0-028", "P1-006D", "P1-006E", "P1-001B"]
    # Initial 4.5h is historical; P1-006D's reviewed trace/retention scope adds 1.5h.
    assert sum(tasks[t].estimated_effort for t in incremental) == 6
    assert {
        t for day in ["TECH_D1", "TECH_D2", "TECH_D3"] for t in store.project["milestones"][day]
    } == set(incremental)
    assert tasks["P1-001C"].priority == "P1"
    assert tasks["P1-001C"].status == "deferred"
    assert store.project["final_acceptance"]["nat_installed_fake_provider"] == ["P0-028"]
    assert store.project["final_acceptance"]["local_product"] == ["P0-026"]
    for task_id in incremental:
        task = tasks[task_id]
        if task.attempts.count == 0:
            assert task.latest_evidence_file is None
            assert task.verification_summary.state == "not_run"
    assert store.path("fixtures/eval/evaluation_cases.jsonl").is_file()
    assert "persona_core_v1.jsonl" in " ".join(tasks["P1-006"].scope.planned_paths)
    assert "persona_regression_v2.jsonl" in " ".join(tasks["P1-006B"].scope.planned_paths)
    assert store.path("tasks/P0-027/handoff.md").is_file()


def test_administrative_audit_cannot_close_product_task(control):
    task = control.task()
    with pytest.raises(ControlError, match="cannot bypass"):
        control.run(
            "audit-control",
            task.id,
            "--session",
            "coordinator",
            "--expected-revision",
            str(task.revision),
            "--attempt",
            "admin-1",
            "--phase",
            "begin",
        )


def test_unblock_is_coordinator_only_and_does_not_bypass_contract_readiness(control):
    task = control.task()
    task.status = "deferred"
    task.spec_state = "draft"
    task.unresolved = ["Needs contract approval"]
    control.put(task)
    with pytest.raises(ControlError, match="Coordinator"):
        control.run(
            "unblock",
            task.id,
            "--session",
            "worker-a",
            "--expected-revision",
            "1",
            "--reason",
            "Not authorized",
        )
    control.run(
        "unblock",
        task.id,
        "--session",
        "coordinator",
        "--expected-revision",
        "1",
        "--reason",
        "Prioritized after explicit review",
    )
    with pytest.raises(ControlError, match="spec unresolved"):
        control.claim()


def test_publish_contract_requires_digest_proof_and_stales_consumers(control):
    store = Store(control.root)
    evidence = control.root / "contract-check.json"
    evidence.write_text(
        json.dumps({"schema_checked": True, "fixtures_checked": True, "contract_digest": "wrong"})
    )
    args = [
        "publish-contract",
        "--session",
        "coordinator",
        "--contract",
        "fixture-contract",
        "--version",
        "2",
        "--expected-project-digest",
        digest(store.project),
        "--evidence",
        "contract-check.json",
        "--reason",
        "Synthetic compatibility change",
        "--compatibility",
        "breaking",
        "--migration",
        "No persisted product data in fixture",
    ]
    with pytest.raises(ControlError, match="digest mismatch"):
        control.run(*args)
    contract = control.root / "docs/contract.json"
    contract.write_text('{"fixture_version": 2}\n')
    evidence.write_text(
        json.dumps(
            {
                "schema_checked": True,
                "fixtures_checked": True,
                "contract_digest": hashlib.sha256(contract.read_bytes()).hexdigest(),
            }
        )
    )
    result = control.run(*args)
    assert set(result["affected"]) == set(store.tasks())
    task = control.task()
    assert task.spec_state == "draft" and task.spec_revision == 2
    assert task.contract_refs[0].version == "1"  # No implicit acceptance of a new contract.


def test_edit_spec_increases_spec_revision_and_forbids_state_patch(control):
    patch = control.root.parent / "spec-patch.yaml"
    patch.write_text(dump({"status": "done"}))
    args = [
        "edit-spec",
        "DEV-001",
        "--session",
        "coordinator",
        "--expected-revision",
        "1",
        "--patch",
        str(patch),
        "--reason",
        "Synthetic scope explanation",
    ]
    with pytest.raises(ControlError, match="operational state"):
        control.run(*args)
    patch.write_text(dump({"objective": "Inspect the same fixture with an explicit objective"}))
    control.run(*args)
    assert control.task().spec_revision == 2 and control.task().revision == 2


def test_report_input_rejects_fifo_without_reading_it(tmp_path):
    import os

    path = tmp_path / "report-pipe"
    os.mkfifo(path)
    with pytest.raises(ControlError, match="Unsafe/large"):
        read_input(str(path))


def test_control_audit_uses_actual_report_and_no_product_integration_bypass(control):
    store = Store(control.root)
    store.project["administrative_tasks"] = ["DEV-001"]
    store.project["integration_target"]["state"] = "unregistered"
    store.project["integration_target"]["worktree"] = str(control.root)
    (control.root / "tasks/project.yaml").write_text(dump(store.project))
    task = control.task()
    task.kind = "control"
    task.execution_role = "coordinator"
    task.scope.owned_paths = ["docs/requirements.md"]
    task.scope.planned_paths = []
    task.deliverables[0].path = "docs/requirements.md"
    task.deliverables[0].availability = "existing"
    task.integration.state = "not_required"
    task.integration.reason = "Temporary control audit, not a product task"
    control.put(task)
    control.run(
        "audit-control",
        task.id,
        "--session",
        "coordinator",
        "--expected-revision",
        "1",
        "--attempt",
        "control-audit",
        "--phase",
        "begin",
    )
    assert control.task().status == "verifying"
    report = control.root.parent / "audit-report.json"
    report.write_text(json.dumps(control.report()))
    control.run(
        "audit-control",
        task.id,
        "--session",
        "coordinator",
        "--expected-revision",
        str(control.task().revision),
        "--attempt",
        "control-audit",
        "--phase",
        "record",
        "--report",
        str(report),
        "--handoff-file",
        str(control.handoff_file),
    )
    assert control.task().status == "done"
    assert Store(control.root).complete(control.task())


def test_global_context_change_fences_active_claim_until_coordinator_review(control):
    control.claim()
    context = control.root / "docs/PROJECT_CONTEXT.md"
    context.write_text("Changed synthetic global policy; re-read before execution.\n")
    with pytest.raises(ControlError, match="Recovery required"):
        control.owned("heartbeat")
    row = next(r for r in control.run("status")["tasks"] if r["id"] == "DEV-001")
    assert row["recovery_required"] is True
