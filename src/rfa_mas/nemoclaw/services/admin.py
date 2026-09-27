"""관리 · 「에이전트」·「샌드박스」 (D-16~D-19).

- Lists are the real declaration (``assignments.yaml`` + teams): every backend agent, task agents first
  and the management agents (assistant, censor) last and read-only (D-17); the declared sandboxes with
  their security groups (D-16).
- Live state (does the sandbox exist, which agents are on its roster) comes from ``nemoclaw`` and is
  refreshed in the background — the CLI can wait minutes on the host lock, so a request never waits for
  it (``observed=false`` until the first snapshot).
- Numbers come from the audit ledger: ``inference`` rows (one per LLM call, attributed to the agent by
  its signed marker) give calls, tokens, latency, blocked calls, the 7-day series and the last call's
  context breakdown (D-19).
- Actions (D-18): conversation compaction and memory clearing run in the sandbox; prompt instructions
  and source toggles are written into the agent's IDENTITY.md; the LLM provider/model switch is live;
  context length / max output are stored and wait for the next recreate."""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from rfa_mas.nemoclaw import audit, logs
from rfa_mas.nemoclaw.config import Assignments
from rfa_mas.nemoclaw.markers import _MARKER
from rfa_mas.nemoclaw.services.llm import LlmControl, LlmError
from rfa_mas.nemoclaw.services.presentation import initials_for
from rfa_mas.nemoclaw.services.tasks import TaskService, security_label
from rfa_mas.nemoclaw.store import Store

MANAGEMENT_HINT = "데모에서는 관리 에이전트를 수정할 수 없습니다."
SANDBOX_LIMIT = 2
SANDBOX_LIMIT_MESSAGE = "샌드박스는 많은 양의 메모리를 요구합니다. 현재 데모에서는 2개까지만 제공드립니다."
CONTEXT_CHOICES = [8192, 16384, 32768, 65536]
MAX_OUTPUT_CHOICES = [2048, 4096, 8192]
DEFAULT_CONTEXT, DEFAULT_MAX_OUTPUT = 32768, 4096
MANAGEMENT = {"assistant": ("통합 관리자", "통합 질의 담당 · 업무 에이전트를 관장"),
              "censor": ("검열 에이전트", "공개 범위 기밀 검토")}
ROLE_LABELS = {"research": "근거 조사", "benchmark": "수치 조회", "summarizer": "요약", "verifier": "검증"}


def session_script(sessions: str, workspace: str, *, compact: bool) -> str:
    """sh (the sandbox has no python) for OpenClaw's session store: ``<id>.jsonl`` transcripts with
    ``<id>.trajectory.jsonl`` / ``<id>.trajectory-path.json`` beside them and the ``sessions.json`` index.
    compact keeps the newest session, clear removes every session plus the workspace memory; the index
    drops entries whose transcript is gone. Prints the number of sessions removed."""
    keep = ('keep=$(ls -t *.jsonl 2>/dev/null | grep -v "\\.trajectory\\.jsonl$" | head -n 1); keep=${keep%.jsonl}; '
            if compact else 'keep=""; ')
    memory = "" if compact else f'rm -f "{workspace}/MEMORY.md"; rm -rf "{workspace}/memory"; '
    prune = ("node -e 'const fs=require(\"fs\"),p=\"sessions.json\";if(fs.existsSync(p)){const d=JSON.parse("
             "fs.readFileSync(p,\"utf8\"));for(const k of Object.keys(d)){const s=d[k]&&d[k].sessionId;"
             "if(s&&!fs.existsSync(s+\".jsonl\"))delete d[k]}fs.writeFileSync(p,JSON.stringify(d,null,2))}' 2>/dev/null; ")
    return (f'cd "{sessions}" 2>/dev/null || {{ echo 0; exit 0; }}; {keep}n=0; '
            'for f in $(ls *.jsonl 2>/dev/null | grep -v "\\.trajectory\\.jsonl$"); do id=${f%.jsonl}; '
            '[ "$id" = "$keep" ] && continue; rm -f "$id.jsonl" "$id.trajectory.jsonl" "$id.trajectory-path.json"; '
            f'n=$((n+1)); done; {prune}{memory}echo $n')


