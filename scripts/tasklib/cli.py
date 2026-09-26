from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

from pydantic import ValidationError

from .evidence import (
    matches,
    record_attempt,
    scope_check,
    source_manifest,
    start_attempt,
    valid_evidence,
)
from .schema import SPEC_FIELDS, Claim, ControlError, Task, VerificationSummary, parse_yaml
from .store import Store, atomic, conflict, digest, dump, git, git_memo, now, spec_digest


def bump(task: Task) -> None:
    task.revision += 1
    task.updated_at = now()


def context_pack(store: Store, task: Task, tasks: dict[str, Task]) -> dict:
    data = task.model_dump(mode="json")
    return {
        "task_id": task.id,
        "spec_revision": task.spec_revision,
        "spec_digest": spec_digest(task),
        "contract_digest": store.contracts(task),
        "global_digests": {
            p: hashlib.sha256(store.path(p).read_bytes()).hexdigest()
            for p in task.context.global_refs
        },
        "control_root": str(store.root),
        "baseline": store.project["integration_target"],
        "read_first": [
            "AGENTS.md",
            *task.context.global_refs,
            f"tasks/{task.id}/task.yaml",
            task.context.handoff,
        ],
        "spec": {k: data[k] for k in sorted(SPEC_FIELDS)},
        "handoff": store.path(task.context.handoff).read_text()
        if store.path(task.context.handoff).exists()
        else "Not created; use docs/templates/HANDOFF.md",
        "direct_predecessors": [
            {
                "id": d.id,
                "result": tasks[d.id].concise_result,
                "evidence": tasks[d.id].latest_evidence_file,
                "handoff": tasks[d.id].context.handoff,
            }
            for d in task.depends_on
        ],
        "state_instruction": (
            "Re-read status/claim before writes; this pack is not a live lease "
            "and does not reset conversation context."
        ),
    }


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Same-host task control. No shell execution or auto-merge."
    )
    p.add_argument("--control-root", default=os.environ.get("TASK_CONTROL_ROOT"))
    sub = p.add_subparsers(dest="command", required=True)
    for command in ["validate", "refresh", "status", "list", "ready"]:
        q = sub.add_parser(command)
        q.add_argument("--workstream")
    for command in ["show", "context-pack"]:
        sub.add_parser(command).add_argument("task")
    for command in [
        "claim",
        "heartbeat",
        "release",
        "update",
        "block",
        "recover",
        "submit",
        "begin-evidence",
        "record-evidence",
        "integrate",
        "close",
        "edit-spec",
        "unblock",
    ]:
        q = sub.add_parser(command)
        q.add_argument("task")
        q.add_argument("--session", required=True)
        q.add_argument("--expected-revision", type=int, required=True)
        if command not in {"claim", "recover", "edit-spec", "unblock"}:
            q.add_argument("--generation", type=int, required=True)
        if command in {"claim", "recover"}:
            q.add_argument("--source", required=True)
        if command in {"update", "block", "release", "submit", "recover"}:
            q.add_argument("--handoff-file", required=True)
        if command in {"block", "release", "recover"}:
            q.add_argument("--reason", required=True)
            q.add_argument(
                "--resolve-when",
                default="Coordinator checks source/processes and recovers explicitly",
            )
        if command == "recover":
            q.add_argument("--new-session", required=True)
            q.add_argument("--inspection-file", required=True)
            q.add_argument("--disposition", choices=["resume", "discard"], required=True)
        if command == "update":
            q.add_argument("--next-action", required=True)
            q.add_argument("--result", required=True)
        if command in {"begin-evidence", "record-evidence"}:
            q.add_argument("--attempt", required=True)
            q.add_argument("--stage", choices=["worker", "integration"], default="worker")
        if command == "record-evidence":
            q.add_argument("--report", required=True)
        if command == "edit-spec":
            q.add_argument("--patch", required=True)
            q.add_argument("--reason", required=True)
        if command == "unblock":
            q.add_argument("--reason", required=True)
    q = sub.add_parser("publish-contract")
    q.add_argument("--session", required=True)
    q.add_argument("--contract", required=True)
    q.add_argument("--version", required=True)
    q.add_argument("--expected-project-digest", required=True)
    q.add_argument("--evidence", required=True)
    q.add_argument("--reason", required=True)
    q.add_argument("--compatibility", required=True)
    q.add_argument("--migration", required=True)
    q = sub.add_parser("adopt-baseline")
    q.add_argument("--session", required=True)
    q.add_argument("--source", required=True)
    q.add_argument("--expected-project-digest", required=True)
    q.add_argument("--inspection-file", required=True)
    q = sub.add_parser("project-digest")
    q = sub.add_parser("audit-control")
    q.add_argument("task")
    q.add_argument("--session", required=True)
    q.add_argument("--expected-revision", required=True, type=int)
    q.add_argument("--attempt", required=True)
    q.add_argument("--phase", required=True, choices=["begin", "record"])
    q.add_argument("--report")
    q.add_argument("--handoff-file")
    return p


