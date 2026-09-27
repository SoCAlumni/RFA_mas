"""관리 · 에이전트 / 샌드박스 API (D-16~D-19): lists from the declaration, live state from a (fake)
nemoclaw, numbers from the audit ledger, owner-only actions on task agents only, live LLM switch."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from rfa_mas.nemoclaw import audit
from rfa_mas.nemoclaw import config as cfg
from rfa_mas.nemoclaw.ask import build_fake_deps
from rfa_mas.nemoclaw.ask_api import AskService, create_ask_app
from rfa_mas.nemoclaw.runner import CommandResult
from rfa_mas.nemoclaw.services import build_frontend_services
from rfa_mas.nemoclaw.services.llm import LlmControl
from rfa_mas.nemoclaw.store import Store
from rfa_mas.nemoclaw.teams import KeywordPatterner, TeamService
from rfa_mas.nemoclaw.usage import context_breakdown, estimate_tokens, scale_to_usage

OWNER = {"Authorization": "Bearer tok"}


class FakeRunner:
    def __init__(self):
        self.calls: list[list[str]] = []

    def run(self, argv, *, timeout=300, env=None, input_text=None, check=False):
        self.calls.append(list(argv))
        out, err = "", ""
        if argv[1:] == ["list", "--json"]:
            out = json.dumps({"sandboxes": [{"name": "rfa-main", "model": "rfa-auto", "provider": "custom"}]})
        elif argv[2:4] == ["agents", "list"]:
            out = json.dumps([{"id": "main"}, {"id": "research"}, {"id": "censor"}, {"id": "t-jira-sup"}])
        elif "exec" in argv:
            out = "2\n" if "keep=$(ls -t" in argv[-1] else "5\n"
        return CommandResult(list(argv), 0, out, err)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RFA_SG_AUDIT_DB", str(tmp_path / "audit.db"))
    for key, value in (("RFA_LLM_PROVIDER", "nvidia"), ("GEMINI_MODEL", "gemini-3.5-flash-lite"),
                       ("NVIDIA_MODEL", "nvidia/nemotron-3.5-lightning-30b-a3b")):
        monkeypatch.setenv(key, value)  # restored after the test even though the switch rewrites them
    (tmp_path / ".env.dev").write_text("NVIDIA_API_KEY=nv\nGEMINI_API_KEY=gm\nRFA_LLM_PROVIDER=nvidia\n", encoding="utf-8")
    teams = TeamService(roles=cfg.load_roles(), routing=cfg.load_routing(), ask_cfg=cfg.load_ask(),
                        patterner=KeywordPatterner(), teams_path=tmp_path / "teams.yaml",
                        manifests_dir=tmp_path / "agents", fake=True, secret=b"s" * 48)
    deps = build_fake_deps(cfg.load_ask(), cfg.load_censors(), tmp_path / "learned.yaml")
    deps.catalog = teams.catalog_tasks
    service = AskService(deps, "tok")
    routing = cfg.apply_llm_provider(cfg.load_routing(apply_env=False), tmp_path)
    keys = {"build": "nv"}
    runner = FakeRunner()
    frontend = build_frontend_services(assignments=teams.assignments, ask_deps=deps, token="tok",
                                       store=Store(tmp_path / "fe.db"), ask_service=service, teams=teams,
                                       llm=LlmControl(routing, keys, tmp_path), runner=runner, secret=b"s" * 48,
                                       routing=routing)
    frontend.admin._sources_cache = (time.time(), [{"id": "triv3", "title": "TRIV3 벤치마크", "description": ""},
                                                   {"id": "quantization_research", "title": "Quantization Research",
                                                    "description": ""}])
    return {"app": create_ask_app(service, teams, frontend=frontend), "svc": frontend, "runner": runner,
            "routing": routing, "keys": keys, "root": tmp_path}


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://entry")


async def add_task(c, name, **extra):
    async with c.stream("POST", "/tasks", json={"name": name, **extra}, headers=OWNER) as r:
        async for _ in r.aiter_lines():
            pass


async def test_agent_list_puts_task_agents_first_and_management_last_read_only(env):
    async with client(env["app"]) as c:
        await add_task(c, "Jira 이슈", description="Jira 문의에 사내 문서 근거를 찾아 답한다")
        body = (await c.get("/admin/agents")).json()
    ids = [a["id"] for a in body["agents"]]
    assert ids[0] == "research" and ids.index("t-jira-sup") < ids.index("assistant")
    assert ids[-2:] == ["assistant", "censor"]
    members = [a for a in body["agents"] if a["parentId"] == "t-jira-sup"]
    assert members and all(ids.index(m["id"]) == ids.index("t-jira-sup") + 1 + n for n, m in enumerate(members))
    research = body["agents"][0]
    assert [t["id"] for t in research["tasks"]] == ["triv3", "quantization_research"]
    assert research["subtitle"] == "TRIV3 벤치마크 (오로라) 태스크 · Quantization Research (네뷸라) 태스크"
    assert research["editable"] and research["sandboxLine"] == "rfa-main 샌드박스" and research["group"] == "task"
    sup = next(a for a in body["agents"] if a["id"] == "t-jira-sup")
    assert sup["role"] == "supervisor" and sup["initials"] == "JI" and sup["tasks"] == [{"id": "jira", "name": "Jira 이슈"}]
    async with client(env["app"]) as c:
        sup_detail = (await c.get("/admin/agents/t-jira-sup")).json()
    assert all(s["available"] and s["enabled"] for s in sup_detail["sources"])  # through its research member
    assistant = next(a for a in body["agents"] if a["id"] == "assistant")
    assert assistant["group"] == "management" and not assistant["editable"]
    assert assistant["readOnlyReason"] == "데모에서는 관리 에이전트를 수정할 수 없습니다." and assistant["initials"] == "통합"
    assert body["counts"]["total"] == len(ids) and body["counts"]["management"] == 2


async def test_live_state_marks_agents_missing_from_the_roster_as_stopped(env):
    env["svc"].admin.live._refresh()
    async with client(env["app"]) as c:
        agents = {a["id"]: a for a in (await c.get("/admin/agents")).json()["agents"]}
        boxes = (await c.get("/admin/sandboxes")).json()
    assert agents["research"]["status"] == "running" and agents["research"]["observed"]
    assert agents["benchmark"]["status"] == "stopped" and agents["assistant"]["status"] == "running"  # main slot
    status = {s["id"]: s["status"] for s in boxes["sandboxes"]}
    assert status == {"rfa-main": "running", "rfa-tasks-none": "stopped"}


async def test_agent_detail_has_context_sources_and_today_stats_from_the_ledger(env):
    ctx = {"systemPrompt": 1240, "toolDefinitions": 1020, "memoryNotes": 2310, "conversation": 1342, "total": 5912,
           "measured": True}
    for ms, verdict in ((2000, "allow"), (3800, "redact"), (2900, "block")):
        audit.record(kind="inference", verdict=verdict, action="completion", agent="research", sandbox="rfa-main",
                     detail={"ms": ms, "usage": {"total_tokens": 1000}, "context": ctx})
    audit.record(kind="inference", verdict="allow", action="completion", agent="benchmark", detail={"ms": 100})
    async with client(env["app"]) as c:
        d = (await c.get("/admin/agents/research")).json()
        listed = {a["id"]: a["callsToday"] for a in (await c.get("/admin/agents")).json()["agents"]}
    assert listed["research"] == 3 and listed["benchmark"] == 1
    assert d["context"]["usedTokens"] == 5912 and d["context"]["limitTokens"] == 32768 and d["context"]["measured"]
    assert d["context"]["breakdown"] == {k: ctx[k] for k in ("systemPrompt", "toolDefinitions", "memoryNotes", "conversation")}
    s = d["stats"]
    assert s["calls"] == 3 and s["tokens"] == 3000 and s["avgLatencySeconds"] == 2.9 and s["blockedCalls"] == 1
    assert len(s["last7Days"]) == 7 and s["last7Days"][-1]["calls"] == 3 and s["provider"] == "NVIDIA Endpoints"
    assert [x["id"] for x in d["sources"]] == ["triv3", "quantization_research"] and all(x["enabled"] for x in d["sources"])
    assert d["actions"]["compact"] and d["tools"]["profile"] == "coding"
    async with client(env["app"]) as c:
        none_yet = (await c.get("/admin/agents/summarizer")).json()
        missing = await c.get("/admin/agents/nope")
    assert none_yet["context"] is None and none_yet["stats"]["calls"] == 0
    assert none_yet["sources"][0]["available"] is False and "intranet-ro" in none_yet["sources"][0]["unavailableReason"]
    assert missing.status_code == 404


async def test_session_actions_are_owner_only_and_refused_for_management_agents(env):
    async with client(env["app"]) as c:
        guest = await c.post("/admin/agents/research/compact")
        mgmt = await c.post("/admin/agents/assistant/clear-memory", headers=OWNER)
        compact = (await c.post("/admin/agents/research/compact", headers=OWNER)).json()
        clear = (await c.post("/admin/agents/research/clear-memory", headers=OWNER)).json()
    assert guest.status_code == 403 and guest.json()["code"] == "owner_only"
    assert mgmt.status_code == 403 and mgmt.json() == {"code": "management_agent",
                                                       "message": "데모에서는 관리 에이전트를 수정할 수 없습니다."}
    assert compact["applied"] and compact["removedSessions"] == 2 and clear["removedSessions"] == 5
    execs = [c for c in env["runner"].calls if "exec" in c]
    assert execs[0][:2] == ["nemoclaw", "rfa-main"] and "/sandbox/.openclaw/agents/research/sessions" in execs[0][-1]
    assert "workspace-research/MEMORY.md" in execs[1][-1]


async def test_prompt_and_sources_are_written_into_identity(env):
    async with client(env["app"]) as c:
        view = (await c.get("/admin/agents/research/prompt")).json()
        saved = (await c.put("/admin/agents/research/prompt", json={"instructions": "수치는 항상 출처와 함께"},
                             headers=OWNER)).json()
        toggled = (await c.patch("/admin/agents/research/sources/quantization_research", json={"enabled": False},
                                 headers=OWNER)).json()
        blocked = await c.patch("/admin/agents/summarizer/sources/triv3", json={"enabled": True}, headers=OWNER)
        mgmt = await c.put("/admin/agents/censor/prompt", json={"instructions": "x"}, headers=OWNER)
    assert "rfa-agent" not in view["identity"] and "[서명된 라우팅 마커]" in view["identity"] and view["skillText"]
    assert saved["instructions"] == "수치는 항상 출처와 함께" and saved["applied"]
    assert [s["enabled"] for s in toggled["sources"]] == [True, False]
    assert blocked.status_code == 409 and mgmt.status_code == 403
    uploads = [c for c in env["runner"].calls if "upload" in c]
    assert uploads[-1][-1] == "/sandbox/.openclaw/workspace-research/IDENTITY.md"


async def test_sandbox_detail_and_limit(env):
    async with client(env["app"]) as c:
        boxes = (await c.get("/admin/sandboxes")).json()
        detail = (await c.get("/admin/sandboxes/rfa-main")).json()
        add = await c.post("/admin/sandboxes", headers=OWNER)
        guest_add = await c.post("/admin/sandboxes")
    assert [s["id"] for s in boxes["sandboxes"]] == ["rfa-main", "rfa-tasks-none"]
    main = boxes["sandboxes"][0]
    assert main["privilege"] == 2 and main["default"] and main["securityLabel"].startswith("L2 ")
    assert {t["id"] for t in main["tasks"]} == {"triv3", "quantization_research"} and main["gatewayPort"] == 18790
    assert boxes["limit"] == 2 and boxes["canAdd"] is False
    assert add.status_code == 409 and add.json()["message"].startswith("샌드박스는 많은 양의 메모리를 요구합니다")
    assert guest_add.status_code == 403
    opts = {o["provider"]: o for o in detail["inference"]["providerOptions"]}
    assert opts["gemini"]["models"] == ["gemini-3.5-flash-lite", "gemini-3.8-flash"] and opts["nvidia"]["selectable"]
    assert opts["ollama_local"]["selectable"] is False and opts["ollama_local"]["reason"] == "로컬 LLM을 확인할 수 없습니다."
    assert detail["gateway"]["currentRoute"] == "llm-api / nvidia/nemotron-3.5-lightning-30b-a3b"
    assert detail["gateway"]["sharedWith"] == ["rfa-tasks-none"] and detail["securityGroups"][0]["id"] == "control-plane"
    assert detail["inference"]["pendingRecreate"] == []


async def test_provider_switch_applies_live_and_lengths_wait_for_recreate(env):
    async with client(env["app"]) as c:
        guest = await c.patch("/admin/sandboxes/rfa-main", json={"provider": "gemini"})
        local = await c.patch("/admin/sandboxes/rfa-main", json={"provider": "ollama_local"}, headers=OWNER)
        bad = await c.patch("/admin/sandboxes/rfa-main", json={"provider": "gemini", "model": "gpt-9"}, headers=OWNER)
        ok = (await c.patch("/admin/sandboxes/rfa-main", json={"provider": "gemini", "model": "gemini-3.8-flash",
                                                              "contextLength": 16384}, headers=OWNER)).json()
    assert guest.status_code == 403 and local.status_code == 409 and bad.status_code == 422
    assert ok["applied"] == ["provider", "model"] and ok["requiresRecreate"] == ["contextLength"]
    routing = env["routing"]
    assert routing.provider == "gemini" and {a.model for a in routing.aliases.values()} == {"gemini-3.8-flash"}
    assert routing.backends["build"].url.startswith("https://generativelanguage") and env["keys"]["build"] == "gm"
    text = (env["root"] / ".env.dev").read_text(encoding="utf-8")
    assert "RFA_LLM_PROVIDER=gemini" in text and "GEMINI_MODEL=gemini-3.8-flash" in text and "NVIDIA_API_KEY=nv" in text
    assert ok["sandbox"]["inference"]["contextLength"] == 16384 and ok["sandbox"]["inference"]["model"] == "gemini-3.8-flash"


def test_proxy_context_breakdown_splits_the_request_and_scales_to_usage():
    body = {"messages": [{"role": "system", "content": "You are an agent.\n## Project Context\nIDENTITY 노트 내용"},
                         {"role": "user", "content": "안녕하세요"}, {"role": "assistant", "content": "hi there"}],
            "tools": [{"type": "function", "function": {"name": "read", "parameters": {}}}]}
    est = context_breakdown(body)
    assert est["systemPrompt"] == estimate_tokens("You are an agent.\n") and est["memoryNotes"] > 0
    assert est["toolDefinitions"] > 0 and est["conversation"] == estimate_tokens("안녕하세요") + estimate_tokens("hi there")
    scaled = scale_to_usage(est, {"prompt_tokens": 1000})
    assert scaled["total"] == 1000 and scaled["measured"] and sum(
        scaled[k] for k in ("systemPrompt", "toolDefinitions", "memoryNotes", "conversation")) == 1000
    assert scale_to_usage(est, {"prompt_tokens": 0}) == est
