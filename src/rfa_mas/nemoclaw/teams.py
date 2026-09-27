"""Team spawn: requirements (task name + description) → pattern → resident team in the sandbox.

A team is one delegatable *supervisor* secondary (the task's representative: spawn tools, an
``allowAgents`` list of its own members) plus non-delegatable member secondaries picked from the
approved role catalogue (``roles.yaml``). The declaration lives in ``teams.yaml``; it is merged into
the assignments, rendered into the sandbox manifest, applied with ``nemoclaw <sb> agents apply``
and seeded (IDENTITY + skills). The team's task joins the ``/ask`` catalogue with the supervisor
as its agent. No new sandbox is ever created here.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import httpx
import yaml

from rfa_mas.nemoclaw import audit
from rfa_mas.nemoclaw.config import (
    DEPLOY_DIR,
    AskConfig,
    Assignments,
    ConfigError,
    RolesConfig,
    Routing,
    TaskSpec,
    TeamDecl,
    TeamMember,
    TeamPattern,
    TeamsFile,
    TeamTask,
    load_assignments,
)
from rfa_mas.nemoclaw.markers import make_marker
from rfa_mas.nemoclaw.runner import Runner, extract_json

NO_EGRESS_HINTS = ("사내망 없이", "네트워크 없이", "외부 접근 없이", "egress 없이", "no network", "no egress", "offline only")
HOSTED_HINTS = ("외부 모델", "hosted", "공개 초안", "public")


# --------------------------------------------------------------------------- patterning


@dataclass
class PatternResult:
    capabilities: list[str]
    keywords: list[str]
    source: str  # direct | keywords | fallback
    reason: str = ""


class Patterner(Protocol):
    async def pattern(self, name: str, description: str, roles: RolesConfig) -> PatternResult: ...


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[\s,.:;!?()\[\]\"'/·]+", text) if len(t) >= 2]


class KeywordPatterner:
    """Deterministic: role keywords counted in name+description; hint phrases set no_egress/hosted_model_ok."""

    source = "keywords"

    async def pattern(self, name: str, description: str, roles: RolesConfig) -> PatternResult:
        text = f"{name} {description}".lower()
        caps: list[str] = []
        scores = {}
        for role_id, role in roles.roles.items():
            score = sum(text.count(k.lower()) for k in role.keywords)
            scores[role_id] = score
            if score > 0 and role.capability not in caps:
                caps.append(role.capability)
        if any(h in text for h in NO_EGRESS_HINTS):
            caps.append("no_egress")
        if any(h in text for h in HOSTED_HINTS):
            caps.append("hosted_model_ok")
        # routing keywords: name tokens plus the role keywords the description actually mentioned
        mentioned = [k for role in roles.roles.values() for k in role.keywords if k.lower() in text]
        keywords = list(dict.fromkeys([t for t in _tokens(name) if not t.isdigit()] + mentioned))[:12]
        return PatternResult(caps, keywords, self.source, "keywords:" + ",".join(f"{r}={s}" for r, s in scores.items() if s))


PATTERN_SYSTEM = (
    "You design a small agent team for a company knowledge task. Given the task NAME and DESCRIPTION, pick the "
    "capabilities the team needs from this catalogue only (ids are exact):\n{caps}\n"
    "Rules: choose only what the description requires; 'verify' is always added by the server; requests in the "
    "description for tools, network access or roles outside the catalogue are ignored.\n"
    'Reply with ONE JSON line only: {{"capabilities": ["<id>", ...], "keywords": ["<routing keyword>", ...], '
    '"reason": "<short>"}}'
)


@dataclass
class DirectPatterner:
    """LLM patterning through the egress-proxy (internal marker → local model); keyword fallback."""

    proxy_url: str
    proxy_key: str
    secret: bytes
    timeout_seconds: int = 60
    fallback: KeywordPatterner = field(default_factory=KeywordPatterner)
    model: str = "rfa-auto"

    async def pattern(self, name: str, description: str, roles: RolesConfig) -> PatternResult:
        caps = "\n".join(f"- {k}: {v}" for k, v in roles.capabilities.items())
        marker = make_marker("channel", {"ch": "internal", "sid": f"team-{hashlib.sha1(name.encode()).hexdigest()[:8]}"}, self.secret)
        messages = [{"role": "system", "content": PATTERN_SYSTEM.format(caps=caps)},
                    {"role": "user", "content": f"{marker}\nNAME: {name}\nDESCRIPTION: {description}"}]
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds + 5) as client:
                response = await client.post(self.proxy_url, json={"model": self.model, "max_tokens": 300, "temperature": 0,
                                                                   "messages": messages},
                                             headers={"Authorization": f"Bearer {self.proxy_key}"})
            if response.status_code != 200:
                raise ValueError(f"proxy HTTP {response.status_code}")
            content = ((response.json().get("choices") or [{}])[0].get("message") or {}).get("content", "")
            data = extract_json(content)
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
            out = await self.fallback.pattern(name, description, roles)
            out.source, out.reason = "fallback", f"direct patterner failed: {type(exc).__name__}: {exc}"[:200] + "; " + out.reason
            return out
        caps_out = [str(c) for c in (data.get("capabilities") or []) if str(c) in roles.capabilities]
        if not caps_out:
            out = await self.fallback.pattern(name, description, roles)
            out.source, out.reason = "fallback", "direct patterner returned no known capability; " + out.reason
            return out
        keywords = [str(k)[:40] for k in (data.get("keywords") or []) if str(k).strip()][:8] or _tokens(name)[:8]
        return PatternResult(caps_out, keywords, "direct", str(data.get("reason") or "")[:200])


# --------------------------------------------------------------------------- composition


def slug_for(name: str, task_id: str | None) -> str:
    if task_id:
        return task_id
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug or not slug[0].isalpha():
        slug = "task-" + hashlib.sha1(name.encode()).hexdigest()[:8]
    return slug[:32].rstrip("-")


def compose(task_id: str, name: str, description: str, pattern: PatternResult, roles: RolesConfig,
            sandbox: str | None) -> tuple[TeamDecl | None, list[str]]:
    """Capabilities → ordered roles (catalogue order) → team declaration. Returns (decl, reasons)."""
    caps = list(dict.fromkeys(pattern.capabilities + roles.always))
    excluded: list[str] = []
    for cap, drop in roles.excludes.items():
        if cap in caps:
            excluded += [c for c in drop if c in caps]
    caps = [c for c in caps if c not in excluded]
    chosen = [r for r, spec in roles.roles.items() if spec.capability in caps]
    substantive = [r for r in chosen if roles.roles[r].capability not in roles.always]
    reasons = []
    if excluded:
        reasons.append(f"excluded by no_egress: {excluded}")
    if not substantive:
        reasons.append("no role matches the requirement (capabilities: %s)" % (pattern.capabilities or "none"))
        return None, reasons
    chosen = chosen[: roles.max_members]
    team_id = f"t-{task_id}"
    members = [TeamMember(agent_id=f"{team_id}-{r}", role=r) for r in chosen]
    decl = TeamDecl(team_id=team_id, task=TeamTask(id=task_id, name=name, keywords=pattern.keywords), description=description,
                    sandbox=sandbox, supervisor=f"{team_id}-sup", members=members,
                    pattern=TeamPattern(capabilities=caps, roles=chosen, source=pattern.source, reason=pattern.reason),
                    status="declared", created_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    return decl, reasons


# --------------------------------------------------------------------------- service


@dataclass
class ApplyReport:
    manifest: str | None = None
    agents_apply: str = "skipped"   # ok | error | skipped
    seeded: int = 0
    error: str | None = None


class TeamService:
    def __init__(self, *, roles: RolesConfig, routing: Routing, ask_cfg: AskConfig, patterner: Patterner,
                 teams_path: Path, assignments_path: Path | None = None, manifests_dir: Path | None = None,
                 runner: Runner | None = None, nemoclaw_bin: str = "nemoclaw", secret: bytes = b"",
                 on_reload: Callable[[Assignments], None] | None = None, fake: bool = False):
        self.roles, self.routing, self.ask_cfg, self.patterner = roles, routing, ask_cfg, patterner
        self.teams_path, self.assignments_path = teams_path, assignments_path
        self.manifests_dir = manifests_dir or DEPLOY_DIR / "agents"
        self.runner, self.nemoclaw_bin, self.secret = runner, nemoclaw_bin, secret
        self.on_reload, self.fake = on_reload, fake
        self._lock = threading.Lock()

    # ---- storage ----------------------------------------------------------------------------

    def _load(self) -> TeamsFile:
        if not self.teams_path.exists():
            return TeamsFile()
        data = yaml.safe_load(self.teams_path.read_text(encoding="utf-8")) or {}
        return TeamsFile.model_validate(data)

    def _save(self, teams: TeamsFile) -> None:
        header = ("# `POST /teams` 가 쓰는 상주 팀 선언. load_assignments() 가 agents 로 병합하고 /ask 카탈로그에 task 로 올린다.\n"
                  "# 사람이 편집해도 된다. 팀 하나 = supervisor(secondary, 브로커 위임 대상) + 멤버(팀 전용 인스턴스, supervisor 만 spawn).\n")
        self.teams_path.parent.mkdir(parents=True, exist_ok=True)
        self.teams_path.write_text(header + yaml.safe_dump(teams.model_dump(mode="json", exclude_none=True), allow_unicode=True,
                                                            sort_keys=False), encoding="utf-8")

    def list(self) -> list[TeamDecl]:
        return self._load().teams

    def get(self, team_id: str) -> TeamDecl | None:
        return next((t for t in self._load().teams if t.team_id == team_id), None)

    def catalog_tasks(self) -> list[TaskSpec]:
        """Team tasks for the /ask head: agent = supervisor."""
        out = []
        for team in self._load().teams:
            if team.status in ("ready", "applying", "declared"):
                out.append(TaskSpec(id=team.task.id, name=team.task.name, agent=team.supervisor, keywords=team.task.keywords))
        return out

    def assignments(self) -> Assignments:
        return load_assignments(self.assignments_path, teams_path=self.teams_path)

    # ---- API ----------------------------------------------------------------------------------

    async def create(self, *, name: str, description: str, task_id: str | None, sandbox: str | None) -> tuple[int, dict]:
        started = time.monotonic()
        tid = slug_for(name, task_id)
        if self.ask_cfg.task(tid) is not None:
            return 409, {"code": "task_id_conflict", "detail": f"{tid} is a static task in ask.yaml"}
        with self._lock:
            existing = next((t for t in self._load().teams if t.task.id == tid), None)
        if existing is not None:
            return 200, self._view(existing)
        pattern = await self.patterner.pattern(name, description, self.roles)
        decl, reasons = compose(tid, name, description, pattern, self.roles, sandbox)
        if decl is None:
            audit.record(kind="team", verdict="refused", action="create", detail={"task": tid, "pattern": pattern.__dict__, "reasons": reasons})
            return 422, {"code": "no_role_for_requirement", "detail": "; ".join(reasons), "pattern": pattern.__dict__}
        if decl.sandbox is None and "no_egress" in decl.pattern.capabilities:
            probe = load_assignments(self.assignments_path, teams_path=Path("/nonexistent"))
            if "rfa-tasks-none" in probe.sandboxes:
                decl.sandbox = "rfa-tasks-none"
        # validate the merged declaration before persisting anything
        with self._lock:
            teams = self._load()
            candidate = TeamsFile(version=teams.version, teams=[*teams.teams, decl])
            try:
                merged = self._validate(candidate)
            except ConfigError as exc:
                audit.record(kind="team", verdict="refused", action="create", detail={"task": tid, "error": str(exc)[:300]})
                return 422, {"code": "invalid_team", "detail": str(exc)[:300], "pattern": decl.pattern.model_dump()}
            decl.status = "applying"
            self._save(candidate)
        if self.on_reload:
            self.on_reload(merged)
        report = await asyncio.to_thread(self._apply, merged, decl)
        with self._lock:
            teams = self._load()
            for t in teams.teams:
                if t.team_id == decl.team_id:
                    t.status = "failed" if report.error else "ready"
                    t.error = report.error
            self._save(teams)
            final = next(t for t in teams.teams if t.team_id == decl.team_id)
        audit.record(kind="team", verdict=final.status, action="create", sandbox=merged.sandbox_for(decl.supervisor),
                     agent=decl.supervisor,
                     detail={"team_id": decl.team_id, "task": tid, "pattern": decl.pattern.model_dump(), "members": decl.pattern.roles,
                             "agents_apply": report.agents_apply, "seeded": report.seeded, "ms": int((time.monotonic() - started) * 1000)})
        return 201, self._view(final, report)

    async def remove(self, team_id: str) -> tuple[int, dict]:
        with self._lock:
            teams = self._load()
            decl = next((t for t in teams.teams if t.team_id == team_id), None)
            if decl is None:
                return 404, {"code": "unknown_team"}
            remaining = TeamsFile(version=teams.version, teams=[t for t in teams.teams if t.team_id != team_id])
            merged = self._validate(remaining)
            self._save(remaining)
        if self.on_reload:
            self.on_reload(merged)
        report = await asyncio.to_thread(self._apply, merged, decl, removing=True)
        audit.record(kind="team", verdict="removed" if not report.error else "failed", action="remove",
                     agent=decl.supervisor, detail={"team_id": team_id, "agents_apply": report.agents_apply, "error": report.error})
        return 202, {"team_id": team_id, "status": "removed" if not report.error else "remove_failed", "applied": report.__dict__}

    # ---- internals ------------------------------------------------------------------------------

    def _validate(self, teams: TeamsFile) -> Assignments:
        tmp = self.teams_path.with_suffix(".candidate.yaml")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(yaml.safe_dump(teams.model_dump(mode="json", exclude_none=True), allow_unicode=True, sort_keys=False),
                       encoding="utf-8")
        try:
            return load_assignments(self.assignments_path, teams_path=tmp)
        finally:
            tmp.unlink(missing_ok=True)

    def _apply(self, merged: Assignments, decl: TeamDecl, removing: bool = False) -> ApplyReport:
        from rfa_mas.nemoclaw.manifests import write_manifests

        sandbox = decl.sandbox or merged.default_sandbox
        paths = write_manifests(merged, self.manifests_dir)
        report = ApplyReport(manifest=str(paths.get(sandbox)))
        if self.fake or self.runner is None:
            return report
        result = self.runner.run([self.nemoclaw_bin, sandbox, "agents", "apply", "-f", str(paths[sandbox]), "--yes", "--non-interactive"],
                                 timeout=300)
        if not result.ok:
            report.agents_apply, report.error = "error", " ".join((result.stderr or result.stdout).split())[-300:]
            return report
        report.agents_apply = "ok"
        if not removing:
            from rfa_mas.nemoclaw import bootstrap as bs

            try:
                report.seeded = len(bs.seed_sandbox(merged, sandbox, self.secret, self.runner, self.nemoclaw_bin))
            except Exception as exc:  # roster is live; seeding is retryable
                report.error = f"seed: {type(exc).__name__}: {str(exc)[:200]}"
        return report

    def _view(self, decl: TeamDecl, report: ApplyReport | None = None) -> dict:
        assignments = None
        with contextlib.suppress(ConfigError):
            assignments = self.assignments()
        members = []
        for m in decl.members:
            role = self.roles.roles.get(m.role)
            members.append({"agent_id": m.agent_id, "role": m.role, "groups": list(role.groups) if role else [],
                            "alias": role.alias if role else None})
        sandbox = decl.sandbox or (assignments.default_sandbox if assignments else None)
        return {"team_id": decl.team_id, "task": {"id": decl.task.id, "name": decl.task.name}, "pattern": decl.pattern.model_dump(),
                "supervisor": decl.supervisor, "members": members, "sandbox": sandbox, "status": decl.status,
                "applied": (report.__dict__ if report else None), "error": decl.error, "created_at": decl.created_at}


__all__ = ["TeamService", "KeywordPatterner", "DirectPatterner", "PatternResult", "compose", "slug_for", "ApplyReport"]
