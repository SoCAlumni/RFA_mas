"""Typed loaders for assignments.yaml, routing.yaml and censors.yaml."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

DEPLOY_DIR = Path(__file__).resolve().parents[3] / "deploy" / "nemoclaw"
AGENT_ID = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
SANDBOX_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
AUTO_MODE = "rfa-auto"


class ConfigError(ValueError):
    """A declared configuration is inconsistent (reported with the offending key)."""


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- assignments


class HostConfig(Strict):
    lan_ip: str
    sandbox_host_alias: str = "host.openshell.internal"
    nemoclaw_bin: str = "nemoclaw"


class SecurityGroup(Strict):
    description: str = ""
    privilege: int = Field(ge=0, le=10)
    presets: list[str] = []
    mcp_servers: list[str] = []
    fallback_presets: list[str] = []


class SandboxSpec(Strict):
    groups: list[str] = Field(min_length=1)
    default: bool = False   # exactly one: every agent lands here unless it opts into another sandbox

    @property
    def group_key(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.groups)))


class ToolPolicy(Strict):
    profile: str | None = None
    allow: list[str] = []
    deny: list[str] = []

    @model_validator(mode="after")
    def _non_empty(self) -> ToolPolicy:
        if not self.allow and not self.deny:
            raise ValueError("tools must declare a non-empty allow[] or deny[]")
        return self


class AgentSpec(Strict):
    kind: Literal["fixed", "task"]
    sandbox: str | None = None      # fixed: required (it becomes that sandbox's `main`); task: optional opt-in
    groups: list[str] = []          # egress the agent needs; the sandbox it lands in must provide all of them
    alias: str
    skill: str
    description: str = ""
    tools: ToolPolicy
    delegatable: bool = True  # False: lives in a sandbox but is not an ask_task_agent/sessions_spawn target (censor)
    team: str | None = None          # set when the agent was spawned as part of a team (teams.yaml)
    allow_agents: list[str] = []     # spawn allowlist of a team supervisor (its own members only)

    @property
    def group_key(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.groups)))


class Assignments(Strict):
    """Placement model: one *default* sandbox hosts every agent; additional sandboxes exist only
    when an agent opts into them with ``sandbox:``. A sandbox is still one security-group
    combination, and an agent may only land where all of its required groups are provided."""

    version: int = 1
    host: HostConfig
    baseline_excludes: list[str] = []
    security_groups: dict[str, SecurityGroup]
    sandboxes: dict[str, SandboxSpec]
    onboarding_order: list[str] = []
    agents: dict[str, AgentSpec]

    @model_validator(mode="after")
    def _consistent(self) -> Assignments:
        defaults = [n for n, s in self.sandboxes.items() if s.default]
        if len(defaults) != 1:
            raise ValueError("exactly one sandbox must declare default: true")
        for name, sandbox in self.sandboxes.items():
            if not SANDBOX_NAME.match(name):
                raise ValueError(f"sandboxes.{name}: invalid sandbox name")
            for group in sandbox.groups:
                if group not in self.security_groups:
                    raise ValueError(f"sandboxes.{name}: unknown security group {group!r}")
        fixed_owner: dict[str, str] = {}
        for agent_id, agent in self.agents.items():
            if not AGENT_ID.match(agent_id) or agent_id == "main":
                raise ValueError(f"agents.{agent_id}: invalid agent id (lowercase, not 'main')")
            for group in agent.groups:
                if group not in self.security_groups:
                    raise ValueError(f"agents.{agent_id}: unknown security group {group!r}")
            if agent.sandbox is not None and agent.sandbox not in self.sandboxes:
                raise ValueError(f"agents.{agent_id}: unknown sandbox {agent.sandbox!r}")
            if agent.kind == "fixed":
                if agent.sandbox is None:
                    raise ValueError(f"agents.{agent_id}: fixed agents must name their sandbox")
                if agent.sandbox in fixed_owner:
                    raise ValueError(
                        f"agents.{agent_id}: sandbox {agent.sandbox} already has fixed agent "
                        f"{fixed_owner[agent.sandbox]} as its main"
                    )
                fixed_owner[agent.sandbox] = agent_id
            placed = self.sandboxes[agent.sandbox or defaults[0]]
            missing = sorted(set(agent.groups) - set(placed.groups))
            if missing:
                raise ValueError(
                    f"agents.{agent_id}: sandbox {agent.sandbox or defaults[0]} lacks security groups {missing} "
                    "the agent requires; add them to the sandbox or opt the agent into another sandbox"
                )
        if self.onboarding_order and sorted(self.onboarding_order) != sorted(self.sandboxes):
            raise ValueError("onboarding_order must list every declared sandbox exactly once")
        return self

    # ---- derived views -------------------------------------------------------------------

    @property
    def default_sandbox(self) -> str:
        return next(n for n, s in self.sandboxes.items() if s.default)

    def placement(self) -> dict[str, str]:
        """agent id → sandbox name (explicit ``sandbox:`` wins, otherwise the default sandbox)."""
        return {a: (s.sandbox or self.default_sandbox) for a, s in self.agents.items()}

    def sandbox_for(self, agent_id: str) -> str:
        try:
            return self.placement()[agent_id]
        except KeyError:
            raise ConfigError(f"unknown agent {agent_id!r}") from None

    def sandbox_agents(self, sandbox: str) -> list[str]:
        """Non-fixed agents placed in the sandbox (its secondaries), sorted."""
        return sorted(a for a, s in self.placement().items() if s == sandbox and self.agents[a].kind != "fixed")

    def main_agent(self, sandbox: str) -> str | None:
        """The fixed agent that owns the sandbox's OpenClaw ``main`` slot, else None (routing head)."""
        return next((a for a, s in self.agents.items() if s.kind == "fixed" and s.sandbox == sandbox), None)

    def active_sandboxes(self) -> list[str]:
        """Sandboxes that must exist: the default one plus any sandbox an agent opted into."""
        used = set(self.placement().values()) | {self.default_sandbox}
        return [s for s in self.ordered_sandboxes() if s in used]

    def sandbox_presets(self, sandbox: str, *, fallback: bool = False) -> list[str]:
        names: list[str] = []
        for group in self.sandboxes[sandbox].groups:
            sg = self.security_groups[group]
            for preset in sg.presets + (sg.fallback_presets if fallback else []):
                if preset not in names:
                    names.append(preset)
        return names

    def sandbox_mcp_servers(self, sandbox: str) -> list[str]:
        names: list[str] = []
        for group in self.sandboxes[sandbox].groups:
            for server in self.security_groups[group].mcp_servers:
                if server not in names:
                    names.append(server)
        return names

    def sandbox_privilege(self, sandbox: str) -> int:
        return max(self.security_groups[g].privilege for g in self.sandboxes[sandbox].groups)

    def ordered_sandboxes(self) -> list[str]:
        if self.onboarding_order:
            return list(self.onboarding_order)
        rest = sorted((n for n in self.sandboxes if n != self.default_sandbox),
                      key=lambda n: (self.sandbox_privilege(n), n))
        return [self.default_sandbox, *rest]