def read_input(path: str) -> str:
    candidate = Path(path)
    if (
        any(p.is_symlink() for p in [candidate, *candidate.parents])
        or any(p.startswith(".env") for p in candidate.parts)
        or re.search(r"(?i)(credentials|secrets?|tokens?)(\.|/|$)", str(candidate))
        or not stat.S_ISREG(candidate.stat().st_mode)
        or candidate.stat().st_size > 100_000
    ):
        raise ControlError("Unsafe/large input; use a small sanitized handoff/report")
    return candidate.read_text()


def handoff(store: Store, task: Task, path: str) -> None:
    text = read_input(path)
    if len(text.strip()) < 80:
        raise ControlError("Handoff must contain facts, AC, evidence and an exact next action")
    atomic(store.path(task.context.handoff), text)


def integration_guard(store: Store, task: Task, args) -> None:
    store.coordinator(args.session)
    if task.revision != args.expected_revision:
        raise ControlError("Stale revision")
    if not task.integration.submission or task.integration.submission_generation != args.generation:
        raise ControlError("Stale submission generation")
    if task.status != "verifying" or not task.integration.reservation:
        raise ControlError("No reserved submission")


def _captured_head_matches(
    source: dict, captured: str | None, recorded: str, manifest: dict | None = None
) -> bool:
    """Exact binding, or a legacy record whose head advanced during the same attempt.

    Before OPS-003, integration results stored the target head at record time. When an
    unrelated commit landed between begin/record, that head descends from the captured
    manifest head. Tolerate only that relation, and only when every clean captured file
    hash equals the blob at the captured head, so a rewritten manifest head is rejected.
    Fingerprint/spec/contract equality is still checked by the caller.
    """
    if not captured:
        return False
    if captured == recorded:
        return True
    worktree = Path(source["worktree"])
    try:
        git(worktree, "merge-base", "--is-ancestor", captured, recorded)
    except ControlError:
        return False
    files = (manifest or {}).get("files") or {}
    if not files:
        return False
    for name, entry in files.items():
        if entry.get("staged") or entry.get("unstaged") or entry.get("untracked"):
            return False  # A dirty capture has no commit to compare against.
        blob = subprocess.run(
            ["git", "-C", str(worktree), "show", f"{captured}:{name}"],
            capture_output=True,
            timeout=15,
            env={"PATH": os.environ.get("PATH", ""), "LANG": "C", "GIT_CONFIG_NOSYSTEM": "1"},
        )
        actual = hashlib.sha256(blob.stdout).hexdigest() if blob.returncode == 0 else None
        if actual != entry.get("sha256"):
            return False
    return True


def revalidation_baseline(store: Store, task: Task, source: dict) -> str:
    """Confirm historical integration, not current AC, before resetting its baseline."""
    integration = task.integration
    result = integration.result or {}
    submission = integration.submission or {}
    summary = result.get("verification_summary", {})
    if (
        task.claim
        or task.status != "verifying"
        or integration.state != "integrated"
        or not integration.reservation
        or not submission.get("head")
        or not result.get("head")
        or not integration.evidence
        or summary.get("state") != "passed"
        or summary.get("evidence") != integration.evidence
    ):
        raise ControlError("Revalidation requires a previously integrated submission and evidence")
    try:
        record = json.loads(store.path(integration.evidence).read_text())
        before = json.loads(store.path(record["source_manifest"]).read_text())
    except (OSError, KeyError, ValueError):
        raise ControlError("Historical integration evidence unavailable") from None
    if (
        record.get("task_id") != task.id
        or record.get("result") != "passed"
        or before.get("repository_id") != store.project["repository_id"]
        or not _captured_head_matches(source, before.get("head"), result["head"], before)
        or record.get("source_fingerprint") != summary.get("source_fingerprint")
        or before.get("fingerprint") != summary.get("source_fingerprint")
        or any(
            before.get("files", {}).get(name, {}).get("sha256") != expected
            for name, expected in submission.get("changed_files", {}).items()
        )
        or any(
            record.get(field) != summary.get(field) or before.get(field) != summary.get(field)
            for field in ["spec_revision", "spec_digest", "contract_digest"]
        )
    ):
        raise ControlError("Historical integration evidence binding mismatch")
    path = Path(source["worktree"])
    # Integration may have been cherry-picked; require its recorded artifact binding,
    # not ancestry of the worker commit. The integrated target must remain an ancestor.
    git(path, "cat-file", "-e", submission["head"] + "^{commit}")
    git(path, "merge-base", "--is-ancestor", result["head"], source["target_head"])
    return source["target_head"]