class AdminError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# --------------------------------------------------------------------------- live state


class LiveState:
    """Background snapshot of ``nemoclaw list --json`` and each existing sandbox's roster."""

    def __init__(self, runner, nemoclaw_bin: str, sandboxes: Callable[[], list[str]], ttl: float = 60.0):
        self.runner, self.bin, self._sandboxes, self.ttl = runner, nemoclaw_bin, sandboxes, ttl
        self._snap: dict = {"sandboxes": None, "agents": {}, "observedAt": None, "error": None}
        self._busy = False
        self._lock = threading.Lock()

    def snapshot(self) -> dict:
        if self.runner is None:
            return self._snap
        with self._lock:
            stale = not self._snap["observedAt"] or time.time() - self._snap["observedAt"] > self.ttl
            if stale and not self._busy:
                self._busy = True
                threading.Thread(target=self._refresh, daemon=True).start()
        return self._snap

    def _refresh(self) -> None:
        from rfa_mas.nemoclaw.controller import Observer

        try:
            obs = Observer(self.runner, self.bin)
            listed = obs.list_sandboxes()
            agents = {sb: obs.agents_list(sb) for sb in self._sandboxes() if sb in listed}
            self._snap = {"sandboxes": listed, "agents": agents, "observedAt": time.time(), "error": None}
        except Exception as exc:  # host lock busy, CLI missing … keep the last snapshot
            logs.event("admin_live", action="refresh_failed", error=f"{type(exc).__name__}: {exc}"[:200])
            self._snap = {**self._snap, "error": f"{type(exc).__name__}", "observedAt": time.time()}
        finally:
            self._busy = False


# --------------------------------------------------------------------------- metrics


def _midnight(days_ago: int = 0) -> float:
    today = dt.datetime.combine(dt.date.today(), dt.time())
    return (today - dt.timedelta(days=days_ago)).timestamp()