# --------------------------------------------------------------------------- teams (roles.yaml / teams.yaml)


class RoleSpec(Strict):
    capability: str
    groups: list[str] = []
    alias: str
    skill: str
    tools: ToolPolicy
    description: str = ""
    keywords: list[str] = []


class SupervisorTemplate(Strict):
    skill: str = "team-supervisor"
    tools: ToolPolicy


class RolesConfig(Strict):
    version: int = 1
    max_members: int = Field(default=4, ge=1, le=5)
    capabilities: dict[str, str]
    roles: dict[str, RoleSpec]
    supervisor: SupervisorTemplate
    always: list[str] = []
    excludes: dict[str, list[str]] = {}

    @model_validator(mode="after")
    def _consistent(self) -> RolesConfig:
        for name, role in self.roles.items():
            if not AGENT_ID.match(name):
                raise ValueError(f"roles.{name}: invalid role id")
            if role.capability not in self.capabilities:
                raise ValueError(f"roles.{name}: unknown capability {role.capability!r}")
        for cap in self.always:
            if cap not in self.capabilities:
                raise ValueError(f"always: unknown capability {cap!r}")
        for cap, excluded in self.excludes.items():
            if cap not in self.capabilities or any(e not in self.capabilities for e in excluded):
                raise ValueError(f"excludes.{cap}: unknown capability")
        return self

    def role_for(self, capability: str) -> str | None:
        return next((n for n, r in self.roles.items() if r.capability == capability), None)


