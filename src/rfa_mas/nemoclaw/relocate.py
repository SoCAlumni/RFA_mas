"""Agent relocation between security groups, treating an agent as a portable bundle.

Bundle = definition (assignments entry + skill) + state (``workspace-<id>`` and ``agents/<id>``).
Policy (assignments privilege of the source vs target sandbox):

Target = ``--to-sandbox <name>`` (opt-in sandbox), ``--to-groups`` (opt-in sandbox with that exact
group set) or nothing (back to the default sandbox). The target must provide the agent's groups.

- promote (target privilege > source): move with state;
- demote (target privilege < source): scan the workspace with the ``external`` censor profile and
  carry only redacted copies, or ``--wipe`` to leave state behind; a blocked file aborts (fail-closed);
- lateral: move with state.

Before touching anything the broker drains the agent (no new ask_task_agent, wait in-flight).
The move itself is ``agents apply`` on both sandboxes (live roster reconcile) plus upload.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml

from rfa_mas.nemoclaw import audit
from rfa_mas.nemoclaw import bootstrap as bs
from rfa_mas.nemoclaw import controller
from rfa_mas.nemoclaw.censor import CensorPipeline
from rfa_mas.nemoclaw.config import DEPLOY_DIR, Assignments, ConfigError, load_assignments, load_censors, load_routing
from rfa_mas.nemoclaw.manifests import agent_dir, render_identity, workspace_path, write_manifests
from rfa_mas.nemoclaw.markers import load_or_create_secret
from rfa_mas.nemoclaw.runner import Runner, SubprocessRunner

TEXT_SUFFIXES = {".md", ".txt", ".json", ".jsonl", ".yaml", ".yml", ".csv", ".log"}


@dataclass
class ScanReport:
    files: int = 0
    scanned: int = 0
    redacted_files: int = 0
    redactions: int = 0
    blocked: list[str] = field(default_factory=list)
    skipped_binary: int = 0

    def summary(self) -> dict:
        return {"files": self.files, "scanned": self.scanned, "redacted_files": self.redacted_files,
                "redactions": self.redactions, "blocked": self.blocked, "skipped_binary": self.skipped_binary}


def direction_for(assignments: Assignments, from_sandbox: str, to_sandbox: str) -> str:
    a, b = assignments.sandbox_privilege(from_sandbox), assignments.sandbox_privilege(to_sandbox)
    return "promote" if b > a else "demote" if b < a else "lateral"


def target_sandbox(assignments: Assignments, groups: list[str]) -> str:
    """Opt-in sandbox whose security-group set equals ``groups`` (never the default sandbox)."""
    key = tuple(sorted(set(groups)))
    for name, spec in assignments.sandboxes.items():
        if not spec.default and spec.group_key == key:
            return name
    raise ConfigError(f"no opt-in sandbox declares groups {list(key)}; add one under sandboxes")


def resolve_target(assignments: Assignments, agent: str, *, to_sandbox: str | None, to_groups: list[str] | None) -> str:
    """Where the agent should move: an explicit sandbox, the sandbox matching a group set, or the
    default sandbox when neither is given. The target must provide every group the agent needs."""
    if to_sandbox:
        if to_sandbox not in assignments.sandboxes:
            raise ConfigError(f"unknown sandbox {to_sandbox!r}")
        target = to_sandbox
    elif to_groups:
        target = target_sandbox(assignments, to_groups)
    else:
        target = assignments.default_sandbox
    needed = set(assignments.agents[agent].groups)
    missing = sorted(needed - set(assignments.sandboxes[target].groups))
    if missing:
        raise ConfigError(f"sandbox {target} lacks groups {missing} that agent {agent} requires")
    return target


def scan_workspace(root: Path, pipeline: CensorPipeline, profile: str = "external") -> ScanReport:
    """Censor every text file in place (redacted copy) and report; blocked files are listed."""
    report = ScanReport()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        report.files += 1
        if path.suffix.lower() not in TEXT_SUFFIXES or path.name == "IDENTITY.md":
            report.skipped_binary += 1
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            report.skipped_binary += 1
            continue
        report.scanned += 1
        result = pipeline.run(text, profile, stages=("regex",))
        if result.verdict == "block":
            report.blocked.append(str(path.relative_to(root)))
            continue
        if result.redacted_count:
            report.redacted_files += 1
            report.redactions += result.redacted_count
            path.write_text(result.text, encoding="utf-8")
    return report


def rewrite_agent_sandbox(path: Path, agent: str, sandbox: str | None) -> None:
    """Set, replace or remove ``agents.<agent>.sandbox`` in assignments.yaml without disturbing
    comments. ``None`` means "back to the default sandbox" (line removed)."""
    text = path.read_text(encoding="utf-8")
    block = re.search(rf"(?m)^  {re.escape(agent)}:\n(?P<body>(?:    .*\n)+)", text)
    if block is None:
        raise ConfigError(f"agents.{agent} not found in {path}")
    body = block.group("body")
    line_re = re.compile(r"(?m)^    sandbox: .*\n")
    if sandbox is None:
        new_body = line_re.sub("", body)
    elif line_re.search(body):
        new_body = line_re.sub(f"    sandbox: {sandbox}\n", body, count=1)
    else:
        new_body = body.replace("    kind: ", f"    sandbox: {sandbox}\n    kind: ", 1)
        if new_body == body:
            new_body = f"    sandbox: {sandbox}\n" + body
    path.write_text(text.replace(body, new_body, 1), encoding="utf-8")


def export_bundle(assignments: Assignments, agent: str, dest: Path, runner: Runner, nb: str) -> dict:
    sandbox = assignments.sandbox_for(agent)
    dest.mkdir(parents=True, exist_ok=True)
    spec = assignments.agents[agent]
    (dest / "agent.yaml").write_text(yaml.safe_dump({agent: spec.model_dump(exclude_none=True)}, allow_unicode=True), encoding="utf-8")
    skill_src = DEPLOY_DIR / "skills" / spec.skill
    if skill_src.is_dir():
        shutil.copytree(skill_src, dest / "skill", dirs_exist_ok=True)
    state = {}
    for label, remote in (("workspace", workspace_path(agent)), ("agent_dir", agent_dir(agent))):
        local = dest / label
        result = runner.run([nb, sandbox, "download", remote + "/", str(local)], timeout=300)
        state[label] = result.ok
    return {"agent": agent, "from": sandbox, "state": state, "path": str(dest)}


def import_bundle(assignments: Assignments, agent: str, bundle: Path, secret: bytes, runner: Runner, nb: str,
                  *, carry_state: bool) -> list[str]:
    sandbox = assignments.sandbox_for(agent)
    workspace = workspace_path(agent)
    uploaded: list[str] = []
    runner.run([nb, sandbox, "exec", "--timeout", "30", "--", "mkdir", "-p", f"{workspace}/skills"], timeout=90)
    if carry_state and (bundle / "workspace").is_dir():
        for path in sorted(p for p in (bundle / "workspace").rglob("*") if p.is_file()):
            rel = path.relative_to(bundle / "workspace")
            if rel.name == "IDENTITY.md":
                continue
            remote = f"{workspace}/{rel.as_posix()}"
            runner.run([nb, sandbox, "exec", "--timeout", "30", "--", "mkdir", "-p", str(Path(remote).parent)], timeout=90)
            if runner.run([nb, sandbox, "upload", str(path), remote], timeout=120).ok:
                uploaded.append(remote)
    with tempfile.TemporaryDirectory() as tmp:
        identity = Path(tmp) / "IDENTITY.md"
        identity.write_text(render_identity(assignments, agent, secret), encoding="utf-8")
        runner.run([nb, sandbox, "upload", str(identity), f"{workspace}/IDENTITY.md"], timeout=120, check=True)
        uploaded.append(f"{workspace}/IDENTITY.md")
    skill = bundle / "skill"
    if skill.is_dir():
        spec = assignments.agents[agent]
        runner.run([nb, sandbox, "upload", str(skill), f"{workspace}/skills/{spec.skill}"], timeout=300)
        uploaded.append(f"{workspace}/skills/{spec.skill}")
    return uploaded


def _admin(method: str, path: str, routing) -> dict | None:
    try:
        response = httpx.request(method, f"http://{routing.entry.bind}{path}", timeout=90)
        return response.json()
    except httpx.HTTPError:
        return None


def relocate(agent: str, to_groups: list[str] | None = None, *, to_sandbox: str | None = None,
             wipe: bool = False, dry_run: bool = False, runner: Runner | None = None) -> int:
    runner = runner or SubprocessRunner(cwd=bs.ROOT)
    assignments, routing, censors = load_assignments(), load_routing(), load_censors()
    spec = assignments.agents.get(agent)
    if spec is None or spec.kind != "task":
        print(f"error: {agent!r} is not a task agent")
        return 2
    from_sandbox = assignments.sandbox_for(agent)
    try:
        to_sandbox = resolve_target(assignments, agent, to_sandbox=to_sandbox, to_groups=to_groups)
    except ConfigError as exc:
        print(f"error: {exc}")
        return 2
    if to_sandbox == from_sandbox:
        print(f"{agent} already lives in {from_sandbox} (groups {', '.join(assignments.sandboxes[from_sandbox].groups)})")
        return 0
    direction = direction_for(assignments, from_sandbox, to_sandbox)
    plan = {"agent": agent, "from": from_sandbox, "to": to_sandbox, "direction": direction,
            "state": "wipe" if (direction == "demote" and wipe) else ("scan-then-carry" if direction == "demote" else "carry")}
    print("relocation plan:", plan)
    if dry_run:
        return 0
    nb = assignments.host.nemoclaw_bin
    started = time.monotonic()
    drained = _admin("POST", f"/broker/admin/drain/{agent}", routing)
    print(f"drain: {drained or 'broker admin unreachable (serve not running) — continuing without drain'}")
    secret = load_or_create_secret(bs.ROOT / routing.proxy.marker_key_file)
    scan: ScanReport | None = None
    with tempfile.TemporaryDirectory() as tmp:
        bundle = Path(tmp) / agent
        info = export_bundle(assignments, agent, bundle, runner, nb)
        print(f"bundle exported: {info['state']}")
        carry = True
        if direction == "demote":
            if wipe:
                carry = False
                print("demote --wipe: workspace state is not carried")
            else:
                pipeline = CensorPipeline(censors, None)
                scan = scan_workspace(bundle / "workspace", pipeline) if (bundle / "workspace").is_dir() else ScanReport()
                print(f"workspace scan: {scan.summary()}")
                if scan.blocked:
                    _admin("POST", f"/broker/admin/undrain/{agent}", routing)
                    audit.record(kind="relocation", verdict="aborted", action="demote", agent=agent, sandbox=from_sandbox,
                                 detail={"from_sandbox": from_sandbox, "to_sandbox": to_sandbox, "direction": direction,
                                         "scan": scan.summary()})
                    print("aborted: blocked content in workspace (fail-closed); use --wipe to move without state")
                    return 1
        # definition: assignments.yaml → manifests → agents apply on both sandboxes
        rewrite_agent_sandbox(DEPLOY_DIR / "assignments.yaml", agent,
                              None if to_sandbox == assignments.default_sandbox else to_sandbox)
        assignments = load_assignments()
        manifests = write_manifests(assignments, DEPLOY_DIR / "agents")
        for sandbox in (from_sandbox, to_sandbox):
            result = runner.run([nb, sandbox, "agents", "apply", "-f", str(manifests[sandbox]), "--yes", "--non-interactive"],
                                timeout=600)
            print(f"agents apply {sandbox}: {'ok' if result.ok else 'failed: ' + (result.stderr or result.stdout)[-200:]}")
            if not result.ok:
                _admin("POST", f"/broker/admin/undrain/{agent}", routing)
                return 1
        uploaded = import_bundle(assignments, agent, bundle, secret, runner, nb, carry_state=carry)
        print(f"state imported into {to_sandbox}: {len(uploaded)} paths")
        runner.run([nb, to_sandbox, "policy", "explain", "--write"], timeout=180)
    _admin("POST", f"/broker/admin/undrain/{agent}", routing)
    ms = int((time.monotonic() - started) * 1000)
    audit.record(kind="relocation", verdict="moved", action=direction, agent=agent, sandbox=to_sandbox,
                 detail={"from_sandbox": from_sandbox, "to_sandbox": to_sandbox, "direction": direction,
                         "state": plan["state"], "scan": scan.summary() if scan else None, "ms": ms})
    print(f"relocated {agent}: {from_sandbox} → {to_sandbox} ({direction}, {plan['state']}) in {ms} ms")
    return 0
