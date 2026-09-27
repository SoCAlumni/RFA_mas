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
    fixed: bool = False

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
    sandbox: str | None = None
    groups: list[str] = []
    alias: str
    skill: str
    description: str = ""
    tools: ToolPolicy

    @property
    def group_key(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.groups)))


class Assignments(Strict):
    version: int = 1
    host: HostConfig
    baseline_excludes: list[str] = []
    security_groups: dict[str, SecurityGroup]
    sandboxes: dict[str, SandboxSpec]
    onboarding_order: list[str] = []
    agents: dict[str, AgentSpec]

    @model_validator(mode="after")
    def _consistent(self) -> Assignments:
        for name, sandbox in self.sandboxes.items():
            if not SANDBOX_NAME.match(name):
                raise ValueError(f"sandboxes.{name}: invalid sandbox name")
            for group in sandbox.groups:
                if group not in self.security_groups:
                    raise ValueError(f"sandboxes.{name}: unknown security group {group!r}")
        keys: dict[tuple[str, ...], str] = {}
        for name, sandbox in self.sandboxes.items():
            if sandbox.fixed:
                continue
            if sandbox.group_key in keys:
                raise ValueError(
                    f"sandboxes.{name}: same group set as {keys[sandbox.group_key]!r}; "
                    "a security-group combination maps to exactly one task sandbox"
                )
            keys[sandbox.group_key] = name
        fixed_owner: dict[str, str] = {}
        for agent_id, agent in self.agents.items():
            if not AGENT_ID.match(agent_id) or agent_id == "main":
                raise ValueError(f"agents.{agent_id}: invalid agent id (lowercase, not 'main')")
            if agent.kind == "fixed":
                if agent.sandbox is None or agent.groups:
                    raise ValueError(f"agents.{agent_id}: fixed agents declare sandbox, not groups")
                sandbox = self.sandboxes.get(agent.sandbox)
                if sandbox is None or not sandbox.fixed:
                    raise ValueError(f"agents.{agent_id}: sandbox must be a fixed sandbox")
                if agent.sandbox in fixed_owner:
                    raise ValueError(
                        f"agents.{agent_id}: fixed sandbox {agent.sandbox} already owned by "
                        f"{fixed_owner[agent.sandbox]}"
                    )
                fixed_owner[agent.sandbox] = agent_id
            else:
                if agent.sandbox is not None or not agent.groups:
                    raise ValueError(f"agents.{agent_id}: task agents declare groups, not sandbox")
                for group in agent.groups:
                    if group not in self.security_groups:
                        raise ValueError(f"agents.{agent_id}: unknown security group {group!r}")
                if agent.group_key not in keys:
                    raise ValueError(
                        f"agents.{agent_id}: no task sandbox declares groups "
                        f"{list(agent.group_key)}; add one under sandboxes"
                    )
        for name, sandbox in self.sandboxes.items():
            if sandbox.fixed and name not in fixed_owner:
                raise ValueError(f"sandboxes.{name}: fixed sandbox has no fixed agent")
        if self.onboarding_order:
            if sorted(self.onboarding_order) != sorted(self.sandboxes):
                raise ValueError("onboarding_order must list every sandbox exactly once")
        return self

    # ---- derived views -------------------------------------------------------------------

    def placement(self) -> dict[str, str]:
        """agent id → sandbox name (fixed agents explicit, task agents by group-set match)."""
        by_key = {s.group_key: n for n, s in self.sandboxes.items() if not s.fixed}
        out: dict[str, str] = {}
        for agent_id, agent in self.agents.items():
            out[agent_id] = agent.sandbox if agent.kind == "fixed" else by_key[agent.group_key]
        return out

    def sandbox_for(self, agent_id: str) -> str:
        try:
            return self.placement()[agent_id]
        except KeyError:
            raise ConfigError(f"unknown agent {agent_id!r}") from None

    def sandbox_agents(self, sandbox: str) -> list[str]:
        return sorted(a for a, s in self.placement().items() if s == sandbox)

    def main_agent(self, sandbox: str) -> str | None:
        """The fixed agent that owns a fixed sandbox (its OpenClaw ``main``), else None."""
        spec = self.sandboxes[sandbox]
        if not spec.fixed:
            return None
        return next(a for a, s in self.agents.items() if s.kind == "fixed" and s.sandbox == sandbox)

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
        return sorted(self.sandboxes, key=lambda n: (self.sandbox_privilege(n), n))


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
    kind: Literal["openai"] = "openai"
    url: str
    auth: Literal["none", "bearer"] = "bearer"
    credential_env: str | None = None
    env_file: str | None = None

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


def load_assignments(path: Path | None = None) -> Assignments:
    return _build(Assignments, path or DEPLOY_DIR / "assignments.yaml")


def load_routing(path: Path | None = None) -> Routing:
    return _build(Routing, path or DEPLOY_DIR / "routing.yaml")


def load_censors(path: Path | None = None) -> Censors:
    return _build(Censors, path or DEPLOY_DIR / "censors.yaml")


def cross_check(assignments: Assignments, routing: Routing, censors: Censors) -> list[str]:
    """Problems that only show across files (alias/profile/sandbox references)."""
    problems: list[str] = []
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
                if stage.alias not in routing.aliases:
                    problems.append(f"profiles.{profile_name}.{stage.id}: unknown alias")
    if routing.entry.assistant_sandbox not in assignments.sandboxes:
        problems.append("routing.entry.assistant_sandbox is not a declared sandbox")
    return problems
