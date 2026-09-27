"""Temporary management fixtures, not product evidence."""

# ruff: noqa: F811 -- imported pytest fixture is requested by parameter name
import json

import pytest
from test_taskctl import completed_consumer, control, git_call  # noqa: F401

from scripts.tasklib.schema import ControlError


def complete_with_document(control):
    task = control.task()
    task.scope.owned_paths.append("docs/reference.md")
    task.scope.planned_paths.append("docs/reference.md")
    control.put(task)
    (control.first / "docs").mkdir(exist_ok=True)
    (control.first / "docs/reference.md").write_text("Existing implementation reference.\n")
    git_call(control.first, "add", "docs/reference.md")
    git_call(control.first, "commit", "-m", "Fixture documentation")
    # Claim must start at the integration baseline, so include the document there first.
    git_call(control.target, "merge", "--ff-only", "worker-a")
    completed_consumer(control)
    old = control.task().integration.evidence
    original = (control.root / old).read_bytes()
    (control.target / "docs/reference.md").write_text("Added unrelated component reference.\n")
    git_call(control.target, "add", "docs/reference.md")
    git_call(control.target, "commit", "-m", "Fixture docs update")
    control.run("status")
    return old, original


def review(control, **overrides):
    args = dict(
        session="coordinator",
        expected_revision=control.task().revision,
        attempt="doc-review-1",
        changed_path="docs/reference.md",
        reason="Reviewed diff: unrelated component reference; behavior/AC unchanged",
    )
    args.update(overrides)
    argv = ["review-doc-change", "DEV-001"]
    for key, value in args.items():
        argv += ["--" + key.replace("_", "-"), str(value)]
    return control.run(*argv)


def test_review_keeps_original_execution_and_unlocks_dependency(control):
    old, original = complete_with_document(control)
    assert control.task().status == "verifying"
    review(control)
    assert control.task().status == "done"
    assert (control.root / old).read_bytes() == original
    evidence = json.loads((control.root / control.task().integration.evidence).read_text())
    assert evidence["documentation_review"]["tests_reexecuted"] is False
    assert evidence["report"] == json.loads(original)["report"]
    rows = {r["id"]: r for r in control.run("status")["tasks"]}
    assert rows["DEV-001"]["status"] == "done"
    assert "dependency:DEV-001" not in rows["DEV-003"]["reasons"]
    # A second documentation review must retain a valid historical integration binding.
    (control.target / "docs/reference.md").write_text("Second reviewed reference update.\n")
    git_call(control.target, "add", "docs/reference.md")
    git_call(control.target, "commit", "-m", "Second docs update")
    control.run("status")
    review(control, attempt="doc-review-2")


@pytest.mark.parametrize("change", ["code", "global", "contract", "ac", "dirty", "failed"])
def test_related_change_or_invalid_evidence_cannot_be_carried(control, change):
    complete_with_document(control)
    if change == "code":
        control.commit_change(source=control.target, value="VALUE = 99\n")
    elif change == "global":
        (control.root / "docs/PROJECT_CONTEXT.md").write_text("Changed instructions\n")
    elif change == "contract":
        (control.root / "docs/contract.json").write_text('{"fixture_version": 2}\n')
    elif change == "ac":
        task = control.task()
        task.spec_revision += 1
        control.put(task)
    elif change == "dirty":
        (control.target / "docs/reference.md").write_text("Uncommitted edit\n")
    else:
        task = control.task()
        task.integration.result["verification_summary"]["state"] = "failed"
        control.put(task)
    with pytest.raises(ControlError):
        review(control)
    assert control.task().status == "verifying"


@pytest.mark.parametrize(
    "bad",
    [
        dict(session="worker-a"),
        dict(expected_revision=0),
        dict(changed_path="docs/wrong.md"),
        dict(attempt="../escape"),
    ],
)
def test_explicit_coordinator_revision_delta_and_safe_path_required(control, bad):
    complete_with_document(control)
    with pytest.raises(ControlError):
        review(control, **bad)


def test_requirement_document_cannot_use_reference_review(control):
    complete_with_document(control)
    task = control.task()
    task.requirement_refs.append("docs/reference.md")
    control.put(task)
    with pytest.raises(ControlError):
        review(control)


def test_active_worker_cannot_be_completed_by_document_review(control):
    control.claim()
    with pytest.raises(ControlError):
        review(control)
    assert control.task().status == "in_progress"
