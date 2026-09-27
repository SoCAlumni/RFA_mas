"""The four resident task teams in deploy/nemoclaw/teams.yaml: role choice, placement, and the task
spec that reaches each supervisor's IDENTITY.md."""

from __future__ import annotations

from rfa_mas.nemoclaw import config as cfg
from rfa_mas.nemoclaw.manifests import TASK_SPECS_DIR, render_identity

EXPECTED = {
    "t-training": ("ondevice-train", ["research", "benchmark", "summarizer", "verifier"]),
    "t-inference": ("infer-opt", ["research", "benchmark", "verifier"]),
    "t-automation": ("agent-ops", ["research", "verifier"]),
    "t-npu": ("npu-sdk", ["research", "summarizer", "verifier"]),
}


def test_declared_task_teams_use_catalogue_roles_and_always_verify():
    teams = {t.team_id: t for t in cfg.load_teams().teams}
    roles = cfg.load_roles()
    assert set(EXPECTED) <= set(teams)
    for team_id, (supervisor, role_names) in EXPECTED.items():
        team = teams[team_id]
        assert team.supervisor == supervisor and team.pattern.source == "manual"
        assert [m.role for m in team.members] == role_names == team.pattern.roles
        assert all(m.role in roles.roles for m in team.members) and len(team.members) <= roles.max_members
        assert all(m.agent_id == f"{team_id}-{m.role}" for m in team.members)


def test_task_teams_merge_into_the_default_sandbox_and_pass_cross_check():
    merged = cfg.load_assignments()
    for team_id, (supervisor, _) in EXPECTED.items():
        sup = merged.agents[supervisor]
        assert sup.delegatable and sup.team == team_id and merged.sandbox_for(supervisor) == "rfa-main"
        assert all(not merged.agents[m].delegatable for m in sup.allow_agents)
    # internal figures stay on the internal alias: no hosted-external member in the inference team
    inference = merged.agents["infer-opt"]
    assert all(merged.agents[m].alias == "rfa-internal" or m.endswith("-research") for m in inference.allow_agents)
    assert cfg.cross_check(merged, cfg.load_routing(), cfg.load_censors(), cfg.load_ask(), cfg.load_roles()) == []


def test_each_supervisor_identity_carries_its_task_spec(tmp_path):
    merged = cfg.load_assignments()
    for team_id, (supervisor, _) in EXPECTED.items():
        assert (TASK_SPECS_DIR / f"{team_id}.md").is_file()
        identity = render_identity(merged, supervisor, b"s" * 48)
        assert "# TEAM" in identity and "# TASK SPEC" in identity
        assert identity.index("# TEAM") < identity.index("# TASK SPEC")
        assert f"담당 `{supervisor}`" in identity
    # members get no spec; a team without a spec file renders without the section
    assert "# TASK SPEC" not in render_identity(merged, "t-npu-research", b"s" * 48)
    assert "# TASK SPEC" not in render_identity(merged, "npu-sdk", b"s" * 48, task_specs_dir=tmp_path)