def inactive_integrated_guard(store: Store, task: Task) -> None:
    """A stale reservation protects prior artifacts; it is not an active worker."""
    if task.claim or task.verification_summary.state != "stale":
        raise ControlError("Operation requires an inactive stale integrated reservation")
    target = store.project["integration_target"]
    source = store.source(target["worktree"])
    if source["branch"] != target["branch"]:
        raise ControlError("Wrong integration branch")
    # Reuse the same historical evidence/manifest/commit binding as recovery. This
    # does NOT certify current AC or release the reservation.
    revalidation_baseline(store, task, source)


def contract_notice(contract_id: str, version: str) -> str:
    return f"Re-read {contract_id} {version}; coordinator edit-spec must accept new digest"


def reserved_contract_acceptance(store: Store, task: Task, patch: dict) -> None:
    """Permit only explicit published-baseline acceptance, never a scope rewrite."""
    inactive_integrated_guard(store, task)
    if "contract_refs" not in patch or set(patch) - {"contract_refs", "spec_state", "unresolved"}:
        raise ControlError("Reserved scope allows only published contract acceptance")
    try:
        candidate = Task.model_validate(task.model_dump(mode="json") | patch)
    except ValidationError:
        raise ControlError(
            "Invalid contract acceptance or unresolved ready specification"
        ) from None
    if [(ref.id, ref.role) for ref in candidate.contract_refs] != [
        (ref.id, ref.role) for ref in task.contract_refs
    ]:
        raise ControlError("Contract acceptance must preserve contract IDs and roles")
    notices = set()
    for before, after in zip(task.contract_refs, candidate.contract_refs, strict=True):
        if before == after:
            continue
        entry = store.project["contracts"][after.id]
        path = store.path(entry["path"])
        if (
            before.role != "consumer"
            or entry.get("state") != "ready"
            or after.version != entry["version"]
            or after.digest != entry["digest"]
            or not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != entry["digest"]
        ):
            raise ControlError("Accept only the current published consumer version/digest")
        notices.add(contract_notice(after.id, after.version))
        # A consumer can skip intermediate published versions. Only notices with
        # exact publisher provenance may be cleared; never match arbitrary text
        # by prefix or erase an independent unresolved decision.
        for change in store.project.get("contract_changes", []):
            version = change.get("version")
            if (
                change.get("id") == after.id
                and task.id in change.get("affected", [])
                and isinstance(version, str)
                and change.get("notice") == contract_notice(after.id, version)
            ):
                notices.add(change["notice"])
    if not notices:
        raise ControlError("Contract acceptance requires a changed published baseline")
    if set(candidate.unresolved) & notices:
        raise ControlError("Remove accepted publisher notices in the same specification change")
    removed = set(task.unresolved) - set(candidate.unresolved)
    if not removed <= notices or candidate.unresolved != [
        item for item in task.unresolved if item not in removed
    ]:
        raise ControlError("Contract acceptance cannot clear unrelated unresolved decisions")
    if candidate.spec_state == "ready" and not store.contract_ready(candidate):
        raise ControlError("All required contracts must be published before spec readiness")


def _fast_forwarded_to_target(store: Store, task: Task, source: Path) -> bool:
    """True only if source HEAD is exactly the current target HEAD and descends from the
    claim baseline, i.e. the revalidation worktree carries no worker commit of its own."""
    head = git(source, "rev-parse", "HEAD")
    target_head = git(Path(store.project["integration_target"]["worktree"]), "rev-parse", "HEAD")
    if head != target_head or head == task.claim.baseline_head:
        return False
    try:
        git(source, "merge-base", "--is-ancestor", task.claim.baseline_head, head)
    except ControlError:
        return False
    return True


