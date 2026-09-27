"""Team spawn: patterning from requirements, declaration merge + manifest, /teams API, /ask catalogue, apply path."""

from __future__ import annotations

import tempfile
from pathlib import Path

import httpx
import pytest
import yaml

from rfa_mas.nemoclaw import audit
from rfa_mas.nemoclaw import config as cfg
from rfa_mas.nemoclaw.ask import build_fake_deps
from rfa_mas.nemoclaw.ask_api import AskService, create_ask_app
from rfa_mas.nemoclaw.manifests import render_identity, render_manifest
from rfa_mas.nemoclaw.runner import CommandResult
from rfa_mas.nemoclaw.teams import DirectPatterner, KeywordPatterner, TeamService, compose, slug_for

H = {"Authorization": "Bearer tok"}
REQ = {"task_id": "aurora_dash", "name": "오로라 대시보드 문의 대응",
       "description": "오로라 벤치마크 수치와 사내 문서 근거를 찾아 파트너 질문에 답하고 요약 공지 초안을 만든다"}


@pytest.fixture(autouse=True)
def audit_db(tmp_path, monkeypatch):
    monkeypatch.setenv("RFA_SG_AUDIT_DB", str(tmp_path / "audit.db"))


def make_service(tmp_path, *, fake=True, runner=None, patterner=None) -> TeamService:
    return TeamService(roles=cfg.load_roles(), routing=cfg.load_routing(), ask_cfg=cfg.load_ask(),
                       patterner=patterner or KeywordPatterner(), teams_path=tmp_path / "teams.yaml",
                       manifests_dir=tmp_path / "agents", runner=runner, fake=fake, secret=b"s" * 48)


def make_app(tmp_path, service: TeamService):
    deps = build_fake_deps(cfg.load_ask(), cfg.load_censors(), tmp_path / "learned.yaml")
    deps.catalog = service.catalog_tasks
    return create_ask_app(AskService(deps, "tok"), service), deps


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://x")


# ---- patterning ----------------------------------------------------------------------------------


async def test_keyword_patterning_picks_roles_from_catalogue_and_always_adds_verifier():
    roles = cfg.load_roles()
    pattern = await KeywordPatterner().pattern(REQ["name"], REQ["description"], roles)
    assert set(pattern.capabilities) == {"intranet_evidence", "benchmark_numbers", "summarize"}
    decl, reasons = compose("aurora_dash", REQ["name"], REQ["description"], pattern, roles, None)
    assert decl is not None and reasons == []
    assert decl.pattern.roles == ["research", "benchmark", "summarizer", "verifier"]  # catalogue order, verifier always
    assert decl.supervisor == "t-aurora_dash-sup" and [m.agent_id for m in decl.members][-1] == "t-aurora_dash-verifier"
    assert "오로라" in decl.task.keywords and "벤치마크" in decl.task.keywords


async def test_no_egress_requirement_drops_intranet_roles_and_unroutable_requirement_is_refused():
    roles = cfg.load_roles()
    pattern = await KeywordPatterner().pattern("공지 정리", "받은 텍스트를 사내망 없이 요약만 한다", roles)
    assert "no_egress" in pattern.capabilities and "summarize" in pattern.capabilities
    decl, reasons = compose("notes", "공지 정리", "x", pattern, roles, None)
    assert decl is not None and decl.pattern.roles == ["summarizer", "verifier"] and "intranet_evidence" not in decl.pattern.capabilities
    none, reasons = compose("lunch", "점심", "메뉴 추천", await KeywordPatterner().pattern("점심", "메뉴 추천", roles), roles, None)
    assert none is None and "no role matches" in reasons[0]
    assert slug_for("오로라 대시보드", None).startswith("task-") and slug_for("Aurora Dash Q&A", None) == "aurora-dash-q-a"