class TeamTask(Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    name: str
    keywords: list[str] = []
    kb_domain: str | None = None     # fake-agent mode: KB seed domain to answer from (real teams use the facade)


class TeamMember(Strict):
    agent_id: str
    role: str


class TeamPattern(Strict):
    capabilities: list[str] = []
    roles: list[str] = []
    source: Literal["direct", "keywords", "fallback", "manual"] = "manual"
    reason: str = ""


class TeamDecl(Strict):
    team_id: str = Field(pattern=r"^t-[a-z][a-z0-9_-]{0,31}$")
    task: TeamTask
    description: str = ""
    sandbox: str | None = None
    supervisor: str
    members: list[TeamMember] = Field(min_length=1)
    pattern: TeamPattern = TeamPattern()
    status: Literal["ready", "applying", "failed", "declared"] = "declared"
    created_at: str = ""
    error: str | None = None


class TeamsFile(Strict):
    version: int = 1
    teams: list[TeamDecl] = []

    @model_validator(mode="after")
    def _unique(self) -> TeamsFile:
        ids = [t.team_id for t in self.teams]
        tasks = [t.task.id for t in self.teams]
        if len(ids) != len(set(ids)) or len(tasks) != len(set(tasks)):
            raise ValueError("teams: team_id and task.id must be unique")
        return self


def team_agents(teams: TeamsFile, roles: RolesConfig, routing_exposure: dict[str, int] | None = None) -> dict[str, dict]:
    """Agent declarations (AgentSpec dicts) for every team: one delegatable supervisor whose spawn
    allowlist is its members, and non-delegatable members with the role's fixed egress/alias/skill."""
    out: dict[str, dict] = {}
    for team in teams.teams:
        member_aliases = []
        for member in team.members:
            role = roles.roles[member.role]
            member_aliases.append(role.alias)
            out[member.agent_id] = {
                "kind": "task", "sandbox": team.sandbox, "groups": list(role.groups), "alias": role.alias,
                "skill": role.skill, "description": f"[{team.task.name}] {member.role}: {role.description}",
                "tools": role.tools.model_dump(exclude_defaults=True), "delegatable": False, "team": team.team_id,
            }
        exposure = routing_exposure or {}
        alias = min(member_aliases, key=lambda a: (exposure.get(a, 0), a)) if member_aliases else "rfa-internal"
        out[team.supervisor] = {
            "kind": "task", "sandbox": team.sandbox, "groups": [], "alias": alias, "skill": roles.supervisor.skill,
            "description": f"[{team.task.name}] task 대표(supervisor): 멤버 {', '.join(m.role for m in team.members)} 를 부려 답하고 verifier 로 검증한다.",
            "tools": roles.supervisor.tools.model_dump(exclude_defaults=True), "delegatable": True, "team": team.team_id,
            "allow_agents": [m.agent_id for m in team.members],
        }
    return out


# --------------------------------------------------------------------------- routing


class ProxyConfig(Strict):
    bind: str
    route_url: str
    credential_env: str = "COMPATIBLE_API_KEY"
    key_file: str = ".local/sg/proxy-api.key"
    marker_key_file: str = ".local/sg/marker.key"
    default_mode: str = AUTO_MODE
    unattributed_alias: str


class Backend(Strict):
    kind: Literal["openai", "ollama"] = "openai"
    url: str
    auth: Literal["none", "bearer"] = "bearer"
    credential_env: str | None = None
    env_file: str | None = None
    think: bool = False          # ollama: disable the model's reasoning channel (JSON/tool answers)
    num_ctx: int | None = None   # ollama: context window override (OpenClaw prompts are long)
    chat_template_kwargs: dict[str, object] | None = None  # openai-kind extras (e.g. NVIDIA enable_thinking=false)

    @model_validator(mode="after")
    def _auth(self) -> Backend:
        if self.auth == "bearer" and not self.credential_env:
            raise ValueError("bearer backends need credential_env")
        return self


class Alias(Strict):
    backend: str
    model: str
    censor: str
    exposure: int = Field(ge=0, le=10)


class Channel(Strict):
    alias: str
    profile: str


class EntryConfig(Strict):
    bind: str
    default_channel: str
    hosted_turn_timeout_seconds: int = 90
    assistant_sandbox: str
    assistant_agent: str = "main"


class BrokerConfig(Strict):
    bind: str
    path: str = "/mcp"
    tls_dir: str
    credential_env: str
    key_file: str
    task_turn_timeout_seconds: int = 120


class Routing(Strict):
    version: int = 1
    proxy: ProxyConfig
    backends: dict[str, Backend]
    aliases: dict[str, Alias]
    channels: dict[str, Channel]
    entry: EntryConfig
    broker: BrokerConfig

    @model_validator(mode="after")
    def _consistent(self) -> Routing:
        for name, alias in self.aliases.items():
            if alias.backend not in self.backends:
                raise ValueError(f"aliases.{name}: unknown backend {alias.backend!r}")
        for name, channel in self.channels.items():
            if channel.alias not in self.aliases:
                raise ValueError(f"channels.{name}: unknown alias {channel.alias!r}")
        if self.proxy.unattributed_alias not in self.aliases:
            raise ValueError("proxy.unattributed_alias must be a declared alias")
        if self.proxy.default_mode != AUTO_MODE and self.proxy.default_mode not in self.aliases:
            raise ValueError("proxy.default_mode must be rfa-auto or a declared alias")
        if self.entry.default_channel not in self.channels:
            raise ValueError("entry.default_channel must be a declared channel")
        bypass = [n for n, a in self.aliases.items() if a.censor == "bypass"]
        if len(bypass) != 1:
            raise ValueError("exactly one alias must use censor: bypass (the censor LLM route)")
        return self

    @property
    def bypass_alias(self) -> str:
        return next(n for n, a in self.aliases.items() if a.censor == "bypass")

    def resolve_alias(
        self, mode: str, channel_alias: str | None, agent_alias: str | None
    ) -> tuple[str, str]:
        """Return (alias, reason). Bypass wins (censor recursion guard); a non-auto mode wins next;
        otherwise the least-exposed of the verified channel/agent aliases, else the safe default."""
        if agent_alias == self.bypass_alias:
            return agent_alias, "censor-bypass"
        if mode != AUTO_MODE:
            if mode not in self.aliases:
                return self.proxy.unattributed_alias, "unknown-mode"
            return mode, f"mode:{mode}"
        candidates = [a for a in (channel_alias, agent_alias) if a and a in self.aliases]
        if not candidates:
            return self.proxy.unattributed_alias, "unattributed"
        chosen = min(candidates, key=lambda a: (self.aliases[a].exposure, a))
        return chosen, "min-exposure"


# --------------------------------------------------------------------------- censors


class RegexRule(Strict):
    id: str
    pattern: str | None = None
    keywords: list[str] = []
    replacement: str
    action: Literal["redact", "block"] = "redact"

    @model_validator(mode="after")
    def _one_of(self) -> RegexRule:
        if bool(self.pattern) == bool(self.keywords):
            raise ValueError(f"rule {self.id}: declare exactly one of pattern/keywords")
        if self.pattern:
            re.compile(self.pattern)
        return self


class RegexStage(Strict):
    id: str
    type: Literal["regex"]
    rules: list[RegexRule]


class LlmStage(Strict):
    id: str
    type: Literal["llm"]
    runner: Literal["sandbox-agent", "direct"] = "sandbox-agent"
    sandbox: str
    agent: str = "main"
    alias: str
    timeout_seconds: int = 45
    skip_if_shorter_than: int = 0
    categories: list[str]
    on_error: Literal["block", "allow"] = "block"


Stage = Annotated[RegexStage | LlmStage, Field(discriminator="type")]


class Profile(Strict):
    stages: list[Stage] = []


class Censors(Strict):
    version: int = 1
    default_action: Literal["redact", "block"] = "redact"
    fail_closed: bool = True
    profiles: dict[str, Profile]

    @model_validator(mode="after")
    def _has_none(self) -> Censors:
        if "none" not in self.profiles:
            raise ValueError("profiles.none is required (no-op profile)")
        return self


# --------------------------------------------------------------------------- ask


class AskAuth(Strict):
    credential_env: str = "RFA_ASK_TOKEN"
    env_file: str = ".env.dev"


class AudienceSpec(Strict):
    profile: str
    channel: str


class ServerConfig(Strict):
    """Synchronous ``/ask``: one server-side timeout and the ``request_id`` result cache TTL."""

    timeout_seconds: int = Field(default=180, ge=1)
    result_ttl_seconds: int = Field(default=3600, ge=1)


class LineageConfig(Strict):
    max_rejections: int = Field(default=3, ge=1)


class HeadConfig(Strict):
    runner: Literal["direct", "fake"] = "direct"
    timeout_seconds: int = Field(default=60, ge=1)
    fallback: Literal["keywords", "none"] = "keywords"


class TaskSpec(Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    name: str
    agent: str
    keywords: list[str] = []


class AskConfig(Strict):
    version: int = 1
    auth: AskAuth = AskAuth()
    audiences: dict[str, AudienceSpec]
    server: ServerConfig = ServerConfig()
    lineage: LineageConfig = LineageConfig()
    head: HeadConfig = HeadConfig()
    tasks: list[TaskSpec] = Field(min_length=1)
    learned_rules_file: str = "deploy/nemoclaw/censor-rules/learned.yaml"

    @model_validator(mode="after")
    def _consistent(self) -> AskConfig:
        for required in ("public", "company", "self"):
            if required not in self.audiences:
                raise ValueError(f"audiences.{required} is required")
        ids = [t.id for t in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("tasks ids must be unique")
        return self

    def task(self, task_id: str | None) -> TaskSpec | None:
        return next((t for t in self.tasks if t.id == task_id), None)


# --------------------------------------------------------------------------- loading


def _load_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"{path}: not found") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def _build(model: type[Strict], path: Path):
    try:
        return model.model_validate(_load_yaml(path))
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first.get("loc", ()))
        raise ConfigError(f"{path}: {loc or 'root'}: {first.get('msg')}") from None


def load_roles(path: Path | None = None) -> RolesConfig:
    return _build(RolesConfig, path or DEPLOY_DIR / "roles.yaml")


def load_teams(path: Path | None = None) -> TeamsFile:
    path = path or DEPLOY_DIR / "teams.yaml"
    if not path.exists():
        return TeamsFile()
    return _build(TeamsFile, path)


def load_assignments(path: Path | None = None, teams_path: Path | None = None,
                     roles_path: Path | None = None) -> Assignments:
    """assignments.yaml plus the agents of every declared team (teams.yaml), validated together.
    ``teams_path=Path("/dev/null")`` (or any missing file) loads the static declaration only."""
    data = _load_yaml(path or DEPLOY_DIR / "assignments.yaml")
    teams = load_teams(teams_path)
    if teams.teams:
        roles = load_roles(roles_path)
        try:  # supervisor alias = least-exposed member alias; exposure comes from routing.yaml
            exposure = {n: a.exposure for n, a in load_routing().aliases.items()}
        except ConfigError:
            exposure = {}
        agents = dict(data.get("agents") or {})
        for agent_id, spec in team_agents(teams, roles, exposure).items():
            if agent_id in agents:
                raise ConfigError(f"teams.yaml: agent {agent_id} collides with assignments.yaml")
            agents[agent_id] = spec
        data = {**data, "agents": agents}
    try:
        return Assignments.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first.get("loc", ()))
        raise ConfigError(f"assignments(+teams): {loc or 'root'}: {first.get('msg')}") from None