def submission_paths(store: Store, task: Task, source: Path) -> list[str]:
    """Only a fenced, explicitly recovered integration may submit unchanged artifacts."""
    approach = task.attempts.approaches[-1] if task.attempts.approaches else {}
    revalidation = (
        approach.get("kind") == "integrated_revalidation"
        and approach.get("generation") == task.claim.generation
        and approach.get("baseline_head") == task.claim.baseline_head
    )
    if revalidation and git(source, "rev-parse", "HEAD") == task.claim.baseline_head:
        # A no-change revalidation still submits an exact, clean current Git commit.
        # Never swallow scope_check errors for dirty or out-of-scope worker changes.
        store.source(str(source), feature=True, current=True)
        paths = []
    elif revalidation and _fast_forwarded_to_target(store, task, source):
        # OPS-005: another integration moved the target during this revalidation. A clean
        # fast-forward to exactly the current integrated HEAD adds no worker change; the
        # evidence must still be captured at this head (fingerprint check at submit).
        store.source(str(source), feature=True, current=True)
        paths = []
    else:
        paths = scope_check(store, task, source)
    if revalidation:
        manifest = source_manifest(store, task, source)
        paths = sorted(
            set(paths)
            | {
                name
                for name in manifest["files"]
                if any(matches(name, pattern) for pattern in task.scope.owned_paths)
            }
        )
        if not paths:
            raise ControlError("Revalidation requires committed owned source artifacts")
    return paths


