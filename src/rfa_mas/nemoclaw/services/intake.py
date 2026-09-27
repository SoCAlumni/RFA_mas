"""RFA_module's head requests (D-21): ``POST /v1/head/ask`` with RFA_module's head contract v0.2.0.

The desk sends no bearer and no ``request_id``, retries after its 120 s client timeout and expects
``refusal`` as a string. So the id is derived from the mention URL and the round (``len(feedback)+1``
— RFA_module sends every earlier rejection of the approval), oversized fields are cut instead of
refused, and the pipeline gets 110 s. A request from a registered source takes that source's grade
and task (D-24).

Every request is recorded in ``intake`` as soon as it arrives, and its stages (routing + task agent =
"RAG 검색", censor = "검증") are updated while the pipeline runs — the 결재함 shows the item before the
desk has even written its draft."""

from __future__ import annotations

import hashlib
import re

from rfa_mas.nemoclaw import audit, logs
from rfa_mas.nemoclaw.ask import INJECTION_PATTERNS, HeadDecision
from rfa_mas.nemoclaw.ask_contract import AskRequest, ContextItem, FeedbackItem
from rfa_mas.nemoclaw.services.chat import sentences
from rfa_mas.nemoclaw.store import Store

HEAD_TIMEOUT_SECONDS = 110          # RFA_module's client gives up at 120 s
REFUSAL_TEXT = {"no_task": "관련 업무를 찾지 못했습니다",
                "no_knowledge": "답할 근거를 찾지 못했습니다",
                "blocked_by_policy": "공개 범위 정책상 답할 수 없습니다"}
_MENTION = re.compile(r"(^|\s)@[A-Za-z0-9_-]+|<@[A-Z0-9]+>")


def item_id_for(url: str) -> str:
    """Stable 결재함 item id of one mention (same before and after the approval exists)."""
    return "q" + hashlib.sha1(url.encode()).hexdigest()[:12]


def request_id_for(url: str, round_: int) -> str:
    return f"rfa-{hashlib.sha1(url.encode()).hexdigest()[:16]}-r{round_}"


def title_for(channel: str, url: str, context: list[dict], question: str) -> str:
    """GitHub: the issue title when the thread starts with the issue (the desk sends "title\\nbody"
    first unless the mention *is* the issue body or the thread was cut to 10). Else the question's
    first sentence without @mentions."""
    if channel == "github" and "#issuecomment" in url and context and len(context) < 10:
        first = (context[0].get("text") or "").split("\n", 1)[0].strip()
        if first:
            return first[:80]
    text = " ".join(_MENTION.sub(" ", question).split())
    head = sentences(text)
    return (head[0].strip() if head else text)[:80] or "(제목 없음)"


def injection_sentences(question: str, context: list[dict]) -> list[dict]:
    """Sentences that read as instructions to the agent (same patterns as the head's flags)."""
    out = []
    for where, text in [("question", question), *((f"context[{i}]", c.get("text") or "") for i, c in enumerate(context))]:
        for chunk in sentences(text):
            if any(p.search(chunk) for p in INJECTION_PATTERNS):
                out.append({"where": where, "text": chunk.strip()[:300]})
    return out[:10]


