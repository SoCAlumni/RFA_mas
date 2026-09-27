"""Security-group controller: observe live sandboxes, diff against assignments.yaml, apply.

Only NemoClaw CLI verbs are used (``policy add --from-file``, ``policy remove``,
``policy exclude``, ``agents apply``, ``mcp add/remove``, ``policy explain --write``);
``openshell policy set`` is never issued. Plans are pure data so tests can assert them.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from rfa_mas.nemoclaw.config import DEPLOY_DIR, Assignments, ConfigError
from rfa_mas.nemoclaw.manifests import manifest_agent_ids, render_manifest
from rfa_mas.nemoclaw.runner import CommandResult, Runner, extract_json, strip_warnings

CUSTOM_PREFIX = "nemoclaw_custom__"
PROTECTED_KEYS = {"managed_inference", "openclaw_gateway_dialback"}
STATIC_SECTIONS = ("filesystem_policy", "landlock", "process")


@dataclass
class Observed:
    sandboxes: dict[str, dict] = field(default_factory=dict)
    policies: dict[str, dict] = field(default_factory=dict)
    agents: dict[str, list[str]] = field(default_factory=dict)
    mcp: dict[str, set[str]] = field(default_factory=dict)

    def exists(self, sandbox: str) -> bool:
        return sandbox in self.sandboxes


@dataclass
class Action:
    kind: str
    sandbox: str
    argv: list[str]
    reason: str
    env_keys: list[str] = field(default_factory=list)
    timeout: float = 300

    def display(self) -> str:
        return f"[{self.kind}] {self.sandbox}: {' '.join(self.argv)}  # {self.reason}"


class Observer:
    """Reads live state through the NemoClaw CLI (no local shadow registry)."""

    def __init__(self, runner: Runner, nemoclaw_bin: str = "nemoclaw"):
        self.runner = runner
        self.bin = nemoclaw_bin

    def list_sandboxes(self) -> dict[str, dict]:
        result = self.runner.run([self.bin, "list", "--json"], timeout=120, check=True)
        data = extract_json(result.stdout)
        return {entry["name"]: entry for entry in data.get("sandboxes", [])}

    def policy_get(self, sandbox: str) -> dict:
        result = self.runner.run([self.bin, sandbox, "policy", "get"], timeout=120, check=True)
        data = yaml.safe_load(strip_warnings(result.stdout))
        if not isinstance(data, dict):
            raise ConfigError(f"{sandbox}: policy get returned no mapping")
        return data

    def agents_list(self, sandbox: str) -> list[str]:
        result = self.runner.run(
            [self.bin, sandbox, "agents", "list", "--json"], timeout=120, check=True
        )
        data = extract_json(result.stdout)
        entries = data if isinstance(data, list) else data.get("agents", [])
        return sorted(str(e["id"]) for e in entries)

    def mcp_list(self, sandbox: str) -> set[str]:
        result = self.runner.run([self.bin, sandbox, "mcp", "list", "--json"], timeout=120)
        if not result.ok:
            return set()
        try:
            data = extract_json(result.stdout)
        except ValueError:
            return set()
        entries = data if isinstance(data, list) else (
            data.get("bridges") or data.get("servers") or data.get("mcpServers") or [])
        if isinstance(entries, dict):
            return set(entries)
        names = set()
        for entry in entries:
            if isinstance(entry, dict):
                name = entry.get("name") or entry.get("server") or entry.get("id")
                if name:
                    names.add(str(name))
        return names

    def observe(self, sandboxes: Iterable[str]) -> Observed:
        observed = Observed(sandboxes=self.list_sandboxes())
        for sandbox in sandboxes:
            if not observed.exists(sandbox):
                continue
            observed.policies[sandbox] = self.policy_get(sandbox)
            observed.agents[sandbox] = self.agents_list(sandbox)
            observed.mcp[sandbox] = self.mcp_list(sandbox)
        return observed


# --------------------------------------------------------------------------- presets


def render_preset(name: str, lan_ip: str, out_dir: Path, presets_dir: Path | None = None) -> Path:
    """Substitute ``__HOST__`` and write the preset the CLI will read (no secrets inside)."""
    source = (presets_dir or DEPLOY_DIR / "presets") / f"{name}.yaml"
    if not source.exists():
        raise ConfigError(f"preset {name!r} not found at {source}")
    text = source.read_text(encoding="utf-8").replace("__HOST__", lan_ip)
    data = yaml.safe_load(text)
    if data.get("preset", {}).get("name") != name:
        raise ConfigError(f"preset file {source} declares a different preset.name")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def load_preset(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def custom_presets(policy: dict) -> set[str]:
    names = set()
    for key in (policy.get("network_policies") or {}):
        if key.startswith(CUSTOM_PREFIX):
            rest = key[len(CUSTOM_PREFIX) :]
            names.add(rest.split("__", 1)[0])
    return names


def _normalize_entry(entry: dict) -> dict:
    endpoints = []
    for ep in entry.get("endpoints") or []:
        endpoints.append(
            {
                "host": ep.get("host"),
                "port": ep.get("port"),
                "protocol": ep.get("protocol"),
                "rules": ep.get("rules") or [],
                "access": ep.get("access"),
            }
        )
    binaries = sorted(b.get("path") for b in (entry.get("binaries") or []) if isinstance(b, dict))
    return {"endpoints": endpoints, "binaries": binaries}


def preset_drift(preset: dict, policy: dict) -> bool:
    """True when a rendered preset differs from its live entries (ignoring NemoClaw's IP pins)."""
    name = preset["preset"]["name"]
    live = policy.get("network_policies") or {}
    for entry_name, entry in preset["network_policies"].items():
        live_entry = live.get(f"{CUSTOM_PREFIX}{name}__{entry_name}")
        if live_entry is None or _normalize_entry(entry) != _normalize_entry(live_entry):
            return True
    return False


def verify_baseline(policy: dict, baseline: dict) -> list[str]:
    """Differences in the creation-locked static sections between the live policy and the
    checked-in baseline reference (``deploy/nemoclaw/baseline/openclaw-sandbox.yaml``)."""
    problems = []
    for section in STATIC_SECTIONS:
        if policy.get(section) != baseline.get(section):
            problems.append(f"{section}: live={policy.get(section)!r} baseline={baseline.get(section)!r}")
    return problems


# --------------------------------------------------------------------------- planning


@dataclass
class PlanInputs:
    assignments: Assignments
    observed: Observed
    rendered_presets: dict[str, Path]
    manifests: dict[str, Path]
    nemoclaw_bin: str = "nemoclaw"
    mcp_url: str | None = None
    mcp_credential_env: str | None = None
    mcp_fallback: bool = False


def plan(inputs: PlanInputs) -> list[Action]:
    a, obs, nb = inputs.assignments, inputs.observed, inputs.nemoclaw_bin
    actions: list[Action] = []
    active = set(a.active_sandboxes())
    for sandbox in a.ordered_sandboxes():
        if not obs.exists(sandbox):
            if sandbox not in active:
                continue  # opt-in sandbox nobody uses yet: nothing to create
            actions.append(
                Action("onboard", sandbox, [nb, "onboard", "--name", sandbox, "--agents",
                                            str(inputs.manifests[sandbox]), "--non-interactive",
                                            "--yes-i-accept-third-party-software"],
                       "sandbox missing (bootstrap onboards it with NEMOCLAW_* env)",
                       env_keys=["COMPATIBLE_API_KEY"], timeout=1800)
            )
            continue
        changed = False
        policy = obs.policies.get(sandbox, {})
        live_presets = custom_presets(policy)
        desired = a.sandbox_presets(sandbox, fallback=inputs.mcp_fallback)
        for preset in desired:
            path = inputs.rendered_presets[preset]
            if preset not in live_presets or preset_drift(load_preset(path), policy):
                reason = "preset missing" if preset not in live_presets else "preset content drift"
                actions.append(Action("policy-add", sandbox,
                                      [nb, sandbox, "policy", "add", "--from-file", str(path),
                                       "--trusted-private-host", a.host.lan_ip, "--yes"], reason))
                changed = True
        for preset in sorted(live_presets):
            if preset.startswith("sg-") and preset not in desired:
                actions.append(Action("policy-remove", sandbox,
                                      [nb, sandbox, "policy", "remove", preset, "--yes"],
                                      "preset no longer assigned to this sandbox's groups"))
                changed = True
        live_keys = set(policy.get("network_policies") or {})
        for key in a.baseline_excludes:
            if key in PROTECTED_KEYS:
                continue
            if key in live_keys:
                actions.append(Action("policy-exclude", sandbox,
                                      [nb, sandbox, "policy", "exclude", key, "--force"],
                                      "baseline entry allows direct egress; removed on every sandbox"))
                changed = True
        desired_agents = manifest_agent_ids(render_manifest(a, sandbox))
        live_agents = [i for i in obs.agents.get(sandbox, []) if i != "main"]
        if sorted(live_agents) != desired_agents:
            actions.append(Action("agents-apply", sandbox,
                                  [nb, sandbox, "agents", "apply", "-f", str(inputs.manifests[sandbox]),
                                   "--yes", "--non-interactive"],
                                  f"roster {sorted(live_agents)} → {desired_agents}", timeout=600))
            changed = True
        if not inputs.mcp_fallback:
            desired_mcp = set(a.sandbox_mcp_servers(sandbox))
            live_mcp = obs.mcp.get(sandbox, set())
            for server in sorted(desired_mcp - live_mcp):
                if inputs.mcp_url and inputs.mcp_credential_env:
                    actions.append(Action("mcp-add", sandbox,
                                          [nb, sandbox, "mcp", "add", server, "--url", inputs.mcp_url,
                                           "--env", inputs.mcp_credential_env,
                                           "--trusted-private-host", a.host.lan_ip],
                                          "managed MCP server assigned by security group",
                                          env_keys=[inputs.mcp_credential_env], timeout=900))
                    changed = True
            for server in sorted(live_mcp - desired_mcp):
                actions.append(Action("mcp-remove", sandbox,
                                      [nb, sandbox, "mcp", "remove", server, "--force"],
                                      "managed MCP server not assigned"))
                changed = True
        if changed:
            actions.append(Action("policy-explain", sandbox,
                                  [nb, sandbox, "policy", "explain", "--write"],
                                  "refresh /sandbox/.openclaw/workspace/POLICY.md for the agent"))
    return actions


def apply(
    actions: list[Action],
    runner: Runner,
    secrets: Callable[[str], str | None] | None = None,
    *,
    skip_kinds: Iterable[str] = ("onboard",),
    on_result: Callable[[Action, CommandResult], None] | None = None,
) -> list[tuple[Action, CommandResult]]:
    """Run actions in order; stop at the first failure (state stays consistent per NemoClaw)."""
    results: list[tuple[Action, CommandResult]] = []
    skip = set(skip_kinds)
    for action in actions:
        if action.kind in skip:
            continue
        env = {}
        for key in action.env_keys:
            value = secrets(key) if secrets else None
            if value is None:
                raise ConfigError(f"{action.kind} {action.sandbox}: missing credential env {key}")
            env[key] = value
        result = runner.run(action.argv, timeout=action.timeout, env=env or None)
        results.append((action, result))
        if on_result:
            on_result(action, result)
        if not result.ok:
            break
    return results


def summarize_policy(policy: dict) -> dict:
    """Redacted view for status output: entry keys, hosts and rule counts only."""
    out = {}
    for key, entry in (policy.get("network_policies") or {}).items():
        hosts = []
        rules = 0
        for ep in entry.get("endpoints") or []:
            hosts.append(f"{ep.get('host')}:{ep.get('port')}")
            rules += len(ep.get("rules") or [])
        out[key] = {"hosts": hosts, "rules": rules, "binaries": len(entry.get("binaries") or [])}
    return copy.deepcopy(out)
