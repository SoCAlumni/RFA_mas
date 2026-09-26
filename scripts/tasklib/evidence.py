from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

from .schema import ControlError, Task, relative_path
from .store import Store, digest, git, now, spec_digest

EXCLUDED_DIRS = {
    ".git",
    ".venv",
    ".local",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
}
EXCLUDED_SUFFIXES = {".db", ".sqlite", ".log", ".pyc", ".pem", ".key", ".p12"}


def permitted(path: str) -> bool:
    parts = Path(path).parts
    if set(parts) & EXCLUDED_DIRS or Path(path).suffix in EXCLUDED_SUFFIXES:
        return False
    if any(p.startswith(".env") and p != ".env.example" for p in parts):
        return False
    if re.search(r"(?i)(credentials|secrets?|tokens?)(\.|/|$)", path):
        return False
    if path.startswith(("tasks/", ".agent/evidence/", ".agent/control", ".agent/packs/")):
        return False
    return path != "TASKS.md"


def matches(path: str, pattern: str) -> bool:
    return (
        fnmatch.fnmatchcase(path, pattern)
        or path == pattern.rstrip("/")
        or path.startswith(pattern.rstrip("/") + "/")
    )


def source_manifest(store: Store, task: Task, source: Path, *, unversioned: bool = False) -> dict:
    source = source.resolve(strict=True)
    if store.project["integration_target"]["state"] == "registered":
        identity = store.source(str(source))
        if (
            task.claim
            and str(source) == task.claim.worktree
            and identity["branch"] != task.claim.branch
        ):
            raise ControlError("Source branch changed after claim")
    try:
        head = git(source, "rev-parse", "HEAD")
        branch = git(source, "symbolic-ref", "--short", "HEAD")
        tracked = set(git(source, "ls-files", "-z").split("\0")) - {""}
        untracked = set(
            git(source, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
        ) - {""}
        staged = set(git(source, "diff", "--cached", "--name-only", "-z").split("\0")) - {""}
        unstaged = set(git(source, "diff", "--name-only", "-z").split("\0")) - {""}
    except ControlError:
        if not unversioned:
            raise
        head, branch = None, None
        tracked, untracked, staged, unstaged = set(), set(), set(), set()
        # Never descend into secret/runtime/vendor directories, even in pre-Git migration.
        for directory, dirs, names in os.walk(source, followlinks=False):
            dirs[:] = [
                d for d in dirs if d not in EXCLUDED_DIRS and not (Path(directory) / d).is_symlink()
            ]
            for name in names:
                value = str((Path(directory) / name).relative_to(source))
                if permitted(value):
                    untracked.add(value)
    patterns = (
        task.scope.owned_paths + task.scope.read_only_paths + store.project["fingerprint_inputs"]
    )
    files = {}
    for name in sorted(tracked | untracked | staged | unstaged):
        if not permitted(name) or not any(matches(name, pattern) for pattern in patterns):
            continue
        relative_path(name)
        path = source / name
        if not path.resolve().is_relative_to(source) or path.is_symlink():
            raise ControlError("Relevant source symlink denied")
        if path.exists() and path.stat().st_size > 2_000_000:
            raise ControlError("Relevant source exceeds 2 MB; declare a reviewed exclusion")
        content_hash = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        entry = {
            "sha256": content_hash,
            "staged": name in staged,
            "unstaged": name in unstaged,
            "untracked": name in untracked,
            "index_blob": None,
        }
        if name in staged and name in tracked:
            # Hash only. Never serialize diff bodies or git blob contents.
            entry["index_blob"] = git(source, "rev-parse", ":" + name)
        files[name] = entry
    fingerprint = digest(
        {
            "files": {k: v["sha256"] for k, v in files.items()},
            "spec_revision": task.spec_revision,
            "spec_digest": spec_digest(task),
            "contracts": store.contracts(task),
            "context_digest": store.context_digest(task),
        }
    )
    return {
        "repository_id": store.project["repository_id"],
        "source_worktree": str(source),
        "head": head,
        "branch": branch,
        "captured_at": now(),
        "files": files,
        "fingerprint": fingerprint,
        "spec_revision": task.spec_revision,
        "spec_digest": spec_digest(task),
        "contract_digest": store.contracts(task),
        "exclusions": [
            "secrets/.env except .env.example",
            "runtime/vendor/generated task state/evidence",
            ">2MB rejected",
            "symlinks rejected",
        ],
        "mode": "unversioned_audit" if head is None else "git_worktree",
    }


def immutable(store: Store, path: str, value: dict) -> None:
    target = store.path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise ControlError("Evidence is immutable; use a new attempt ID") from None
    with os.fdopen(fd, "w") as stream:
        stream.write(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def start_attempt(
    store: Store, task: Task, source: Path, attempt: str, *, unversioned=False
) -> str:
    if not store.contract_ready(task):
        raise ControlError("Contract baseline pending/stale; explicit spec acceptance required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", attempt):
        raise ControlError("Invalid development attempt ID")
    path = f"{task.evidence_dir}/{attempt}/source.json"
    immutable(store, path, source_manifest(store, task, source, unversioned=unversioned))
    return path


def check_report(task: Task, report: dict) -> str:
    """Validate explicit operator attestations; never execute arbitrary YAML commands."""
    entries = report.get("checks", [])
    plans = {v.id: v for v in task.verification}
    seen, covered, passed = set(), set(), True
    if not report.get("reviewer") or not report.get("started_at") or not report.get("finished_at"):
        raise ControlError("Reviewer and actual execution times required")
    try:
        start, finish = (datetime.fromisoformat(report[k]) for k in ["started_at", "finished_at"])
        if start.tzinfo is None or finish.tzinfo is None or finish < start:
            raise ValueError
    except (ValueError, TypeError):
        raise ControlError("Verification requires ordered timezone-aware execution times") from None
    for check in entries:
        check_id = check.get("id")
        if check_id not in plans or check_id in seen:
            raise ControlError("Unknown/duplicate verification ID")
        seen.add(check_id)
        plan = plans[check_id]
        if check.get("result") not in {"passed", "failed", "not_run"}:
            raise ControlError("Explicit check result required")
        if check["result"] != "passed":
            passed = False
            continue
        if set(check.get("assertions", [])) != set(plan.assertions):
            raise ControlError("All planned assertions must be explicitly confirmed")
        if plan.type == "command":
            if check.get("argv") != plan.argv or check.get("cwd") != plan.cwd:
                raise ControlError(
                    "Executed argv/cwd differs from plan; revise specification first"
                )
            if check.get("exit_code") != 0 or not check.get("log_excerpt"):
                raise ControlError("Successful command and safe result excerpt required")
            if plan.runner == "pytest":
                counts = check.get("counts", {})
                if (
                    counts.get("collected", 0) < 1
                    or counts.get("passed", 0) != counts.get("collected")
                    or any(
                        counts.get(k, 0)
                        for k in ["skipped", "failed", "errors", "xfailed", "deselected"]
                    )
                ):
                    raise ControlError(
                        "No collection, skip, deselection or environment error is not passed"
                    )
        elif not check.get("observed") or not check.get("procedure_performed"):
            raise ControlError("Manual/API/UI observation and performed steps required")
        covered.update(plan.ac_ids)
    required = {a.id for a in task.acceptance_criteria if a.required}
    if not required <= covered or seen != set(plans):
        passed = False
    return "passed" if passed else "failed"


def record_attempt(
    store: Store, task: Task, source: Path, attempt: str, report: dict, *, unversioned=False
) -> dict:
    if not store.contract_ready(task):
        raise ControlError("Contract baseline pending/stale")
    start_path = f"{task.evidence_dir}/{attempt}/source.json"
    before = json.loads(store.path(start_path).read_text())
    after = source_manifest(store, task, source, unversioned=unversioned)
    if before["fingerprint"] != after["fingerprint"] or before["source_worktree"] != str(
        source.resolve()
    ):
        raise ControlError("Source/spec/contract changed since capture; new attempt required")
    result = check_report(task, report)
    # Reject obvious unsanitized payloads rather than collecting source/private context by default.
    serialized = json.dumps(report)
    if re.search(
        r"(?i)(bearer\s+|(?:api[_-]?key|token|password|secret)\s*[=:]\s*[^\s\"']+)", serialized
    ):
        raise ControlError("Potential secret in evidence; provide a redacted result-only report")
    path = f"{task.evidence_dir}/{attempt}/result.json"
    record = {
        "task_id": task.id,
        "attempt_id": attempt,
        "mode": task.product_mode,
        "source_manifest": start_path,
        "result": result,
        "report": report,
        "spec_revision": task.spec_revision,
        "spec_digest": spec_digest(task),
        "contract_digest": store.contracts(task),
        "source_fingerprint": before["fingerprint"],
    }
    immutable(store, path, record)
    return {
        "state": result,
        "spec_revision": task.spec_revision,
        "spec_digest": spec_digest(task),
        "contract_digest": store.contracts(task),
        "source_fingerprint": before["fingerprint"],
        "evidence": path,
    }


def valid_evidence(store: Store, task: Task, source: Path) -> dict:
    summary = task.verification_summary
    if summary.state != "passed" or not summary.evidence:
        raise ControlError("Required verification has not passed")
    if not store.contract_ready(task):
        raise ControlError("Contract baseline pending/stale")
    for artifact in task.deliverables:
        if not (source / artifact.path).exists():
            raise ControlError("Required deliverable is missing from the verified source")
    record = json.loads(store.path(summary.evidence).read_text())
    if record["task_id"] != task.id or check_report(task, record["report"]) != "passed":
        raise ControlError("Evidence does not satisfy required AC")
    before = json.loads(store.path(record["source_manifest"]).read_text())
    for field in ["spec_revision", "spec_digest", "contract_digest"]:
        if record.get(field) != getattr(summary, field) or before.get(field) != getattr(
            summary, field
        ):
            raise ControlError("Evidence/summary/source manifest binding mismatch")
    if (
        record.get("source_fingerprint") != summary.source_fingerprint
        or before.get("fingerprint") != summary.source_fingerprint
    ):
        raise ControlError("Evidence source fingerprint binding mismatch")
    if (
        record.get("result") != "passed"
        or before.get("repository_id") != store.project["repository_id"]
    ):
        raise ControlError("Evidence result/repository identity mismatch")
    current = source_manifest(store, task, source, unversioned=task.kind == "control")
    if (
        summary.spec_revision != task.spec_revision
        or summary.spec_digest != spec_digest(task)
        or summary.contract_digest != store.contracts(task)
        or summary.source_fingerprint != current["fingerprint"]
    ):
        raise ControlError("Stale verification; capture and verify again")
    return record


def scope_check(store: Store, task: Task, source: Path) -> list[str]:
    if not task.claim:
        raise ControlError("Scope check requires the current claim baseline")
    base = task.claim.baseline_head
    paths = set(git(source, "diff", "--name-only", base, "HEAD", "-z").split("\0")) - {""}
    for name in paths:
        if not permitted(name) or not any(
            matches(name, pattern) for pattern in task.scope.owned_paths
        ):
            raise ControlError("Submission includes out-of-scope/control/secret changes")
    dirty = git(source, "status", "--porcelain", "--untracked-files=all")
    if dirty:
        raise ControlError("Submit a clean source commit; exclude canonical operational state")
    if not paths:
        raise ControlError("No source artifact relative to integration baseline")
    return sorted(paths)