class IntakeService:
    def __init__(self, store: Store, ask_service, sources):
        self.store, self.ask_service, self.sources = store, ask_service, sources

    def rows(self, url: str) -> list[dict]:
        return self.store.query("SELECT * FROM intake WHERE url=? ORDER BY round", (url,))

    def all(self) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for row in self.store.query("SELECT * FROM intake ORDER BY url, round"):
            out.setdefault(row["url"], []).append(row)
        return out

    def _steps(self, url: str, round_: int) -> dict:
        row = self.store.one("SELECT steps FROM intake WHERE url=? AND round=?", (url, round_))
        return self.store.loads(row["steps"] if row else None, {}) or {}

    def _save_steps(self, url: str, round_: int, steps: dict, **fields) -> None:
        sets = ", ".join(["steps=?", "updated=?", *(f"{k}=?" for k in fields)])
        self.store.execute(f"UPDATE intake SET {sets} WHERE url=? AND round=?",
                           (self.store.dumps(steps), self.store.now(), *fields.values(), url, round_))

    async def handle(self, body) -> dict:
        url, round_ = str(body.url), len(body.feedback) + 1
        item_id, request_id = item_id_for(url), request_id_for(url, round_)
        source = self.sources.match(body.channel, body.target, body.requester)
        audience = source["grade"] if source else body.audience
        route = None
        if source:
            spec = next((t for t in self.ask_service.deps.tasks_catalog() if t.id == source["taskId"]), None)
            if spec is not None:
                route = HeadDecision(spec, spec.agent, "", f"소스 {source['kindLabel']} {source['target']}", "source")
        context = [{"author": (c.author or "")[:200], "text": (c.text or "")[:8000], "at": str(c.at)[:64]}
                   for c in body.context][-50:]
        feedback = [FeedbackItem(draft=(f.draft or "")[:20000], reason=f.reason.strip()[:1000], at=str(f.at)[:64])
                    for f in body.feedback if (f.reason or "").strip()][-20:]
        req = AskRequest(request_id=request_id, question=body.question[:10000] or "(빈 질문)", channel=body.channel,
                         audience=audience, target=url[:500], url=url[:2000], requester=(body.requester or "")[:200],
                         context=[ContextItem(**c) for c in context], feedback=feedback)
        logs.bind(request_id=request_id)
        now = self.store.now()
        steps = {"rag": {"state": "running"}, "verify": {"state": "pending"}}
        self.store.execute(
            "INSERT OR IGNORE INTO intake (url, round, item_id, request_id, channel, audience, grade_source, source_id, "
            "target, requester, question, context, title, status, steps, injection, started, updated) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (url, round_, item_id, request_id, body.channel, audience, "source" if source else "channel",
             source["id"] if source else None, body.target, body.requester or "", body.question, self.store.dumps(context),
             title_for(body.channel, url, context, body.question), "running", self.store.dumps(steps),
             self.store.dumps(injection_sentences(body.question, context)), now, now))

        def emit(kind: str, data: dict) -> None:
            if kind != "stage":
                return
            s = self._steps(url, round_)
            stage = data.get("stage", "")
            if stage == "head":
                s["rag"] = {**s.get("rag", {}), "state": "running", "headMs": data.get("ms"), "task": data.get("task"),
                            "agent": data.get("agent"), "routing": data.get("source"), "outcome": data.get("outcome")}
                if data.get("outcome") == "no_task":
                    s["rag"]["state"], s["verify"] = "done", {"state": "skipped"}
            elif stage.startswith("task:"):
                s["rag"] = {**s.get("rag", {}), "state": "done" if data.get("outcome") == "ok" else "error",
                            "taskMs": data.get("ms"), "chars": data.get("chars"), "citations": data.get("citations") or [],
                            "route": data.get("route")}
                s["verify"] = {"state": "running"}
            elif stage == "censor":
                s["verify"] = {"state": "done", "ms": data.get("ms"), "verdict": data.get("outcome"),
                               "redactions": data.get("redactions") or [], "blockedBy": data.get("blocked_by")}
            self._save_steps(url, round_, s)

        timer = logs.Timer()
        try:
            _, payload = await self.ask_service.submit(req, emit, timeout_seconds=HEAD_TIMEOUT_SECONDS, route=route)
        except Exception:
            s = self._steps(url, round_)
            for key in ("rag", "verify"):
                if s.get(key, {}).get("state") in ("running", "pending"):
                    s[key] = {**s.get(key, {}), "state": "error"}
            self._save_steps(url, round_, s, status="error", finished=self.store.now())
            raise
        refusal = payload.get("refusal")
        task = payload.get("task")
        censor = payload.get("censor") or {}
        s = self._steps(url, round_)
        if refusal and refusal.get("code") == "no_knowledge" and refusal.get("message") == "timeout":
            for key in ("rag", "verify"):
                if s.get(key, {}).get("state") in ("running", "pending"):
                    s[key] = {**s.get(key, {}), "state": "error"}
        elif refusal and s.get("verify", {}).get("state") in ("running", "pending"):
            s["verify"] = {"state": "skipped"}
        if refusal and str(refusal.get("message", "")).startswith("task agent failed"):
            s["rag"] = {**s.get("rag", {}), "state": "error"}
        if censor.get("redactions") and s.get("verify", {}).get("state") == "done":  # counts per rule from the reply
            s["verify"]["counts"] = {r["reason"]: 1 for r in censor["redactions"]}
        text = None
        if not (payload.get("knowledge") or "").strip() and not refusal:  # RFA_module needs a refusal then
            refusal = {"code": "no_knowledge", "message": "empty"}
        if refusal:
            text = REFUSAL_TEXT.get(refusal.get("code"), "답할 수 없습니다")
            if refusal.get("message") == "timeout":
                text = "시간 안에 답할 근거를 찾지 못했습니다"
        self._save_steps(url, round_, s, status="refused" if refusal else "done", finished=self.store.now(),
                         task_id=(task or {}).get("id"), task_name=(task or {}).get("name"), refusal=text,
                         refusal_code=(refusal or {}).get("code"), agent=s.get("rag", {}).get("agent"))
        audit.record(kind="ask", verdict="refused" if refusal else censor.get("verdict", "allow"), action="head-compat",
                     channel=body.channel, profile=censor.get("profile"), session_id=f"ask-{request_id}",
                     detail={"request_id": request_id, "item": item_id, "round": round_, "audience": audience,
                             "grade_source": "source" if source else "channel", "ms": timer.ms,
                             "task": (task or {}).get("id")})
        return {"knowledge": payload.get("knowledge") or "", "task": task, "refusal": text,
                "requestId": request_id, "itemId": item_id, "round": round_, "grade": audience,
                "gradeSource": "source" if source else "channel", "sourceId": source["id"] if source else None,
                "censor": censor}