def execute(store: Store, args) -> dict:
    with store.lock():
        tasks = store.tasks()
        store.reconcile(tasks)
        store.validate(tasks)
        command = args.command
        if command == "project-digest":
            return {"project_digest": digest(store.project)}
        if command == "publish-contract":
            store.coordinator(args.session)
            if digest(store.project) != args.expected_project_digest:
                raise ControlError("Stale project revision")
            if args.contract not in store.project["contracts"]:
                raise ControlError("Unknown registered contract")
            affected = [
                t
                for t in tasks.values()
                if any(r.id == args.contract and r.role == "consumer" for r in t.contract_refs)
            ]
            for task in affected:
                if task.claim:
                    raise ControlError("Recover/release affected claims before contract change")
                if task.integration.reservation:
                    inactive_integrated_guard(store, task)
            entry = store.project["contracts"][args.contract]
            path = store.path(entry["path"])
            proof = json.loads(store.path(args.evidence).read_text())
            if (
                not path.is_file()
                or not proof.get("schema_checked")
                or not proof.get("fixtures_checked")
            ):
                raise ControlError("Schema and fixture check evidence required")
            file_digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if proof.get("contract_digest") != file_digest:
                raise ControlError("Contract evidence digest mismatch")
            entry.update(version=args.version, digest=file_digest, state="ready")
            store.project.setdefault("contract_changes", []).append(
                {
                    "id": args.contract,
                    "at": now(),
                    "reason": args.reason,
                    "compatibility": args.compatibility,
                    "migration": args.migration,
                    "evidence": args.evidence,
                    "affected": [t.id for t in affected],
                    "version": args.version,
                    "digest": file_digest,
                    "notice": contract_notice(args.contract, args.version),
                }
            )
            store.project["updated_at"] = now()
            atomic(store.path("tasks/project.yaml"), dump(store.project))
            for task in affected:
                if all(
                    r.version == args.version and r.digest == file_digest
                    for r in task.contract_refs
                    if r.id == args.contract
                ):
                    continue
                task.spec_state = "draft"
                task.unresolved = list(
                    dict.fromkeys(
                        [
                            *task.unresolved,
                            contract_notice(args.contract, args.version),
                        ]
                    )
                )
                task.spec_revision += 1
                if task.latest_evidence_file:
                    task.verification_summary.state = "stale"
                if task.status == "done":
                    task.status = "verifying"
                    if task.kind != "control":
                        task.integration.reservation = True
                bump(task)
                atomic(store.path(f"tasks/{task.id}/task.yaml"), dump(task.model_dump(mode="json")))
            store.refresh(tasks)
            return {
                "contract": args.contract,
                "digest": file_digest,
                "affected": [t.id for t in affected],
            }
        if command == "adopt-baseline":
            store.coordinator(args.session)
            if digest(store.project) != args.expected_project_digest:
                raise ControlError("Stale project revision")
            if any(t.claim or t.integration.reservation for t in tasks.values()):
                raise ControlError("Resolve active claims/reservations before baseline change")
            inspection = json.loads(read_input(args.inspection_file))
            for key in [
                "user_changes_preserved",
                "source_reviewed",
                "no_secrets_tracked",
                "control_files_excluded_from_worker_commits",
            ]:
                if inspection.get(key) is not True:
                    raise ControlError("Explicit baseline inspection required")
            source = Path(args.source).resolve(strict=True)
            identity_file = source / ".agent/control.json"
            if (
                not identity_file.is_file()
                or json.loads(identity_file.read_text()).get("repository_id")
                != store.project["repository_id"]
            ):
                raise ControlError("Source checkout lacks this repository identity marker")
            if git(source, "status", "--porcelain", "--untracked-files=all"):
                raise ControlError(
                    "Baseline must be clean; never commit/stash user work automatically"
                )
            names = git(source, "ls-files", "-z").split("\0")
            if any(
                any(p.startswith(".env") and p != ".env.example" for p in Path(n).parts)
                or Path(n).suffix in {".pem", ".key", ".secret", ".secrets"}
                for n in names
            ):
                raise ControlError("Tracked secret path denied; preserve it outside Git explicitly")
            store.project["integration_target"] = {
                "state": "registered",
                "worktree": str(source),
                "branch": git(source, "symbolic-ref", "--short", "HEAD"),
                "baseline_head": git(source, "rev-parse", "HEAD"),
                "git_common_dir": git(
                    source, "rev-parse", "--path-format=absolute", "--git-common-dir"
                ),
                "inspection": inspection,
            }
            store.project["updated_at"] = now()
            atomic(store.path("tasks/project.yaml"), dump(store.project))
            store.refresh(tasks)
            return {"baseline": "registered"}
        if command == "validate":
            if not store.views_current(tasks):
                raise ControlError("Derived views stale/tampered; run refresh")
            return {"valid": True, "tasks": len(tasks), "views": "current"}
        if command in {"refresh", "status", "list", "ready", "show", "context-pack"}:
            stale = not store.views_current(tasks)
            if stale or command == "refresh":
                store.refresh(tasks)
            if command == "refresh":
                return {"views": "current", "repaired": stale}
            if command in {"show", "context-pack"}:
                if args.task not in tasks:
                    raise ControlError("Unknown task")
                task = tasks[args.task]
                return (
                    task.model_dump(mode="json")
                    if command == "show"
                    else context_pack(store, task, tasks)
                )
            rows = []
            for task in tasks.values():
                ready = store.readiness(task, tasks)
                if args.workstream and args.workstream != task.workstream:
                    continue
                if command == "ready" and not ready["planning_ready"]:
                    continue
                rows.append(
                    {
                        "id": task.id,
                        "title": task.title,
                        "revision": task.revision,
                        "spec_revision": task.spec_revision,
                        "status": task.status,
                        "verification": task.verification_summary.state,
                        "integration": task.integration.state,
                        "claim": task.claim.model_dump() if task.claim else None,
                        "recovery_required": store.needs_recovery(task),
                        **ready,
                    }
                )
            return {"repaired_views": stale, "tasks": rows}
        if args.task not in tasks:
            raise ControlError("Unknown task")
        task = tasks[args.task]
        if task.revision != args.expected_revision:
            raise ControlError("Stale revision")
        if command == "audit-control":
            store.coordinator(args.session)
            if (
                task.id not in store.project.get("administrative_tasks", [])
                or task.kind != "control"
                or task.integration.state != "not_required"
                or task.claim
            ):
                raise ControlError("Administrative audit cannot bypass product task integration")
            if (
                task.id == "OPS-000"
                and store.project["integration_target"]["state"] != "registered"
            ):
                raise ControlError("Register and inspect the Git baseline first")
            if args.phase == "begin":
                start_attempt(store, task, store.root, args.attempt, unversioned=True)
                task.attempts.count += 1
                task.attempts.latest_attempt = args.attempt
                task.status = "verifying"
            else:
                if not args.report or not args.handoff_file:
                    raise ControlError("Actual audit report and handoff required")
                summary = record_attempt(
                    store,
                    task,
                    store.root,
                    args.attempt,
                    json.loads(read_input(args.report)),
                    unversioned=True,
                )
                handoff(store, task, args.handoff_file)
                task.verification_summary = VerificationSummary.model_validate(summary)
                task.latest_evidence_file = summary["evidence"]
                task.attempts.evidence_refs.append(summary["evidence"])
                task.attempts.cycles += 1
                task.integration.evidence = summary["evidence"]
                task.integration.result = {
                    "kind": "administrative_audit",
                    "source": str(store.root),
                }
                task.blocker = None
                task.status = "done" if summary["state"] == "passed" else "verifying"
                task.concise_result = (
                    "Development-control audit " + summary["state"] + "; not product acceptance"
                )
                if summary["state"] == "passed":
                    task.next_action = (
                        "이관 완료. OPS-000의 Git baseline을 먼저 확인한다. "
                        "그다음 planning-ready task를 선택한다. "
                        "제품 전체 구현 권한은 추가하지 않는다."
                    )
        elif command == "claim":
            readiness = store.readiness(task, tasks)
            if not readiness["executable"]:
                raise ControlError("Not claimable: " + ", ".join(readiness["reasons"]))
            if task.execution_role == "coordinator":
                store.coordinator(args.session)
            active = [t for t in tasks.values() if t.claim]
            if len(active) >= store.project["max_workers"]:
                raise ControlError("Worker limit reached")
            source = store.source(
                args.source, feature=task.execution_role == "worker", current=True
            )
            if any(t.claim.worktree == source["worktree"] for t in active):
                raise ControlError("One active executor per source worktree")
            task.claim_generation += 1
            task.claim = Claim(
                session_id=args.session,
                worktree=source["worktree"],
                branch=source["branch"],
                generation=task.claim_generation,
                claimed_at=now(),
                heartbeat_at=now(),
                lease_expires_at=store.lease(),
                baseline_head=source["target_head"],
                context_digest=store.context_digest(task),
            )
            task.status = "in_progress"
        elif command == "recover":
            store.coordinator(args.session)
            if not task.claim and not task.integration.reservation:
                raise ControlError("Nothing to recover")
            inspection = json.loads(read_input(args.inspection_file))
            for field in [
                "old_process_stopped_or_fenced",
                "worktree_reviewed",
                "unintegrated_changes_reviewed",
                "side_effects_reconciled",
            ]:
                if inspection.get(field) is not True:
                    raise ControlError(
                        "Recovery inspection incomplete; expiry alone is insufficient"
                    )
            revalidation = inspection.get("integrated_revalidation") is True
            if revalidation and args.disposition != "resume":
                raise ControlError("Integrated revalidation requires explicit resume")
            source = store.source(
                args.source,
                feature=revalidation or task.execution_role == "worker",
                current=revalidation,
            )
            if task.execution_role == "coordinator":
                store.coordinator(args.new_session)
            active = [t for t in tasks.values() if t.id != task.id and t.claim]
            if args.disposition == "resume" and (
                len(active) >= store.project["max_workers"]
                or any(t.claim.worktree == source["worktree"] for t in active)
            ):
                raise ControlError(
                    "Recovery would exceed concurrency or reuse an occupied worktree"
                )
            if args.disposition == "resume" and (
                task.spec_state != "ready" or not store.contract_ready(task)
            ):
                raise ControlError("Reconcile specification/contract before recovery resume")
            if args.disposition == "resume" and any(
                not store.complete(tasks[d.id]) for d in task.depends_on
            ):
                raise ControlError("Recovery dependency is not integrated and verified")
            previous_claim = next(
                (a["claim"] for a in reversed(task.attempts.approaches) if a.get("claim")),
                {},
            )
            original_base = (
                task.claim.baseline_head
                if task.claim
                else task.integration.submission.get("baseline_head")
                if task.integration.submission
                else previous_claim.get("baseline_head")
            )
            baseline = revalidation_baseline(store, task, source) if revalidation else original_base
            if args.disposition == "resume" and not baseline:
                raise ControlError(
                    "Original claim baseline unavailable; coordinator must inspect history"
                )
            if args.disposition == "resume":
                for other in tasks.values():
                    if (
                        other.id == task.id
                        or not (other.claim or other.integration.reservation)
                        or not conflict(task, other)
                    ):
                        continue
                    if revalidation and other.claim is None:
                        try:
                            inactive_integrated_guard(store, other)
                        except ControlError:
                            pass
                        else:
                            # Its reservation/history remains intact. The new active
                            # claim fences any later overlapping recovery until close.
                            continue
                    raise ControlError("Recovery conflicts with another reservation")
            handoff(store, task, args.handoff_file)
            task.claim_generation += 1
            task.attempts.approaches.append(
                {
                    "kind": "integrated_revalidation" if revalidation else "recovery",
                    "generation": task.claim_generation,
                    "baseline_head": baseline,
                    "reason": args.reason,
                    "at": now(),
                    "cycles": task.attempts.cycles,
                    "no_progress_count": task.attempts.no_progress_count,
                    "claim": task.claim.model_dump(mode="json") if task.claim else None,
                    "integration": task.integration.model_dump(mode="json"),
                    "verification_summary": task.verification_summary.model_dump(mode="json"),
                    "latest_evidence_file": task.latest_evidence_file,
                }
            )
            task.attempts.cycles = 0
            task.attempts.no_progress_count = 0
            task.integration.submission = None
            task.integration.submission_generation = None
            task.integration.result = None
            task.integration.evidence = None
            task.integration.reservation = False
            task.integration.state = "pending"
            task.verification_summary.state = "stale" if task.latest_evidence_file else "not_run"
            task.claim = None
            task.blocker = None
            task.status = "todo"
            if args.disposition == "resume":
                task.claim = Claim(
                    session_id=args.new_session,
                    worktree=source["worktree"],
                    branch=source["branch"],
                    generation=task.claim_generation,
                    claimed_at=now(),
                    heartbeat_at=now(),
                    lease_expires_at=store.lease(),
                    baseline_head=baseline,
                    context_digest=store.context_digest(task),
                )
                task.status = "in_progress"
            task.concise_result = "Recovery inspected: " + args.reason
        elif command == "unblock":
            store.coordinator(args.session)
            if (
                task.claim
                or task.integration.reservation
                or task.status not in {"blocked", "deferred"}
            ):
                raise ControlError("Only unreserved blocked/deferred tasks can be unblocked")
            task.status = "todo"
            task.blocker = None
            task.concise_result = "Unblocked by coordinator: " + args.reason
        elif command == "edit-spec":
            store.coordinator(args.session)
            patch = parse_yaml(read_input(args.patch))
            if set(patch) - SPEC_FIELDS:
                raise ControlError("Spec patch cannot set operational state")
            if task.claim:
                raise ControlError("Recover/release active scope before specification change")
            if task.integration.reservation:
                reserved_contract_acceptance(store, task, patch)
            data = task.model_dump(mode="json")
            data.update(patch)
            task = Task.model_validate(data)
            task.spec_revision += 1
            task.verification_summary.state = "stale" if task.latest_evidence_file else "not_run"
            if task.status == "done":
                task.status = "verifying"
            task.concise_result = "Specification changed: " + args.reason
            tasks[task.id] = task
            store.validate(tasks)
        elif command in {"integrate", "close"} or (
            command in {"begin-evidence", "record-evidence"} and args.stage == "integration"
        ):
            integration_guard(store, task, args)
            target = Path(store.project["integration_target"]["worktree"])
            source = store.source(str(target))
            if source["branch"] != store.project["integration_target"]["branch"]:
                raise ControlError("Wrong integration branch")
            if command == "begin-evidence":
                start_attempt(store, task, target, args.attempt)
                task.attempts.count += 1
                task.attempts.latest_attempt = args.attempt
            elif command == "record-evidence":
                summary = record_attempt(
                    store, task, target, args.attempt, json.loads(read_input(args.report))
                )
                # Bind the head the checks actually ran on (captured at begin-evidence).
                # Another session may commit unrelated files meanwhile; record_attempt has
                # already rejected any change to this task's fingerprinted sources.
                captured = json.loads(
                    store.path(f"{task.evidence_dir}/{args.attempt}/source.json").read_text()
                )
                task.integration.result = {
                    "verification_summary": summary,
                    "head": captured["head"],
                }
                task.integration.evidence = summary["evidence"]
            else:
                result = task.integration.result
                if not result or not task.integration.evidence:
                    raise ControlError("Target integration evidence required")
                saved = task.verification_summary
                task.verification_summary = VerificationSummary.model_validate(
                    result["verification_summary"]
                )
                valid_evidence(store, task, target)
                manifest = source_manifest(store, task, target)
                if any(
                    v["staged"] or v["unstaged"] or v["untracked"]
                    for v in manifest["files"].values()
                ):
                    raise ControlError(
                        "Relevant target sources must be committed before integration"
                    )
                for name, expected in task.integration.submission["changed_files"].items():
                    actual = manifest["files"].get(name, {}).get("sha256")
                    if actual != expected:
                        raise ControlError(
                            "Target differs from submission; "
                            "reassign/reverify reviewed conflict resolution"
                        )
                if command == "integrate":
                    task.integration.state = "integrated"
                    task.verification_summary = saved
                else:
                    if (
                        task.integration.state != "integrated"
                        or not store.path(task.context.handoff).is_file()
                    ):
                        raise ControlError("Integrate and provide handoff before close")
                    task.status = "done"
                    task.latest_evidence_file = task.integration.evidence
                    task.integration.reservation = False
                    task.concise_result = (
                        "Integrated target and required AC verified; see immutable evidence"
                    )
        else:
            store.guard(task, args.expected_revision, args.session, args.generation)
            if command == "heartbeat":
                task.claim.heartbeat_at = now()
                task.claim.lease_expires_at = store.lease()
            elif command == "update":
                handoff(store, task, args.handoff_file)
                task.next_action, task.concise_result = args.next_action, args.result
            elif command in {"release", "block"}:
                handoff(store, task, args.handoff_file)
                task.attempts.approaches.append(
                    {
                        "kind": command,
                        "at": now(),
                        "reason": args.reason,
                        "claim": task.claim.model_dump(mode="json"),
                    }
                )
                data = task.model_dump(mode="json")
                data["blocker"] = {
                    "cause": args.reason,
                    "evidence": [task.context.handoff],
                    "resolve_when": args.resolve_when,
                    "affects": [
                        t.id for t in tasks.values() if any(d.id == task.id for d in t.depends_on)
                    ],
                }
                task = Task.model_validate(data)
                task.status = "blocked"
                task.integration.reservation = True
                task.claim = None
            elif command == "begin-evidence":
                if (
                    task.attempts.cycles >= store.project["cycle_limit"]
                    or task.attempts.no_progress_count >= store.project["no_progress_limit"]
                ):
                    raise ControlError(
                        "Retry budget exhausted; block/handoff and change approach with coordinator"
                    )
                start_attempt(store, task, Path(task.claim.worktree), args.attempt)
                task.attempts.count += 1
                task.attempts.latest_attempt = args.attempt
            elif command == "record-evidence":
                summary = record_attempt(
                    store,
                    task,
                    Path(task.claim.worktree),
                    args.attempt,
                    json.loads(read_input(args.report)),
                )
                previous_fingerprint = task.verification_summary.source_fingerprint
                task.verification_summary = VerificationSummary.model_validate(summary)
                task.latest_evidence_file = summary["evidence"]
                task.attempts.evidence_refs.append(summary["evidence"])
                task.attempts.cycles += 1
                task.attempts.no_progress_count = (
                    task.attempts.no_progress_count + 1
                    if previous_fingerprint == summary["source_fingerprint"]
                    and summary["state"] == "failed"
                    else 0
                )
            elif command == "submit":
                source = Path(task.claim.worktree)
                valid_evidence(store, task, source)
                paths = submission_paths(store, task, source)
                handoff(store, task, args.handoff_file)
                manifest = source_manifest(store, task, source)
                task.status = "verifying"
                task.integration.state = "pending"
                task.integration.reservation = True
                task.integration.submission_generation = args.generation
                task.integration.submission = {
                    "head": manifest["head"],
                    "baseline_head": task.claim.baseline_head,
                    "worktree": str(source),
                    "branch": manifest["branch"],
                    "evidence": task.latest_evidence_file,
                    "changed_files": {p: manifest["files"].get(p, {}).get("sha256") for p in paths},
                }
                task.claim = None
            else:
                raise ControlError("Unsupported command")
        bump(task)
        return {
            "task": task.id,
            "status": task.status,
            "generation": task.claim_generation,
            **store.save(task),
        }


def main() -> None:
    args = parser().parse_args()
    try:
        if not args.control_root:
            raise ControlError("--control-root or TASK_CONTROL_ROOT is required; no local fallback")
        # OPS-004: one read-only Git snapshot per CLI invocation (cleared on exit).
        with git_memo():
            result = execute(Store(args.control_root), args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ControlError, OSError, ValueError, KeyError) as exc:
        # Do not echo Pydantic input values, subprocess output or credentials.
        message = str(exc) if isinstance(exc, ControlError) else type(exc).__name__
        print(json.dumps({"error": message}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2) from None
