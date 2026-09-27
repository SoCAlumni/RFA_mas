"""Owner's Task-team overview with linked KB (PoC-owned ledger, no core DTO change).

Explicit task<->source links live in the local chat DB. They are an owner reference
scope shown in the UI, never data authority: role searches stay bounded by the domain's
current ACL/policy version, and cited evidence is read from the core's team results.
Sources are always re-read through the core's current-ACL listing, never cached here.
"""

import json
from contextlib import suppress
from datetime import UTC, datetime

from rfa_mas.application.workers import ROLE_TOOLS
from rfa_mas.errors import RfaError
from rfa_mas.poc.routing import subjects

MAX_LINKS = 50
RECENT_RUNS = 5
MAX_SUBJECT_MATCHES = 12
BUDGET_FIELDS = ("max_steps", "max_tool_calls", "max_tokens", "timeout_seconds", "concurrency")
NOTICE = (
    "연결 자료는 담당 팀의 참고 범위입니다. 역할 검색은 자료 공간의 현재 권한 범위에서 "
    "수행되며, 실제 인용 근거는 팀 실행 결과에서 읽습니다."
)


class TeamOverview:
    def __init__(self, chat):
        self.chat = chat
        with self.chat.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS task_kb_links (
                task_id TEXT NOT NULL, source_id TEXT NOT NULL, domain_id TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                PRIMARY KEY(task_id, source_id))""")

    async def _records(self):
        principal = await self.chat.router.repository.local_principal()
        return await self.chat.router.catalog.list_for(principal)

    async def _record(self, task_id):
        for record in await self._records():
            if record.task.task_id == task_id:
                return record
        raise RfaError("not_found", "Task 팀을 찾을 수 없습니다.")

    async def _documents(self, domain_id):
        listed = await self.chat.call("GET", f"/v1/knowledge/sources?domain_id={domain_id}")
        return {item["document"]["source_id"]: item for item in listed}

    async def list(self):
        return [await self._overview(record) for record in await self._records()]

    async def get(self, task_id):
        return await self._overview(await self._record(task_id))

    async def link(self, task_id, source_ids, *, note=""):
        record = await self._record(task_id)
        domain = record.task.domain_id.value
        documents = await self._documents(domain)
        wanted = list(dict.fromkeys(source_ids))
        if any(source_id not in documents for source_id in wanted):
            raise RfaError(
                "source_not_linkable",
                "같은 자료 공간에서 현재 접근 가능한 자료만 연결할 수 있습니다.",
            )
        now = datetime.now(UTC).isoformat()
        with self.chat.connect() as db:
            existing = {
                r[0]
                for r in db.execute(
                    "SELECT source_id FROM task_kb_links WHERE task_id=?", (task_id,)
                )
            }
            new = [source_id for source_id in wanted if source_id not in existing]
            if len(existing) + len(new) > MAX_LINKS:
                raise RfaError("too_many_links", f"Task당 연결 자료는 {MAX_LINKS}개까지입니다.")
            db.executemany(
                "INSERT INTO task_kb_links VALUES (?,?,?,?,?)",
                [(task_id, source_id, domain, note[:200], now) for source_id in new],
            )
        return await self._overview(record, documents=documents)

    async def unlink(self, task_id, source_id):
        record = await self._record(task_id)
        with self.chat.connect() as db:
            db.execute(
                "DELETE FROM task_kb_links WHERE task_id=? AND source_id=?", (task_id, source_id)
            )
        return await self._overview(record)

    async def _overview(self, record, *, documents=None):
        task, team, spec = record.task, record.team, record.team.spec
        domain = task.domain_id.value
        if documents is None:
            documents = await self._documents(domain)
        with self.chat.connect() as db:
            links = db.execute(
                "SELECT source_id, note, created_at FROM task_kb_links WHERE task_id=? "
                "ORDER BY rowid",
                (task.task_id,),
            ).fetchall()
            turns = db.execute(
                "SELECT text, run_id, status, created_at, route_json FROM chat_turns "
                "WHERE run_id IS NOT NULL ORDER BY created_at DESC"
            ).fetchall()
        runs = [
            dict(row)
            for row in turns
            if json.loads(row["route_json"] or "{}").get("task_id") == task.task_id
        ]
        cited: dict[str, dict] = {}
        recent = []
        for row in runs[:RECENT_RUNS]:
            entry = {
                "run_id": row["run_id"],
                "text": row["text"][:120],
                "created_at": row["created_at"],
                "chat_status": row["status"],
                "team_status": None,
                "simulated": None,
                "roles": [],
            }
            with suppress(RfaError):
                outcome = await self.chat.call("GET", f"/v1/runs/{row['run_id']}/team")
                entry["team_status"] = outcome["status"]
                entry["simulated"] = outcome.get("simulated")
                for role in outcome.get("roles", []):
                    entry["roles"].append({"role": role["role"], "status": role["status"]})
                    for ref in role.get("evidence", []):
                        item = cited.setdefault(
                            ref["source_id"],
                            {"source_revision": ref["source_revision"], "roles": []},
                        )
                        if role["role"] not in item["roles"]:
                            item["roles"].append(role["role"])
            recent.append(entry)

        def describe(source_id):
            item = documents.get(source_id)
            doc = item["document"] if item else None
            return {
                "source_id": source_id,
                "domain_id": domain,
                "available": doc is not None,
                "title": doc["title"] if doc else None,
                "audience": doc["audience"] if doc else None,
                "source_revision": doc["source_revision"] if doc else None,
                "revision_number": item["revision_number"] if item else None,
            }

        linked_ids = [row["source_id"] for row in links]
        linked = [
            describe(row["source_id"]) | {"note": row["note"], "linked_at": row["created_at"]}
            for row in links
        ]
        goal_words = subjects(task.goal)
        matches = []
        for source_id, item in documents.items():
            if source_id in linked_ids:
                continue
            doc = item["document"]
            shared = goal_words & subjects(f"{doc['title']} {doc['content']}")
            if shared:
                matches.append((-len(shared), source_id, sorted(shared)))
        matches.sort()
        member_states = {m.agent_id: m.prepare for m in team.member_states}
        budget = spec.execution_budget or spec.template.budget
        return {
            "task": {
                "task_id": task.task_id,
                "goal": task.goal,
                "domain_id": domain,
                "status": task.status,
                "revision": task.revision,
            },
            "team": {
                "team_id": spec.team_id,
                "pattern": spec.template.pattern,
                "template_id": spec.template.template_id,
                "template_version": spec.template.version,
                "runtime_kind": spec.template.runtime_kind,
                "state": team.state,
                "mode": str(getattr(team.mode, "value", team.mode)),
                "reason": record.reason,
                "phase": record.phase,
                "generation": record.generation,
                "communication": spec.communication,
                "budget": {field: getattr(budget, field) for field in BUDGET_FIELDS},
                "members": [
                    {
                        "role": m.role,
                        "agent_id": m.spec.agent_id,
                        "capabilities": list(m.spec.capabilities),
                        # Runner allowlist (read-only tools) when the spec binds none.
                        "tools": list(m.tool_names) or sorted(ROLE_TOOLS.get(m.role, ())),
                        "max_steps": m.spec.max_steps,
                        "max_tool_calls": m.spec.max_tool_calls,
                        "prepare": member_states.get(m.spec.agent_id, "not_started"),
                        "failed": m.spec.agent_id in team.failed_agent_ids,
                    }
                    for m in spec.members
                ],
            },
            "selectable": task.status == "active" and record.reason == "ready",
            "runs": {"count": len(runs), "recent": recent},
            "linked_sources": linked,
            "cited_sources": [
                describe(source_id)
                | {"source_revision": item["source_revision"], "roles": item["roles"]}
                for source_id, item in sorted(cited.items())
            ],
            "subject_matches": [
                describe(source_id) | {"shared": shared}
                for _, source_id, shared in matches[:MAX_SUBJECT_MATCHES]
            ],
            "notice": NOTICE,
        }