def load_routing(path: Path | None = None) -> Routing:
    return _build(Routing, path or DEPLOY_DIR / "routing.yaml")


def load_censors(path: Path | None = None) -> Censors:
    return _build(Censors, path or DEPLOY_DIR / "censors.yaml")


def load_ask(path: Path | None = None) -> AskConfig:
    return _build(AskConfig, path or DEPLOY_DIR / "ask.yaml")


def cross_check(assignments: Assignments, routing: Routing, censors: Censors,
                ask: AskConfig | None = None, roles: RolesConfig | None = None) -> list[str]:
    """Problems that only show across files (alias/profile/sandbox/audience references)."""
    problems: list[str] = []
    if roles is not None:
        for name, role in roles.roles.items():
            if role.alias not in routing.aliases:
                problems.append(f"roles.{name}.alias {role.alias!r} is not in routing.aliases")
            for group in role.groups:
                if group not in assignments.security_groups:
                    problems.append(f"roles.{name}.groups: unknown security group {group!r}")
    for agent_id, agent in assignments.agents.items():
        for target in agent.allow_agents:
            if target not in assignments.agents:
                problems.append(f"agents.{agent_id}.allow_agents: unknown agent {target!r}")
            elif assignments.sandbox_for(target) != assignments.sandbox_for(agent_id):
                problems.append(f"agents.{agent_id}.allow_agents: {target!r} is in another sandbox (sessions_spawn is same-sandbox)")
    if ask is not None:
        for name, spec in ask.audiences.items():
            if spec.profile not in censors.profiles:
                problems.append(f"ask.audiences.{name}.profile {spec.profile!r} is not in censors.profiles")
            if spec.channel not in routing.channels:
                problems.append(f"ask.audiences.{name}.channel {spec.channel!r} is not in routing.channels")
        for task in ask.tasks:
            agent = assignments.agents.get(task.agent)
            if agent is None or agent.kind != "task":
                problems.append(f"ask.tasks.{task.id}.agent {task.agent!r} is not a task agent")
    for agent_id, agent in assignments.agents.items():
        if agent.alias not in routing.aliases:
            problems.append(f"agents.{agent_id}.alias {agent.alias!r} is not in routing.aliases")
    for name, alias in routing.aliases.items():
        if alias.censor not in censors.profiles:
            problems.append(f"aliases.{name}.censor {alias.censor!r} is not in censors.profiles")
    for name, channel in routing.channels.items():
        if channel.profile not in censors.profiles:
            problems.append(f"channels.{name}.profile {channel.profile!r} is not in censors.profiles")
    for profile_name, profile in censors.profiles.items():
        for stage in profile.stages:
            if isinstance(stage, LlmStage):
                if stage.sandbox not in assignments.sandboxes:
                    problems.append(f"profiles.{profile_name}.{stage.id}: unknown sandbox")
                elif stage.agent != "main" and (
                    stage.agent not in assignments.agents
                    or assignments.sandbox_for(stage.agent) != stage.sandbox
                ):
                    problems.append(f"profiles.{profile_name}.{stage.id}: agent {stage.agent!r} is not placed in {stage.sandbox}")
                if stage.alias not in routing.aliases:
                    problems.append(f"profiles.{profile_name}.{stage.id}: unknown alias")
    if routing.entry.assistant_sandbox not in assignments.sandboxes:
        problems.append("routing.entry.assistant_sandbox is not a declared sandbox")
    return problems
