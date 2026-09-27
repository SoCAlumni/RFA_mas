"""Explicit coordinator impact review; never represents a new test execution."""

from __future__ import annotations

import json
from pathlib import Path

from .evidence import check_report, immutable, matches, source_manifest, valid_evidence
from .schema import ControlError, VerificationSummary, relative_path
from .store import Store, digest, now, spec_digest


def review_doc_change(
    store: Store, task, attempt: str, paths: list[str], reason: str, reviewer: str
) -> None:
    relative_path(attempt)
    if "/" in attempt or not reason.strip():
        raise ControlError("Named attempt and explicit impact reason required")
    if not store.contract_ready(task) or task.spec_state != "ready":
        raise ControlError("Specification/contract changed; reverify")
    prior = task.integration.model_dump(mode="json")
    old = json.loads(store.path(task.integration.evidence).read_text())
    before = json.loads(store.path(old["source_manifest"]).read_text())
    target = Path(store.project["integration_target"]["worktree"])
    current = source_manifest(store, task, target)
    if check_report(task, old["report"]) != "passed":
        raise ControlError("Original checks did not pass")
    for field, value in (
        ("spec_revision", task.spec_revision),
        ("spec_digest", spec_digest(task)),
        ("contract_digest", store.contracts(task)),
    ):
        if old.get(field) != value or before.get(field) != value:
            raise ControlError("Specification/contract changed; reverify")
    # Reconstruct the historical binding with CURRENT context: a global instruction
    # change cannot be hidden in this review, including old manifests without a
    # separately stored context digest.
    historical = digest(
        dict(
            files={p: v["sha256"] for p, v in before["files"].items()},
            spec_revision=task.spec_revision,
            spec_digest=spec_digest(task),
            contracts=store.contracts(task),
            context_digest=store.context_digest(task),
        )
    )
    if historical != before["fingerprint"]:
        raise ControlError("Global context or historical binding changed; reverify")
    if any(v["staged"] or v["unstaged"] or v["untracked"] for v in current["files"].values()):
        raise ControlError("Commit relevant target changes before review")
    previous_hashes = {p: v["sha256"] for p, v in before["files"].items()}
    current_hashes = {p: v["sha256"] for p, v in current["files"].items()}
    delta = {
        p
        for p in previous_hashes.keys() | current_hashes.keys()
        if previous_hashes.get(p) != current_hashes.get(p)
    }
    if not delta or delta != set(paths) or len(paths) != len(set(paths)):
        raise ControlError("Exact nonempty changed-path set required")
    protected = set(task.context.global_refs)
    protected.update(r.split("#")[0] for r in task.requirement_refs)
    protected.update(c["path"] for c in store.project["contracts"].values())
    for path in delta:
        if (
            not path.startswith("docs/")
            or Path(path).suffix != ".md"
            or path in protected
            or any(matches(path, p) for p in store.project["fingerprint_inputs"])
            or not previous_hashes.get(path)
            or not current_hashes.get(path)
        ):
            raise ControlError("Only existing non-normative Markdown changes can be reviewed")
    if not store.path(task.context.handoff).is_file():
        raise ControlError("Existing integration handoff required")
    base = f"{task.evidence_dir}/{attempt}"
    record = dict(
        old,
        attempt_id=attempt,
        source_manifest=f"{base}/source.json",
        source_fingerprint=current["fingerprint"],
    )
    record["documentation_review"] = dict(
        reviewer=reviewer,
        reviewed_at=now(),
        reason=reason,
        tests_reexecuted=False,
        previous_evidence=task.integration.evidence,
        previous_integration=prior,
        changes={
            p: dict(before=previous_hashes[p], after=current_hashes[p]) for p in sorted(delta)
        },
    )
    # Preserve the original command/report and its actual execution timestamps.
    immutable(store, f"{base}/source.json", current)
    immutable(store, f"{base}/result.json", record)
    summary = VerificationSummary(
        state="passed",
        spec_revision=task.spec_revision,
        spec_digest=spec_digest(task),
        contract_digest=store.contracts(task),
        source_fingerprint=current["fingerprint"],
        evidence=f"{base}/result.json",
    )
    task.verification_summary = summary
    valid_evidence(store, task, target)
    # The coordinator reviewed the committed documentation artifact on the target.
    # Its preceding worker/integration binding remains immutable in previous_integration.
    submission = task.integration.submission
    submission["head"] = current["head"]
    for path in delta & submission["changed_files"].keys():
        submission["changed_files"][path] = current_hashes[path]
    task.integration.result = dict(
        verification_summary=summary.model_dump(mode="json"),
        head=current["head"],
        documentation_review=True,
    )
    task.integration.evidence = summary.evidence
    task.latest_evidence_file = summary.evidence
    task.integration.reservation = False
    task.status = "done"
    task.attempts.count += 1
    task.attempts.latest_attempt = attempt
    task.attempts.evidence_refs.append(summary.evidence)
    task.concise_result = (
        "Documentation impact reviewed; prior execution evidence retained (no tests rerun)"
    )
    task.next_action = "Reverify if code, settings, contract, AC or global instructions change"
