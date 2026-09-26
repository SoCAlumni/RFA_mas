from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from pydantic import ValidationError

from .schema import SPEC_FIELDS, ControlError, Task, parse_yaml, relative_path


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def dump(value) -> str:
    return yaml.safe_dump(value, allow_unicode=True, sort_keys=False, width=100)


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        timeout=15,
        env={"PATH": os.environ.get("PATH", ""), "LANG": "C", "GIT_CONFIG_NOSYSTEM": "1"},
    )
    if result.returncode:
        raise ControlError("Git baseline unavailable; coordinator must establish it explicitly")
    return result.stdout.strip()


def atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ControlError("Symlink write denied")
    if path.exists() and path.read_text() == content:
        return
    fd, name = tempfile.mkstemp(prefix=".taskctl-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def prefix(pattern: str) -> str:
    return pattern[
        : min([pattern.find(c) for c in "*?[" if c in pattern] or [len(pattern)])
    ].rstrip("/")


def overlaps(left: str, right: str) -> bool:
    # Ambiguous glob prefixes are deliberately conflicting, not optimistically independent.
    a, b = prefix(left), prefix(right)
    if not a or not b:
        return True
    if any(c in left + right for c in "*?["):
        return a.startswith(b) or b.startswith(a)
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def conflict(a: Task, b: Task) -> bool:
    return bool(set(a.scope.shared_resources) & set(b.scope.shared_resources)) or any(
        overlaps(x, y) for x in a.scope.owned_paths for y in b.scope.owned_paths
    )


def spec_digest(task: Task) -> str:
    data = task.model_dump(mode="json")
    return digest({key: data[key] for key in sorted(SPEC_FIELDS)})


def expired(task: Task) -> bool:
    return bool(
        task.claim and datetime.fromisoformat(task.claim.lease_expires_at) <= datetime.now(UTC)
    )


class Store:
    """Same-host advisory lock. All task decisions re-read originals under this lock."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve(strict=True)
        marker = self.path(".agent/control.json")
        if not marker.is_file():
            raise ControlError("Not a registered control root")
        self.marker = json.loads(marker.read_text())
        if self.marker.get("control_root") != str(self.root):
            raise ControlError("Copied/wrong control root; automatic local fallback is forbidden")
        self.project = parse_yaml(self.path("tasks/project.yaml").read_text())
        if self.marker.get("repository_id") != self.project.get("repository_id"):
            raise ControlError("Repository identity mismatch")
        if self.project.get("control_root") != str(self.root):
            raise ControlError("Project/control root mismatch")
        self.check_project()

    def check_project(self) -> None:
        if self.project.get("repository_id") != self.marker.get(
            "repository_id"
        ) or self.project.get("control_root") != str(self.root):
            raise ControlError("Repository/control root changed while waiting for lock")
        if not 1 <= self.project["max_workers"] <= 3 or self.project["lease_seconds"] < 1:
            raise ControlError("Invalid concurrency/lease configuration")
        for pattern in self.project["fingerprint_inputs"]:
            relative_path(pattern, glob=True)

    def path(self, value: str) -> Path:
        relative_path(value)
        path = self.root / value
        if not path.resolve().is_relative_to(self.root) or any(
            parent.is_symlink() for parent in [path, *path.parents] if parent != self.root.parent
        ):
            raise ControlError("Path escape or symlink denied")
        return path

    @contextlib.contextmanager
    def lock(self):
        lock_path = self.path(".agent/control.lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            # Reload settings as well: another coordinator may have changed the baseline.
            self.project = parse_yaml(self.path("tasks/project.yaml").read_text())
            self.check_project()
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def tasks(self) -> dict[str, Task]:
        result = {}
        for path in sorted(self.path("tasks").glob("*/task.yaml")):
            self.path(str(path.relative_to(self.root)))
            try:
                task = Task.model_validate(parse_yaml(path.read_text()))
            except ValidationError as exc:
                fields = ", ".join(".".join(map(str, e["loc"])) for e in exc.errors())
                raise ControlError(f"Invalid task {path.parent.name}: {fields}") from None
            if path.parent.name != task.id or task.id in result:
                raise ControlError("Duplicate/misplaced task ID")
            result[task.id] = task
        if not result:
            raise ControlError("No task originals")
        return result

    def contracts(self, task: Task) -> str:
        registry = self.project["contracts"]
        selected = {}
        for ref in task.contract_refs:
            if ref.id not in registry:
                raise ControlError(f"Unknown contract in {task.id}")
            current = registry[ref.id]
            path = self.path(current["path"])
            actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            selected[ref.id] = {"version": current["version"], "digest": actual}
        return digest(selected)

    def context_digest(self, task: Task) -> str:
        return digest(
            {
                "spec": spec_digest(task),
                "contracts": self.contracts(task),
                "global": {
                    p: hashlib.sha256(self.path(p).read_bytes()).hexdigest()
                    for p in task.context.global_refs
                },
            }
        )

    def needs_recovery(self, task: Task) -> bool:
        return expired(task) or bool(
            task.claim
            and (
                task.claim.recovery_required
                or task.claim.context_digest != self.context_digest(task)
            )
        )

    def contract_ready(self, task: Task) -> bool:
        for ref in task.contract_refs:
            entry = self.project["contracts"][ref.id]
            if ref.role == "provider" and entry.get("state") == "planned":
                continue  # This task produces the missing contract; it does not consume it.
            path = self.path(entry["path"])
            if not path.is_file() or entry.get("state") != "ready":
                return False
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if (
                ref.version != entry["version"]
                or entry["digest"] != actual
                or (ref.role == "consumer" and ref.digest != actual)
            ):
                return False
        return True

    def validate(self, tasks: dict[str, Task]) -> None:
        resources = self.project["shared_resources"]
        for task in tasks.values():
            if task.workstream not in self.project["workstreams"]:
                raise ControlError(f"Unknown workstream: {task.id}")
            if set(task.scope.shared_resources) - set(resources):
                raise ControlError(f"Unknown shared resource: {task.id}")
            for dep in task.depends_on:
                if dep.id not in tasks or dep.id == task.id:
                    raise ControlError(f"Unknown/self dependency: {task.id}")
            if len({d.id for d in task.depends_on}) != len(task.depends_on):
                raise ControlError(f"Duplicate dependency: {task.id}")
            self.contracts(task)
            known_results = {f"tasks/{d.id}/handoff.md" for d in task.depends_on}
            if not set(task.context.dependency_results) <= known_results:
                raise ControlError(f"Direct predecessor result references mismatch: {task.id}")
            for ref in task.context.read:
                if not self.path(ref.path).exists() and ref.path not in task.scope.planned_paths:
                    raise ControlError(f"Unmarked missing context path: {task.id}: {ref.path}")
            for ref in task.context.global_refs:
                if not self.path(ref).is_file():
                    raise ControlError(f"Missing global reference: {task.id}")
            for output in task.deliverables:
                if output.availability == "existing" and not self.path(output.path).exists():
                    raise ControlError(f"Existing deliverable missing: {task.id}: {output.path}")
                if output.availability == "planned" and output.path not in task.scope.planned_paths:
                    raise ControlError(f"Planned path not declared: {task.id}")
            for path in (
                task.scope.owned_paths + task.scope.read_only_paths + task.scope.planned_paths
            ):
                self.path(prefix(path) or "tasks")
            for ref in task.requirement_refs:
                if not self.path(ref.split("#")[0]).exists():
                    raise ControlError(f"Missing requirement reference: {task.id}")
            if (
                task.latest_evidence_file
                and not self.path(task.latest_evidence_file).is_file()
                and task.verification_summary.state != "stale"
            ):
                raise ControlError(f"Missing evidence: {task.id}")
        visiting, visited = set(), set()

        def visit(task_id):
            if task_id in visiting:
                raise ControlError("Dependency cycle")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dep in tasks[task_id].depends_on:
                visit(dep.id)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in tasks:
            visit(task_id)
        legacy = self.project.get("migration", {}).get("legacy_ids", [])
        if set(legacy) - set(tasks):
            raise ControlError("Unmapped legacy task")
        if len(legacy) != len(set(legacy)):
            raise ControlError("Duplicate migration ID")

    def complete(self, task: Task) -> bool:
        basic = (
            task.status == "done"
            and task.verification_summary.state == "passed"
            and task.integration.state in {"integrated", "not_required"}
            and bool(task.integration.evidence)
            and self.path(task.integration.evidence).is_file()
            and self.path(task.context.handoff).is_file()
        )
        if not basic:
            return False
        if task.integration.result and task.integration.result.get("kind") == "legacy_baseline":
            migration = self.project.get("migration", {})
            archive = migration.get("source")
            return (
                task.id in migration.get("legacy_ids", [])
                and task.migration.original_status == "done"
                and archive == task.integration.evidence
                and hashlib.sha256(self.path(archive).read_bytes()).hexdigest()
                == migration.get("source_sha256")
            )
        from .evidence import valid_evidence

        try:
            target = Path(self.project["integration_target"]["worktree"])
            valid_evidence(self, task, target)
            return True
        except (ControlError, OSError, ValueError, KeyError):
            return False

    def reconcile(self, tasks: dict[str, Task]) -> None:
        """Persist invalidated completion, without preventing repair commands."""
        for task in tasks.values():
            if task.status != "done" or self.complete(task):
                continue
            task.verification_summary.state = "stale"
            task.status = "verifying"
            task.revision += 1
            task.updated_at = now()
            task.next_action = (
                "Coordinator: inspect changed source/spec/contract, "
                "recover reservation, reverify impacted AC"
            )
            if task.kind != "control":
                task.integration.reservation = True
            atomic(self.path(f"tasks/{task.id}/task.yaml"), dump(task.model_dump(mode="json")))

    def readiness(self, task: Task, tasks: dict[str, Task]) -> dict:
        reasons = []
        if task.kind in {"group", "milestone"}:
            reasons.append("non-executable group")
        if task.status != "todo":
            reasons.append(f"status:{task.status}")
        if task.spec_state != "ready" or task.unresolved:
            reasons.append("spec unresolved")
        if not self.contract_ready(task):
            reasons.append("contract baseline pending/stale")
        for dep in task.depends_on:
            if not self.complete(tasks[dep.id]):
                reasons.append("dependency:" + dep.id)
        planning = not reasons
        baseline = self.project["integration_target"]
        if baseline.get("state") != "registered":
            reasons.append("source Git baseline unregistered")
        for other in tasks.values():
            if (
                other.id != task.id
                and (other.claim or other.integration.reservation)
                and conflict(task, other)
            ):
                reasons.append("reserved:" + other.id)
        return {"planning_ready": planning, "executable": not reasons, "reasons": reasons}

    def save(self, task: Task) -> dict:
        task = Task.model_validate(task.model_dump(mode="json"))
        atomic(self.path(f"tasks/{task.id}/task.yaml"), dump(task.model_dump(mode="json")))
        # A source save is committed even if a derived view fails. Never claim rollback.
        try:
            self.refresh(self.tasks())
            return {"source_saved": True, "views": "current", "revision": task.revision}
        except (OSError, ControlError) as exc:
            return {
                "source_saved": True,
                "views": "repair_required",
                "revision": task.revision,
                "warning": type(exc).__name__,
            }

    def expected_views(self, tasks: dict[str, Task]) -> dict[str, str]:
        revisions = {t.id: t.revision for t in tasks.values()}
        source_digest = digest(
            {
                "project": self.project,
                "sources": {k: t.model_dump(mode="json") for k, t in tasks.items()},
            }
        )
        stamp = now()
        try:
            previous = parse_yaml(self.path("tasks/state.yaml").read_text())
            candidate = previous.get("generated_at")
            if previous.get("source_digest") == source_digest and candidate:
                datetime.fromisoformat(candidate)
                stamp = candidate  # No timestamp-only diff for the same original set.
        except (OSError, ControlError, ValueError, TypeError):
            pass
        metadata = {
            "generated": True,
            "schema_version": "1.0",
            "source_digest": source_digest,
            "source_revisions": revisions,
            "generated_at": stamp,
        }
        rows, index = [], []
        for task in tasks.values():
            ready = self.readiness(task, tasks)
            rows.append(
                {
                    "id": task.id,
                    "status": task.status,
                    "workstream": task.workstream,
                    "owner": task.claim.session_id if task.claim else None,
                    "recovery_required": self.needs_recovery(task),
                    "integration": task.integration.state,
                    "reserved": task.integration.reservation,
                    "blocker": task.blocker.cause if task.blocker else None,
                    "verification": task.verification_summary.state,
                    **ready,
                }
            )
            index.append(
                {
                    "id": task.id,
                    "title": task.title,
                    "workstream": task.workstream,
                    "depends_on": [d.id for d in task.depends_on],
                    "path": f"tasks/{task.id}/task.yaml",
                }
            )
        gates = {
            label: [{"id": key, "passed": self.complete(tasks[key])} for key in ids]
            for label, ids in self.project["final_acceptance"].items()
        }
        state = {
            **metadata,
            "counts": {
                s: sum(t.status == s for t in tasks.values())
                for s in [
                    "todo",
                    "in_progress",
                    "verifying",
                    "blocked",
                    "done",
                    "deferred",
                    "cancelled",
                ]
            },
            "final_acceptance": gates,
            "tasks": rows,
        }
        lines = [
            "# 개발 작업 현황 — 자동 생성",
            "",
            "직접 수정하지 마세요. `tasks/<ID>/task.yaml`만 원본이며 `taskctl`로 갱신합니다.",
            "",
            "[공통 맥락](docs/PROJECT_CONTEXT.md) · [실행 규칙](TASK_EXECUTION_RULES.md) · "
            "[이관 기록](docs/TASK_MIGRATION.md) · [요구사항/일정](docs/PRODUCT_REQUIREMENTS.md)",
            "",
            f"Canonical control root: `{self.root}`. "
            f"Git baseline: **{self.project['integration_target']['state']}**.",
            "",
            "개발 작업 완료와 제품 최종 gate는 별개입니다. "
            "mock 통과는 실제 NVIDIA/Skill/NemoClaw/OpenShell 증거가 아닙니다.",
            "",
            "| ID | 작업 | 우선순위 | 상태 / 검증 | stream | h | 선행 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for task in tasks.values():
            lines.append(
                f"| [{task.id}](tasks/{task.id}/task.yaml) | {task.title} | {task.priority} | "
                f"{task.status} / {task.verification_summary.state} | {task.workstream} | "
                f"{task.estimated_effort:g} | {', '.join(d.id for d in task.depends_on) or '—'} |"
            )
        lines += [
            "",
            "## 다음 작업",
            "",
            *[
                f"- {r['id']}: planning ready; "
                f"{'claim 가능' if r['executable'] else ', '.join(r['reasons'])}"
                for r in rows
                if r["planning_ready"]
            ],
            "",
            "## 최종 gate",
            "",
        ]
        for label, values in gates.items():
            lines.append(
                f"- {label}: "
                + ", ".join(
                    f"{v['id']}={'passed' if v['passed'] else 'not_passed'}" for v in values
                )
            )
        return {
            "tasks/index.yaml": dump({**metadata, "tasks": index}),
            "tasks/state.yaml": dump(state),
            "TASKS.md": "\n".join(lines) + "\n",
        }

    def refresh(self, tasks: dict[str, Task]) -> None:
        for path, content in self.expected_views(tasks).items():
            atomic(self.path(path), content)

    def views_current(self, tasks: dict[str, Task]) -> bool:
        return all(
            self.path(p).is_file() and self.path(p).read_text() == v
            for p, v in self.expected_views(tasks).items()
        )

    def guard(self, task: Task, revision: int, session: str, generation: int) -> None:
        if task.revision != revision:
            raise ControlError("Stale revision; re-read task and claim")
        claim = task.claim
        if not claim or claim.session_id != session or claim.generation != generation:
            raise ControlError("Stale/foreign claim generation")
        if self.needs_recovery(task):
            raise ControlError("Recovery required; expiry does not authorize re-execution")

    def coordinator(self, session: str) -> None:
        if session not in self.project["coordinator_sessions"]:
            raise ControlError("Coordinator session required")

    def source(self, path: str, *, feature: bool = False, current: bool = False) -> dict:
        source = Path(path).resolve(strict=True)
        target = self.project["integration_target"]
        if target["state"] != "registered":
            raise ControlError("Git baseline unregistered; no claim without a checked baseline")
        common = Path(
            git(source, "rev-parse", "--path-format=absolute", "--git-common-dir")
        ).resolve()
        if str(common) != target["git_common_dir"]:
            raise ControlError("Source belongs to another repository")
        head = git(source, "rev-parse", "HEAD")
        git(source, "merge-base", "--is-ancestor", target["baseline_head"], head)
        branch = git(source, "symbolic-ref", "--short", "HEAD")
        target_head = git(Path(target["worktree"]), "rev-parse", "HEAD")
        if current and head != target_head:
            raise ControlError(
                "Source must start from the current integrated HEAD, not an older baseline"
            )
        if current and feature and git(source, "status", "--porcelain", "--untracked-files=all"):
            raise ControlError(
                "New claim requires a clean feature baseline; "
                "preserve existing edits and recover explicitly"
            )
        if feature and (source == Path(target["worktree"]) or branch == target["branch"]):
            raise ControlError("Worker requires a separate feature worktree/branch")
        return {"worktree": str(source), "head": head, "branch": branch, "target_head": target_head}

    def lease(self) -> str:
        return (datetime.now(UTC) + timedelta(seconds=self.project["lease_seconds"])).isoformat(
            timespec="seconds"
        )
