"""Security-group controller: config validation, placement, manifests, reconcile plans, markers."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
import yaml

from rfa_mas.nemoclaw import config as cfg
from rfa_mas.nemoclaw.controller import (
    Action,
    Observed,
    Observer,
    PlanInputs,
    apply,
    custom_presets,
    plan,
    preset_drift,
    render_preset,
    verify_baseline,
)
from rfa_mas.nemoclaw.manifests import (
    manifest_agent_ids,
    render_identity,
    render_manifest,
    write_manifests,
)
from rfa_mas.nemoclaw.markers import find_markers, make_marker, strip_markers
from rfa_mas.nemoclaw.runner import CommandResult

DEPLOY = cfg.DEPLOY_DIR
BASELINE = yaml.safe_load((DEPLOY / "baseline" / "openclaw-sandbox.yaml").read_text())


class FakeRunner:
    """Scripted CLI: answers by argv prefix, records every call, never touches a subprocess."""

    def __init__(self, answers: dict[tuple[str, ...], str] | None = None):
        self.answers = answers or {}
        self.calls: list[list[str]] = []
        self.env_seen: list[dict] = []
        self.fail: set[tuple[str, ...]] = set()

    def run(self, argv: Sequence[str], *, timeout: float = 300, env: Mapping[str, str] | None = None,
            input_text: str | None = None, check: bool = False) -> CommandResult:
        argv = list(argv)
        self.calls.append(argv)
        self.env_seen.append(dict(env or {}))
        key = tuple(argv)
        for prefix, out in self.answers.items():
            if key[: len(prefix)] == prefix:
                return CommandResult(argv, 0, out, "")
        for prefix in self.fail:
            if key[: len(prefix)] == prefix:
                return CommandResult(argv, 1, "", "scripted failure")
        return CommandResult(argv, 0, "", "")


def live_policy(presets: dict[str, dict] | None = None, excluded: set[str] = frozenset()) -> dict:
    policy = copy.deepcopy(BASELINE)
    policy["network_policies"] = {
        "nvidia": {"name": "nvidia", "endpoints": [{"host": "integrate.api.nvidia.com", "port": 443}]},
        "clawhub": {"name": "clawhub", "endpoints": [{"host": "clawhub.ai", "port": 443}]},
        "openclaw_api": {"name": "openclaw_api", "endpoints": []},
        "openclaw_docs": {"name": "openclaw_docs", "endpoints": []},
        "npm_registry": {"name": "npm_registry", "endpoints": []},
        "managed_inference": {"name": "managed_inference", "endpoints": [{"host": "inference.local", "port": 443}]},
        "openclaw_gateway_dialback": {"name": "openclaw_gateway_dialback", "endpoints": []},
    }
    for key in excluded:
        policy["network_policies"].pop(key, None)
    for name, preset in (presets or {}).items():
        for entry_name, entry in preset["network_policies"].items():
            live = copy.deepcopy(entry)
            for ep in live["endpoints"]:
                ep["allowed_ips"] = [ep["host"]]  # NemoClaw pins resolved addresses
            policy["network_policies"][f"nemoclaw_custom__{name}__{entry_name}"] = live
    return policy


@pytest.fixture
def assignments() -> cfg.Assignments:
    return cfg.load_assignments()


@pytest.fixture
def rendered(tmp_path, assignments) -> dict[str, Path]:
    names = {p for sb in assignments.sandboxes for p in assignments.sandbox_presets(sb, fallback=True)}
    return {n: render_preset(n, assignments.host.lan_ip, tmp_path / "rendered") for n in names}


# ----------------------------------------------------------------------------- config


def test_checked_in_configs_load_and_cross_check():
    a, r, c = cfg.load_assignments(), cfg.load_routing(), cfg.load_censors()
    assert cfg.cross_check(a, r, c) == []
    assert a.default_sandbox == "rfa-main"
    assert a.placement() == {a_id: "rfa-main" for a_id in ("assistant", "censor", "research", "benchmark", "summarizer")}
    assert a.ordered_sandboxes() == ["rfa-main", "rfa-tasks-none"]
    assert a.active_sandboxes() == ["rfa-main"]  # opt-in sandbox unused until an agent names it
    assert a.sandbox_agents("rfa-main") == ["benchmark", "censor", "research", "summarizer"]
    assert a.main_agent("rfa-main") == "assistant" and a.main_agent("rfa-tasks-none") is None
    assert not a.agents["censor"].delegatable
    assert a.sandbox_presets("rfa-main") == ["sg-intranet-ro"]
    assert a.sandbox_presets("rfa-main", fallback=True) == ["sg-control-plane", "sg-intranet-ro"]  # sandbox group order
    assert a.sandbox_mcp_servers("rfa-main") == ["broker"]
    assert a.sandbox_privilege("rfa-main") == 2 > a.sandbox_privilege("rfa-tasks-none") == 0


def test_agent_may_only_land_where_its_groups_are_provided(tmp_path):
    data = yaml.safe_load((DEPLOY / "assignments.yaml").read_text())
    data["agents"]["research"]["sandbox"] = "rfa-tasks-none"  # egress-none cannot serve intranet-ro
    path = tmp_path / "a.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True))
    with pytest.raises(cfg.ConfigError, match="lacks security groups"):
        cfg.load_assignments(path)


def test_opting_an_agent_into_a_sandbox_activates_it(tmp_path):
    data = yaml.safe_load((DEPLOY / "assignments.yaml").read_text())
    data["agents"]["summarizer"]["sandbox"] = "rfa-tasks-none"
    path = tmp_path / "a.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True))
    a = cfg.load_assignments(path)
    assert a.sandbox_for("summarizer") == "rfa-tasks-none" and a.active_sandboxes() == ["rfa-main", "rfa-tasks-none"]
    assert a.sandbox_agents("rfa-main") == ["benchmark", "censor", "research"]


def test_exactly_one_default_sandbox(tmp_path):
    data = yaml.safe_load((DEPLOY / "assignments.yaml").read_text())
    data["sandboxes"]["rfa-tasks-none"]["default"] = True
    path = tmp_path / "a.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True))
    with pytest.raises(cfg.ConfigError, match="exactly one sandbox"):
        cfg.load_assignments(path)


def test_routing_alias_resolution_prefers_least_exposure_and_bypass():
    r = cfg.load_routing()
    assert r.resolve_alias("rfa-auto", None, None) == ("rfa-internal", "unattributed")
    assert r.resolve_alias("rfa-auto", "rfa-external", "rfa-external") == ("rfa-external", "min-exposure")
    assert r.resolve_alias("rfa-auto", "rfa-internal", "rfa-external")[0] == "rfa-internal"
    assert r.resolve_alias("rfa-auto", "rfa-external", "rfa-internal")[0] == "rfa-internal"
    assert r.resolve_alias("rfa-internal", "rfa-external", "rfa-external") == ("rfa-internal", "mode:rfa-internal")
    assert r.resolve_alias("rfa-internal", None, "rfa-censor") == ("rfa-censor", "censor-bypass")
    assert r.resolve_alias("bogus", "rfa-external", None) == ("rfa-internal", "unknown-mode")


# ----------------------------------------------------------------------------- manifests


def test_manifest_rendering_is_deterministic_and_schema_shaped(tmp_path, assignments):
    main = render_manifest(assignments, "rfa-main")
    assert main["defaults"] == {"subagents": {"maxSpawnDepth": 1}}
    assert main["main"]["tools"]["profile"] == "minimal"  # the assistant owns the main slot
    assert main["main"]["subagents"]["allowAgents"] == ["benchmark", "research", "summarizer"]  # censor never
    assert main["main"]["subagents"]["requireAgentId"] is True
    assert manifest_agent_ids(main) == ["benchmark", "censor", "research", "summarizer"]
    for agent in main["agents"]:
        assert agent["model"].startswith("inference/rfa-")
        assert agent["tools"]["allow"]  # secondaries inherit no tools by default
        assert set(agent) <= {"id", "description", "model", "tools", "subagents"}
    empty = render_manifest(assignments, "rfa-tasks-none")  # opt-in sandbox nobody uses: head only
    assert "agents" not in empty and "allowAgents" not in empty["main"]["subagents"]
    first = write_manifests(assignments, tmp_path / "a")
    second = write_manifests(assignments, tmp_path / "b")
    for sandbox in first:
        assert first[sandbox].read_text() == second[sandbox].read_text()
        assert yaml.safe_load(first[sandbox].read_text()) == render_manifest(assignments, sandbox)


def test_identity_file_carries_a_verifiable_agent_marker(assignments):
    secret = b"x" * 64
    text = render_identity(assignments, "research", secret)
    markers = find_markers(text, secret)
    assert [m.fields for m in markers if m.verified] == [
        {"agent": "research", "alias": "rfa-external", "sandbox": "rfa-main"}
    ]
    assert not find_markers(text, b"y" * 64)[0].verified  # different key: tampered/unknown


# ----------------------------------------------------------------------------- markers


def test_markers_sign_verify_and_strip():
    secret = b"s" * 48
    marker = make_marker("channel", {"ch": "external", "sid": "sess_1"}, secret)
    text = f"{marker}\n사용자 질문입니다."
    found = find_markers(text, secret)
    assert found[0].verified and found[0].kind == "channel" and found[0].fields["ch"] == "external"
    forged = marker.replace("ch=external", "ch=internal")
    assert not find_markers(forged, secret)[0].verified
    assert strip_markers(text) == "사용자 질문입니다."
    with pytest.raises(ValueError):
        make_marker("channel", {"ch": "ext ernal"}, secret)


# ----------------------------------------------------------------------------- presets / baseline


def test_rendered_preset_substitutes_host_and_detects_drift(rendered, assignments):
    preset = yaml.safe_load(rendered["sg-intranet-ro"].read_text())
    endpoint = preset["network_policies"]["sg_intranet_ro"]["endpoints"][0]
    assert endpoint["host"] == assignments.host.lan_ip and "allowed_ips" not in endpoint
    policy = live_policy({"sg-intranet-ro": preset})
    assert custom_presets(policy) == {"sg-intranet-ro"}
    assert not preset_drift(preset, policy)
    changed = copy.deepcopy(preset)
    changed["network_policies"]["sg_intranet_ro"]["endpoints"][0]["rules"].append(
        {"allow": {"method": "DELETE", "path": "/tasks/*"}}
    )
    assert preset_drift(changed, policy)


def test_verify_baseline_flags_static_section_changes():
    assert verify_baseline(live_policy(), BASELINE) == []
    drifted = live_policy()
    drifted["filesystem_policy"]["read_write"].append("/etc")
    assert any(p.startswith("filesystem_policy") for p in verify_baseline(drifted, BASELINE))


# ----------------------------------------------------------------------------- reconcile plan


def _inputs(assignments, observed, rendered, tmp_path, **kw) -> PlanInputs:
    manifests = write_manifests(assignments, tmp_path / "manifests")
    return PlanInputs(assignments, observed, rendered, manifests, nemoclaw_bin="nemoclaw", **kw)


def test_plan_onboards_only_active_sandboxes(assignments, rendered, tmp_path):
    actions = plan(_inputs(assignments, Observed(), rendered, tmp_path))
    assert [a.sandbox for a in actions if a.kind == "onboard"] == ["rfa-main"]  # opt-in sandbox untouched
    assert all("--agents" in a.argv and "--non-interactive" in a.argv for a in actions)


def test_plan_converges_then_is_idempotent(assignments, rendered, tmp_path):
    intranet = yaml.safe_load(rendered["sg-intranet-ro"].read_text())
    observed = Observed(
        sandboxes={"rfa-main": {"name": "rfa-main"}},
        policies={"rfa-main": live_policy()},
        agents={"rfa-main": ["main", "research"]},
        mcp={"rfa-main": set()},
    )
    actions = plan(_inputs(assignments, observed, rendered, tmp_path,
                           mcp_url="https://192.168.123.191:8798/mcp",
                           mcp_credential_env="RFA_BROKER_MCP_TOKEN"))
    kinds = [(a.kind, a.sandbox) for a in actions]
    assert kinds.count(("policy-exclude", "rfa-main")) == 5
    assert ("policy-add", "rfa-main") in kinds and ("agents-apply", "rfa-main") in kinds
    assert ("mcp-add", "rfa-main") in kinds
    assert kinds[-1] == ("policy-explain", "rfa-main")
    assert not any(a.kind == "policy-exclude" and a.argv[4] in ("managed_inference", "openclaw_gateway_dialback")
                   for a in actions)
    for action in actions:
        assert "policy" not in action.argv or action.argv[2] != "set"  # never `openshell policy set`
    assert not any(a.sandbox == "rfa-tasks-none" for a in actions)

    converged = copy.deepcopy(observed)
    converged.policies["rfa-main"] = live_policy({"sg-intranet-ro": intranet}, excluded=set(assignments.baseline_excludes))
    converged.agents["rfa-main"] = ["main", "benchmark", "censor", "research", "summarizer"]
    converged.mcp["rfa-main"] = {"broker"}
    assert plan(_inputs(assignments, converged, rendered, tmp_path,
                        mcp_url="https://192.168.123.191:8798/mcp",
                        mcp_credential_env="RFA_BROKER_MCP_TOKEN")) == []


def test_plan_removes_unassigned_sg_presets_and_uses_fallback_when_mcp_unavailable(
    assignments, rendered, tmp_path
):
    control = yaml.safe_load(rendered["sg-control-plane"].read_text())
    intranet = yaml.safe_load(rendered["sg-intranet-ro"].read_text())
    excluded = set(assignments.baseline_excludes)
    observed = Observed(
        sandboxes={"rfa-main": {"name": "rfa-main"}, "rfa-tasks-none": {"name": "rfa-tasks-none"}},
        policies={
            "rfa-main": live_policy({"sg-intranet-ro": intranet}, excluded=excluded),
            "rfa-tasks-none": live_policy({"sg-intranet-ro": intranet}, excluded=excluded),  # stale grant on a live opt-in sandbox
        },
        agents={"rfa-main": ["main", "benchmark", "censor", "research", "summarizer"], "rfa-tasks-none": ["main"]},
        mcp={"rfa-main": set(), "rfa-tasks-none": set()},
    )
    actions = plan(_inputs(assignments, observed, rendered, tmp_path, mcp_fallback=True))
    removes = [a for a in actions if a.kind == "policy-remove"]
    assert [(a.sandbox, a.argv[4]) for a in removes] == [("rfa-tasks-none", "sg-intranet-ro")]
    adds = [a for a in actions if a.kind == "policy-add"]
    assert [(a.sandbox, Path(a.argv[5]).name) for a in adds] == [("rfa-main", "sg-control-plane.yaml")]
    assert not any(a.kind.startswith("mcp") for a in actions)
    assert not preset_drift(control, live_policy({"sg-control-plane": control}))


def test_apply_runs_in_order_passes_only_declared_env_and_stops_on_failure():
    runner = FakeRunner()
    runner.fail.add(("nemoclaw", "sb", "policy", "add"))
    actions = [
        Action("policy-exclude", "sb", ["nemoclaw", "sb", "policy", "exclude", "nvidia", "--force"], "x"),
        Action("mcp-add", "sb", ["nemoclaw", "sb", "mcp", "add", "broker"], "x", env_keys=["TOKEN"]),
        Action("policy-add", "sb", ["nemoclaw", "sb", "policy", "add", "--from-file", "p"], "x"),
        Action("policy-explain", "sb", ["nemoclaw", "sb", "policy", "explain", "--write"], "x"),
        Action("onboard", "sb", ["nemoclaw", "onboard"], "x"),
    ]
    results = apply(actions, runner, secrets=lambda key: {"TOKEN": "secret-value"}.get(key))
    assert [r.argv[3] for _, r in results] == ["exclude", "add", "add"]
    assert runner.env_seen[1] == {"TOKEN": "secret-value"} and runner.env_seen[0] == {}
    assert not results[-1][1].ok and len(runner.calls) == 3  # stopped before explain; onboard skipped
    with pytest.raises(cfg.ConfigError, match="missing credential env"):
        apply(actions[1:2], FakeRunner(), secrets=lambda key: None)


def test_observer_parses_cli_json_and_yaml(tmp_path):
    policy_yaml = yaml.safe_dump(live_policy())
    runner = FakeRunner({
        ("nemoclaw", "list", "--json"): "(node) Warning: x\n" + json.dumps(
            {"sandboxes": [{"name": "rfa-main", "policies": []}]}),
        ("nemoclaw", "rfa-main", "policy", "get"): policy_yaml,
        ("nemoclaw", "rfa-main", "agents", "list", "--json"): "✓ Active gateway set to 'nemoclaw'\n"
        + json.dumps([{"id": "main", "isDefault": True}]),
        ("nemoclaw", "rfa-main", "mcp", "list", "--json"): json.dumps({"servers": [{"name": "broker"}]}),
    })
    observed = Observer(runner).observe(["rfa-main", "rfa-tasks-none"])
    assert observed.exists("rfa-main") and not observed.exists("rfa-tasks-none")
    assert observed.agents["rfa-main"] == ["main"] and observed.mcp["rfa-main"] == {"broker"}
    assert "nvidia" in observed.policies["rfa-main"]["network_policies"]