async def test_direct_patterner_parses_json_and_falls_back(monkeypatch):
    answers = iter(['{"capabilities": ["benchmark_numbers", "made_up"], "keywords": ["오로라"], "reason": "r"}', "no json here"])

    class Resp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": next(answers)}}]}

    class FakeClient:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): ...
        async def post(self, url, json=None, headers=None):
            assert "⟦rfa-channel" in json["messages"][-1]["content"] and headers == {"Authorization": "Bearer pk"}
            return Resp()

    monkeypatch.setattr("rfa_mas.nemoclaw.teams.httpx.AsyncClient", FakeClient)
    p = DirectPatterner("http://proxy", "pk", b"s" * 48)
    roles = cfg.load_roles()
    first = await p.pattern("n", "d", roles)
    assert first.source == "direct" and first.capabilities == ["benchmark_numbers"] and first.keywords == ["오로라"]
    second = await p.pattern(REQ["name"], REQ["description"], roles)
    assert second.source == "fallback" and "summarize" in second.capabilities


# ---- declaration merge + manifest ----------------------------------------------------------------


async def test_team_declaration_merges_into_assignments_and_manifest_allows_only_members(tmp_path):
    service = make_service(tmp_path)
    status, payload = await service.create(name=REQ["name"], description=REQ["description"], task_id="aurora_dash", sandbox=None)
    assert status == 201 and payload["status"] == "ready" and payload["sandbox"] == "rfa-main"
    merged = service.assignments()
    sup, members = merged.agents["t-aurora_dash-sup"], payload["members"]
    assert sup.delegatable and sup.allow_agents == [m["agent_id"] for m in members] and sup.team == "t-aurora_dash"
    assert sup.alias == "rfa-internal"  # least exposure among members
    assert all(not merged.agents[m["agent_id"]].delegatable for m in members)
    assert merged.agents["t-aurora_dash-research"].groups == ["intranet-ro"]
    manifest = render_manifest(merged, "rfa-main")
    assert manifest["defaults"] == {"subagents": {"maxSpawnDepth": 2}}
    entries = {a["id"]: a for a in manifest["agents"]}
    assert entries["t-aurora_dash-sup"]["subagents"]["allowAgents"] == sup.allow_agents
    # coding profile narrowed by deny (a `minimal` profile would strip sessions_spawn after allow, sg-4g):
    # the supervisor keeps the sessions tools, members lose them
    sup_tools = entries["t-aurora_dash-sup"]["tools"]
    assert sup_tools["profile"] == "coding" and "group:sessions" not in sup_tools["deny"]
    assert {"group:runtime", "write", "bundle-mcp"} <= set(sup_tools["deny"])
    assert all("allowAgents" not in entries[m["agent_id"]]["subagents"]
               and "group:sessions" in entries[m["agent_id"]]["tools"]["deny"] for m in members)
    assert "t-aurora_dash-sup" in manifest["main"]["subagents"]["allowAgents"]
    assert not any(m["agent_id"] in manifest["main"]["subagents"]["allowAgents"] for m in members)
    identity = render_identity(merged, "t-aurora_dash-sup", b"s" * 48)
    assert "# TEAM" in identity and "t-aurora_dash-verifier" in identity and "sessions_spawn" in identity
    written = yaml.safe_load((tmp_path / "teams.yaml").read_text(encoding="utf-8"))
    assert written["teams"][0]["team_id"] == "t-aurora_dash" and written["teams"][0]["status"] == "ready"
    assert cfg.cross_check(merged, cfg.load_routing(), cfg.load_censors(), cfg.load_ask(), cfg.load_roles()) == []


async def test_invalid_placement_is_refused_before_persisting(tmp_path):
    service = make_service(tmp_path)
    status, payload = await service.create(name=REQ["name"], description=REQ["description"], task_id="iso", sandbox="rfa-tasks-none")
    assert status == 422 and payload["code"] == "invalid_team" and "lacks security groups" in payload["detail"]
    assert not (tmp_path / "teams.yaml").exists()
    status, payload = await service.create(name="공지 정리", description="받은 텍스트를 사내망 없이 요약", task_id="notes", sandbox=None)
    assert status == 201 and payload["sandbox"] == "rfa-tasks-none"  # no_egress → opt-in sandbox exists
    assert service.assignments().active_sandboxes() == ["rfa-main", "rfa-tasks-none"]


# ---- API + /ask catalogue ------------------------------------------------------------------------