class Metrics:
    """Per-agent numbers from the audit ledger's ``inference`` rows (re-read per call; small ledger)."""

    def rows(self, since: float) -> list[dict]:
        return [r for r in audit._rows() if r.get("kind") == "inference" and (r.get("ts") or 0) >= since]

    def calls_today(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.rows(_midnight()):
            if r.get("agent"):
                out[r["agent"]] = out.get(r["agent"], 0) + 1
        return out

    def agent(self, agent_id: str) -> dict:
        week = [r for r in self.rows(_midnight(6)) if r.get("agent") == agent_id]
        today = [r for r in week if r["ts"] >= _midnight()]
        tokens = sum(((r.get("detail") or {}).get("usage") or {}).get("total_tokens") or 0 for r in today)
        latencies = [(r.get("detail") or {}).get("ms") for r in today if (r.get("detail") or {}).get("ms") is not None]
        days = []
        for back in range(6, -1, -1):
            start, end = _midnight(back), _midnight(back - 1) if back else float("inf")
            day = dt.date.today() - dt.timedelta(days=back)
            days.append({"day": day.isoformat(), "calls": sum(1 for r in week if start <= r["ts"] < end)})
        last = next((r for r in sorted(week, key=lambda r: -r["ts"]) if (r.get("detail") or {}).get("context")), None)
        return {"calls": len(today), "tokens": tokens, "tokensThousands": round(tokens / 1000, 1),
                "avgLatencySeconds": round(sum(latencies) / len(latencies) / 1000, 1) if latencies else 0.0,
                "blockedCalls": sum(1 for r in today if r.get("verdict") == "block"), "last7Days": days,
                "lastContext": ({**last["detail"]["context"], "at": last["ts"]} if last else None)}


# --------------------------------------------------------------------------- service


class AdminService:
    def __init__(self, *, assignments: Callable[[], Assignments], tasks: TaskService, store: Store,
                 llm: LlmControl | None = None, runner=None, nemoclaw_bin: str = "nemoclaw", secret: bytes = b"",
                 facade_url: str | None = None, routing=None, metrics: Metrics | None = None):
        self._assignments, self.tasks, self.store, self.llm = assignments, tasks, store, llm
        self.runner, self.bin, self.secret, self.routing = runner, nemoclaw_bin, secret, routing
        self.facade_url = facade_url
        self.metrics = metrics or Metrics()
        self.live = LiveState(runner, nemoclaw_bin, lambda: list(self._assignments().sandboxes))
        self._sources_cache: tuple[float, list[dict]] = (0.0, [])

    # ---- helpers ------------------------------------------------------------------------------

    def _roles(self, a: Assignments) -> dict[str, dict]:
        """agent id → {group, role, tasks, parent}: which task(s) each agent answers for."""
        info: dict[str, dict] = {}
        tasks = self.tasks.list(include_failed=True)
        for t in tasks:
            if t.get("worker"):
                entry = info.setdefault(t["worker"], {"tasks": [], "role": "task", "parent": None, "look": t})
                entry["tasks"].append({"id": t["id"], "name": t["name"]})
        for agent_id, spec in a.agents.items():
            if spec.kind == "fixed" or (spec.kind == "task" and not spec.delegatable and not spec.team):
                info[agent_id] = {"tasks": [], "role": "assistant" if spec.kind == "fixed" else "censor",
                                  "parent": None, "look": None, "group": "management"}
                continue
            entry = info.setdefault(agent_id, {"tasks": [], "role": "task", "parent": None, "look": None})
            if spec.team and not spec.delegatable:
                entry["role"], entry["parent"] = "member", next(
                    (sid for sid, s in a.agents.items() if s.team == spec.team and s.delegatable), None)
            elif spec.team:
                entry["role"] = "supervisor"
            entry["group"] = "task"
        return info

    def _status(self, agent_id: str, a: Assignments, info: dict, snap: dict) -> tuple[str, bool]:
        look = info.get("look") or {}
        if look.get("status") == "applying":
            return "applying", True
        if look.get("status") == "failed":
            return "stopped", True
        sandbox = a.sandbox_for(agent_id)
        if snap["sandboxes"] is None:
            return "running", False  # not observed yet: declared = running
        if sandbox not in snap["sandboxes"]:
            return "stopped", True
        from rfa_mas.nemoclaw.manifests import openclaw_agent_id

        roster = snap["agents"].get(sandbox)
        if roster is None:
            return "running", False
        return ("running" if openclaw_agent_id(a, agent_id) in roster else "stopped"), True

    def _item(self, agent_id: str, a: Assignments, info: dict, snap: dict, calls: dict[str, int]) -> dict:
        spec = a.agents[agent_id]
        group = info.get("group", "task")
        look = info.get("look") or {}
        sandbox = a.sandbox_for(agent_id)
        status, observed = self._status(agent_id, a, info, snap)
        if group == "management":
            title, subtitle = MANAGEMENT.get(agent_id, (agent_id, spec.description))
        elif info["role"] == "member":
            role = next((m for m in (spec.skill or "").split("-")[1:2]), "") or agent_id.rsplit("-", 1)[-1]
            title, subtitle = agent_id, f"{info['parent']} 팀 멤버 · {ROLE_LABELS.get(role, role)}"
        else:
            title = agent_id
            subtitle = " · ".join(f"{t['name']} 태스크" for t in info["tasks"]) or (spec.description.split(".")[0][:60])
        editable = group != "management"
        return {"id": agent_id, "name": agent_id, "title": title, "subtitle": subtitle, "group": group,
                "role": info["role"], "parentId": info.get("parent"), "tasks": info["tasks"], "sandbox": sandbox,
                "sandboxLine": f"{sandbox} 샌드박스" if sandbox else "배치 안 됨",
                "securityLabel": security_label(a, sandbox), "status": status, "observed": observed,
                "callsToday": calls.get(agent_id, 0), "icon": look.get("icon", "generic"),
                "color": look.get("color") or ({"bg": "#1a1d23", "fg": "#ffffff"} if agent_id == "assistant"
                                               else {"bg": "#e6e8ee", "fg": "#1a1d23"}),
                "initials": look.get("initials") or ("통합" if agent_id == "assistant" else initials_for(agent_id)),
                "editable": editable, "readOnlyReason": None if editable else MANAGEMENT_HINT}

    def _check(self, agent_id: str, authenticated: bool, *, write: bool = False) -> tuple[Assignments, dict]:
        a = self._assignments()
        if agent_id not in a.agents:
            raise AdminError(404, "unknown_agent", "없는 에이전트입니다.")
        info = self._roles(a).get(agent_id, {"tasks": [], "role": "task", "parent": None, "group": "task"})
        if write:
            if not authenticated:
                raise AdminError(403, "owner_only", "게스트는 바꿀 수 없습니다. 소유자에게 요청하세요.")
            if info.get("group") == "management":
                raise AdminError(403, "management_agent", MANAGEMENT_HINT)
        return a, info

    # ---- agents -------------------------------------------------------------------------------

    def list_agents(self) -> dict:
        a = self._assignments()
        info, snap, calls = self._roles(a), self.live.snapshot(), self.metrics.calls_today()
        order: list[str] = []
        for t in self.tasks.list(include_failed=True):  # each task's worker, then its team members
            worker = t.get("worker")
            if worker in a.agents and worker not in order:
                order.append(worker)
                order += [m for m, s in a.agents.items() if info.get(m, {}).get("parent") == worker and m not in order]
        order += [x for x in a.agents if info.get(x, {}).get("group") == "task" and x not in order]
        order += [x for x in a.agents if info.get(x, {}).get("group") == "management" and x not in order]
        items = [self._item(x, a, info.get(x, {}), snap, calls) for x in order]
        return {"agents": items, "counts": {"total": len(items), "task": sum(i["group"] == "task" for i in items),
                                            "management": sum(i["group"] == "management" for i in items)},
                "observedAt": snap["observedAt"], "hint": MANAGEMENT_HINT}

    def agent(self, agent_id: str) -> dict:
        a, info = self._check(agent_id, True)
        spec = a.agents[agent_id]
        base = self._item(agent_id, a, info, self.live.snapshot(), self.metrics.calls_today())
        stats = self.metrics.agent(agent_id)
        sandbox = base["sandbox"]
        settings = self._sandbox_settings(sandbox)
        ctx = stats.pop("lastContext")
        current = self.llm.current() if self.llm else {"provider": "", "label": "", "model": ""}
        alias = self.routing.aliases.get(spec.alias) if self.routing else None
        prompt = self._instructions(agent_id)
        return {**base, "description": spec.description, "alias": spec.alias, "skill": spec.skill,
                "groups": list(spec.groups), "tools": spec.tools.model_dump(exclude_none=True),
                "model": alias.model if alias else current["model"],
                "context": None if ctx is None else {
                    "usedTokens": ctx.get("total", 0), "limitTokens": settings["contextLength"],
                    "measured": bool(ctx.get("measured")), "measuredAt": ctx.get("at"),
                    "compaction": "safeguard", "preserveRecentTurns": 1,
                    "breakdown": {k: ctx.get(k, 0) for k in ("systemPrompt", "toolDefinitions", "memoryNotes",
                                                             "conversation")}},
                "sources": self.sources(agent_id, a),
                "stats": {"provider": current["label"], "model": alias.model if alias else current["model"], **stats},
                "instructions": prompt["text"], "instructionsUpdatedAt": prompt["updatedAt"],
                "actions": {"compact": base["editable"], "clearMemory": base["editable"],
                            "editPrompt": base["editable"], "toggleSources": base["editable"]}}

    # ---- sources (불러오는 자료) ------------------------------------------------------------------

    def _catalog_sources(self) -> list[dict]:
        at, cached = self._sources_cache
        if cached and time.time() - at < 60:
            return cached
        sources: list[dict] = []
        if self.facade_url:
            try:
                r = httpx.get(f"{self.facade_url.rstrip('/')}/tasks", timeout=3)
                if r.status_code == 200:
                    sources = [{"id": s["id"], "title": s.get("name") or s["id"], "description": s.get("description", "")}
                               for s in r.json() if isinstance(s, dict) and s.get("id")]
            except (httpx.HTTPError, ValueError):
                sources = []
        if not sources:
            from rfa_mas.nemoclaw.ask import KB_SEED

            seen: dict[str, dict] = {}
            if KB_SEED.exists():
                for line in KB_SEED.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        note = json.loads(line)
                        seen.setdefault(note["domain_id"], {"id": note["domain_id"], "title": note["domain_id"],
                                                            "description": ""})
            sources = list(seen.values())
        self._sources_cache = (time.time(), sources)
        return sources

    def sources(self, agent_id: str, a: Assignments | None = None) -> list[dict]:
        a = a or self._assignments()
        spec = a.agents[agent_id]
        # a team supervisor reads the knowledge API through its members
        team = [s for s in a.agents.values() if spec.team and s.team == spec.team]
        reachable = any("intranet-ro" in s.groups for s in [spec, *team]) or spec.kind == "fixed"
        stored = {r["source_id"]: bool(r["enabled"]) for r in
                  self.store.query("SELECT * FROM agent_sources WHERE agent_id=?", (agent_id,))}
        return [{**s, "enabled": reachable and stored.get(s["id"], True), "available": reachable,
                 "unavailableReason": None if reachable else "사내 지식 경로(intranet-ro)가 없는 구획이라 불러올 수 없음"}
                for s in self._catalog_sources()]

    def set_source(self, agent_id: str, source_id: str, enabled: bool, authenticated: bool) -> dict:
        a, _ = self._check(agent_id, authenticated, write=True)
        source = next((s for s in self.sources(agent_id, a) if s["id"] == source_id), None)
        if source is None:
            raise AdminError(404, "unknown_source", "없는 자료입니다.")
        if not source["available"]:
            raise AdminError(409, "source_unavailable", source["unavailableReason"])
        self.store.execute("INSERT OR REPLACE INTO agent_sources (agent_id, source_id, enabled, updated) VALUES (?,?,?,?)",
                           (agent_id, source_id, int(enabled), self.store.now()))
        applied = self._reseed_identity(a, agent_id)
        audit.record(kind="admin", verdict="applied" if applied["applied"] else "stored", action="agent-source",
                     agent=agent_id, sandbox=a.sandbox_for(agent_id), detail={"source": source_id, "enabled": enabled})
        return {"sources": self.sources(agent_id, a), **applied}

    # ---- prompt (시스템 프롬프트 편집) -------------------------------------------------------------

    def _instructions(self, agent_id: str) -> dict:
        row = self.store.one("SELECT * FROM instructions WHERE id=?", (f"prompt:{agent_id}",))
        return {"text": row["text"] if row else "", "updatedAt": row["created"] if row else None}

    def prompt(self, agent_id: str) -> dict:
        a, _ = self._check(agent_id, True)
        spec = a.agents[agent_id]
        skill = Path(__file__).resolve().parents[4] / "deploy" / "nemoclaw" / "skills" / (spec.skill or "") / "SKILL.md"
        extra = self._instructions(agent_id)
        return {"agentId": agent_id, "identity": self._identity(a, agent_id, extra=False), "skill": spec.skill,
                "skillText": skill.read_text(encoding="utf-8") if spec.skill and skill.exists() else "",
                "instructions": extra["text"], "instructionsUpdatedAt": extra["updatedAt"]}

    def set_prompt(self, agent_id: str, text: str, authenticated: bool) -> dict:
        a, _ = self._check(agent_id, authenticated, write=True)
        self.store.execute("INSERT OR REPLACE INTO instructions (id, agent_id, scope, text, source, created) "
                           "VALUES (?,?,?,?,?,?)", (f"prompt:{agent_id}", agent_id, "prompt", text.strip(), "admin",
                                                     self.store.now()))
        applied = self._reseed_identity(a, agent_id)
        audit.record(kind="admin", verdict="applied" if applied["applied"] else "stored", action="agent-prompt",
                     agent=agent_id, sandbox=a.sandbox_for(agent_id), detail={"chars": len(text.strip())})
        return {**self.prompt(agent_id), **applied}

    def _identity(self, a: Assignments, agent_id: str, *, extra: bool = True) -> str:
        """IDENTITY.md as seeded, without the signed marker line; ``extra`` adds the admin sections."""
        from rfa_mas.nemoclaw.manifests import render_identity

        text = render_identity(a, agent_id, self.secret)
        if not extra:  # the signed routing marker is never shown
            return _MARKER.sub("[서명된 라우팅 마커]", text)
        out = text
        sources = self.sources(agent_id, a)
        off = [s["title"] for s in sources if s["available"] and not s["enabled"]]
        on = [s["title"] for s in sources if s["enabled"]]
        if off:
            out += ("\n\n# 불러오는 자료 (관리자 설정)\n" + f"- 사내 지식에서 다음 자료만 쓴다: {', '.join(on) or '없음'}\n"
                    + f"- 다음 자료는 조회하지도 인용하지도 않는다: {', '.join(off)}\n")
        if instr := self._instructions(agent_id)["text"]:
            out += "\n\n# 운영자 추가 지시\n" + instr + "\n"
        return out

    def _reseed_identity(self, a: Assignments, agent_id: str) -> dict:
        """Upload the agent's IDENTITY.md with the admin sections (the next turn reads it)."""
        from rfa_mas.nemoclaw.manifests import openclaw_agent_id, workspace_path

        sandbox = a.sandbox_for(agent_id)
        if self.runner is None or not sandbox:
            return {"applied": False, "note": "샌드박스 없이 실행 중이라 저장만 했습니다."}
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "IDENTITY.md"
            local.write_text(self._identity(a, agent_id), encoding="utf-8")
            target = f"{workspace_path(openclaw_agent_id(a, agent_id))}/IDENTITY.md"
            result = self.runner.run([self.bin, sandbox, "upload", str(local), target], timeout=180)
        if not result.ok:
            raise AdminError(502, "sandbox_failed", f"IDENTITY.md 를 올리지 못했습니다: {(result.stderr or result.stdout)[-160:]}")
        return {"applied": True, "note": "다음 대화부터 적용됩니다."}

    # ---- session actions (대화 압축 · 기억 비우기) -------------------------------------------------

    def session_action(self, agent_id: str, action: str, authenticated: bool) -> dict:
        from rfa_mas.nemoclaw.manifests import agent_dir, openclaw_agent_id, workspace_path

        a, _ = self._check(agent_id, authenticated, write=True)
        sandbox = a.sandbox_for(agent_id)
        oc = openclaw_agent_id(a, agent_id)
        sessions, workspace = f"{agent_dir(oc)}/sessions", workspace_path(oc)
        if action not in ("compact", "clear-memory"):
            raise AdminError(404, "unknown_action", "없는 동작입니다.")
        label, obj = ("대화 압축", "대화 압축을") if action == "compact" else ("기억 비우기", "기억 비우기를")
        script = session_script(sessions, workspace, compact=action == "compact")
        if self.runner is None or not sandbox:
            return {"agentId": agent_id, "action": action, "applied": False,
                    "note": f"샌드박스 없이 실행 중이라 {obj} 건너뛰었습니다."}
        result = self.runner.run([self.bin, sandbox, "exec", "--timeout", "60", "--", "sh", "-c", script], timeout=180)
        if not result.ok:
            raise AdminError(502, "sandbox_failed", f"{label}에 실패했습니다: {(result.stderr or result.stdout)[-160:]}")
        tail = (result.stdout or "").strip().splitlines()
        removed = int(tail[-1]) if tail and tail[-1].strip().isdigit() else 0
        audit.record(kind="admin", verdict="applied", action=f"agent-{action}", agent=agent_id, sandbox=sandbox,
                     detail={"removed": removed})
        return {"agentId": agent_id, "action": action, "applied": True, "removedSessions": removed,
                "note": f"{obj} 마쳤습니다."}

    # ---- sandboxes ----------------------------------------------------------------------------

    def _sandbox_settings(self, sandbox: str | None) -> dict:
        row = self.store.one("SELECT * FROM sandbox_settings WHERE sandbox=?", (sandbox or "",)) or {}
        return {"contextLength": row.get("context_length") or DEFAULT_CONTEXT,
                "maxOutputTokens": row.get("max_output_tokens") or DEFAULT_MAX_OUTPUT,
                "pending": bool(row)}

    def _gateway(self, sandbox: str) -> dict:
        url = (self.routing.broker.gateways.get(sandbox) if self.routing else None) or ""
        port = int(url.rsplit(":", 1)[1].split("/")[0]) if url.count(":") >= 2 else None
        return {"url": url or None, "port": port}

    def _sandbox_item(self, name: str, a: Assignments, snap: dict) -> dict:
        spec = a.sandboxes[name]
        agents = [x for x, s in a.agents.items() if a.sandbox_for(x) == name]
        tasks = [{"id": t["id"], "name": t["name"]} for t in self.tasks.list() if t.get("sandbox") == name]
        exists = None if snap["sandboxes"] is None else name in snap["sandboxes"]
        current = self.llm.current() if self.llm else {"provider": "", "label": "", "model": ""}
        return {"id": name, "name": name, "default": spec.default, "groups": list(spec.groups),
                "privilege": a.sandbox_privilege(name),
                "securityLabel": security_label(a, name),
                "status": "unknown" if exists is None else ("running" if exists else "stopped"),
                "observed": exists is not None, "agentCount": len(agents), "tasks": tasks,
                "provider": current["label"], "model": current["model"], "gatewayPort": self._gateway(name)["port"]}

    def list_sandboxes(self) -> dict:
        a, snap = self._assignments(), self.live.snapshot()
        items = [self._sandbox_item(n, a, snap) for n in a.ordered_sandboxes()]
        return {"sandboxes": items, "limit": SANDBOX_LIMIT, "canAdd": len(items) < SANDBOX_LIMIT,
                "limitMessage": SANDBOX_LIMIT_MESSAGE, "observedAt": snap["observedAt"]}

    def sandbox(self, name: str) -> dict:
        a = self._assignments()
        if name not in a.sandboxes:
            raise AdminError(404, "unknown_sandbox", "없는 샌드박스입니다.")
        snap = self.live.snapshot()
        item = self._sandbox_item(name, a, snap)
        settings = self._sandbox_settings(name)
        current = self.llm.current() if self.llm else {"provider": "", "label": "", "model": ""}
        live_row = (snap["sandboxes"] or {}).get(name) or {}
        gateway = self._gateway(name)
        groups = [{"id": g, "privilege": a.security_groups[g].privilege, "description": a.security_groups[g].description,
                   "presets": list(a.security_groups[g].presets), "mcpServers": list(a.security_groups[g].mcp_servers)}
                  for g in a.sandboxes[name].groups if g in a.security_groups]
        return {**item, "agentsManifest": f"deploy/nemoclaw/agents/{name}.agents.yaml",
                "agents": [{"id": x, "kind": s.kind} for x, s in a.agents.items() if a.sandbox_for(x) == name],
                "securityGroups": groups,
                "inference": {"provider": current["provider"], "providerLabel": current["label"], "model": current["model"],
                              "providerOptions": self.llm.options() if self.llm else [],
                              "contextLength": settings["contextLength"], "maxOutputTokens": settings["maxOutputTokens"],
                              "contextLengthChoices": CONTEXT_CHOICES, "maxOutputChoices": MAX_OUTPUT_CHOICES,
                              "pendingRecreate": [k for k in ("contextLength", "maxOutputTokens") if settings["pending"]]},
                "gateway": {"id": f"{name}-gateway", "label": f"{name} 게이트웨이", "url": gateway["url"],
                            "port": gateway["port"], "registeredProvider": "llm-api",
                            "hasKey": bool(self.llm and any(o["selectable"] for o in self.llm.options())),
                            "sandboxModel": live_row.get("model"), "sandboxProvider": live_row.get("provider"),
                            "currentRoute": f"llm-api / {current['model']}",
                            "sharedWith": [n for n in a.sandboxes if n != name]},
                "applyNote": "제공자와 모델은 바로 적용 · 컨텍스트 길이와 최대 응답 길이는 다시 만들 때 적용"}

    def update_sandbox(self, name: str, *, provider: str | None, model: str | None, context_length: int | None,
                       max_output_tokens: int | None, authenticated: bool) -> dict:
        if not authenticated:
            raise AdminError(403, "owner_only", "게스트는 바꿀 수 없습니다. 소유자에게 요청하세요.")
        a = self._assignments()
        if name not in a.sandboxes:
            raise AdminError(404, "unknown_sandbox", "없는 샌드박스입니다.")
        applied, pending = [], []
        if provider or model:
            if self.llm is None:
                raise AdminError(503, "llm_unavailable", "추론 제공자를 바꿀 수 없습니다.")
            current = self.llm.current()
            try:
                self.llm.switch(provider or current["provider"], model)
            except LlmError as exc:
                raise AdminError(409 if exc.code == "not_selectable" else 422, exc.code, exc.message) from exc
            applied += [k for k, v in (("provider", provider), ("model", model)) if v]
        if context_length is not None or max_output_tokens is not None:
            if context_length is not None and context_length not in CONTEXT_CHOICES:
                raise AdminError(422, "invalid_context_length", f"컨텍스트 길이는 {CONTEXT_CHOICES} 중 하나입니다.")
            if max_output_tokens is not None and max_output_tokens not in MAX_OUTPUT_CHOICES:
                raise AdminError(422, "invalid_max_output", f"최대 응답 길이는 {MAX_OUTPUT_CHOICES} 중 하나입니다.")
            now = self._sandbox_settings(name)
            self.store.execute("INSERT OR REPLACE INTO sandbox_settings (sandbox, context_length, max_output_tokens, updated) "
                               "VALUES (?,?,?,?)", (name, context_length or now["contextLength"],
                                                    max_output_tokens or now["maxOutputTokens"], self.store.now()))
            pending += [k for k, v in (("contextLength", context_length), ("maxOutputTokens", max_output_tokens))
                        if v is not None]
            audit.record(kind="admin", verdict="stored", action="sandbox-settings", sandbox=name,
                         detail={"contextLength": context_length, "maxOutputTokens": max_output_tokens})
        return {"sandboxId": name, "applied": applied, "requiresRecreate": pending, "sandbox": self.sandbox(name)}

    def add_sandbox(self, authenticated: bool) -> None:
        if not authenticated:
            raise AdminError(403, "owner_only", "게스트는 바꿀 수 없습니다. 소유자에게 요청하세요.")
        if len(self._assignments().sandboxes) >= SANDBOX_LIMIT:
            raise AdminError(409, "sandbox_limit", SANDBOX_LIMIT_MESSAGE)
        raise AdminError(501, "not_supported", "샌드박스 추가는 운영 명령(make bootstrap)으로만 합니다.")