async def test_teams_api_create_idempotent_conflict_list_get_delete_and_ask_routes_to_supervisor(tmp_path):
    service = make_service(tmp_path)
    app, deps = make_app(tmp_path, service)
    async with client(app) as c:
        assert (await c.post("/teams", json=REQ)).status_code == 401
        created = await c.post("/teams", json=REQ, headers=H)
        assert created.status_code == 201 and created.json()["pattern"]["source"] == "keywords"
        again = await c.post("/teams", json={**REQ, "description": "다른 설명"}, headers=H)
        assert again.status_code == 200 and again.json()["team_id"] == "t-aurora_dash"
        conflict = await c.post("/teams", json={"task_id": "triv3", "name": "x", "description": "벤치마크"}, headers=H)
        assert conflict.status_code == 409
        bad = await c.post("/teams", json={"name": "점심", "description": "메뉴 추천"}, headers=H)
        assert bad.status_code == 422 and bad.json()["code"] == "no_role_for_requirement"
        listed = (await c.get("/teams", headers=H)).json()["teams"]
        assert [t["team_id"] for t in listed] == ["t-aurora_dash"]
        assert (await c.get("/teams/t-aurora_dash", headers=H)).status_code == 200
        assert (await c.get("/teams/t-nope", headers=H)).status_code == 404
        asked = await c.post("/ask", json={"request_id": "a1", "question": "오로라 대시보드 문의 대응 방법",
                                           "channel": "github", "audience": "public"}, headers=H)
        assert asked.json()["task"]["id"] == "aurora_dash"
        assert deps.tasks.calls[-1][0] == "t-aurora_dash-sup"  # the supervisor is the task's agent
        removed = await c.delete("/teams/t-aurora_dash", headers=H)
        assert removed.status_code == 202 and removed.json()["status"] == "removed"
        assert (await c.get("/teams", headers=H)).json()["teams"] == []
        static = await c.post("/ask", json={"request_id": "a2", "question": "오로라 대시보드 문의 대응 방법",
                                            "channel": "github", "audience": "public"}, headers=H)
        assert static.json()["task"]["id"] == "triv3"
    kinds = [(e["action"], e["verdict"]) for e in audit.query(kind="team")]
    assert ("create", "ready") in kinds and ("remove", "removed") in kinds and ("create", "refused") in kinds


# ---- apply path ----------------------------------------------------------------------------------


async def test_real_apply_runs_agents_apply_then_seeds_and_records_failures(tmp_path):
    class Runner:
        def __init__(self, fail_apply=False):
            self.calls, self.fail_apply = [], fail_apply

        def run(self, argv, *, timeout=300, env=None, input_text=None, check=False):
            self.calls.append(list(argv))
            if argv[2:4] == ["agents", "apply"] and self.fail_apply:
                return CommandResult(list(argv), 1, "", "manifest rejected")
            return CommandResult(list(argv), 0, "ok", "")

    ok = Runner()
    service = make_service(tmp_path, fake=False, runner=ok)
    status, payload = await service.create(name=REQ["name"], description=REQ["description"], task_id="aurora_dash", sandbox=None)
    assert status == 201 and payload["applied"]["agents_apply"] == "ok" and payload["applied"]["seeded"] > 0
    apply_call = next(c for c in ok.calls if c[2:4] == ["agents", "apply"])
    assert apply_call[:2] == ["nemoclaw", "rfa-main"] and apply_call[-2:] == ["--yes", "--non-interactive"]
    assert any("IDENTITY-t-aurora_dash-sup.md" in " ".join(c) for c in ok.calls)  # supervisor identity seeded
    bad = Runner(fail_apply=True)
    service2 = make_service(tmp_path / "b", fake=False, runner=bad)
    status, payload = await service2.create(name=REQ["name"], description=REQ["description"], task_id="aurora_dash", sandbox=None)
    assert status == 201 and payload["status"] == "failed" and "manifest rejected" in payload["error"]
    assert service2.get("t-aurora_dash").status == "failed"  # kept for retry, roster unchanged


def test_checked_in_roles_and_teams_files_load():
    roles = cfg.load_roles()
    assert set(roles.roles) == {"research", "benchmark", "summarizer", "verifier"} and roles.always == ["verify"]
    teams = cfg.load_teams().teams   # resident task teams (tests/test_task_teams.py checks their contents)
    tmp = Path(tempfile.mkdtemp())
    static = cfg.load_assignments(teams_path=tmp / "missing.yaml").agents.keys()
    team_agents = {a for t in teams for a in [t.supervisor, *(m.agent_id for m in t.members)]}
    assert set(cfg.load_assignments().agents.keys()) == set(static) | team_agents
