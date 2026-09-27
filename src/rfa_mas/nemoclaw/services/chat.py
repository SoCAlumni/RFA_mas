"""Personal chat: one question → rank the tasks → delegate to every task agent scoring ≥ the threshold in
parallel (D-12; a pinned ``agentId`` skips ranking, D-5) → compose the answer from the replies only, or
answer directly without inventing company facts (D-13) → grade check (censor) → sentence deltas.

The grade (사내/사외) belongs to the question, i.e. to the asker's role: owner → audience ``self``
(internal), guest → ``public`` (D-0.4). Progress text that comes from an agent's raw reply is shown to
the owner only; a guest sees fixed phrases (D-14). Events are plain ``(type, data)`` tuples;
``routes/chat.py`` wraps them in the SSE envelope."""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import AsyncIterator, Callable

from rfa_mas.nemoclaw import audit, logs
from rfa_mas.nemoclaw.ask import AGENT_FAILURE_TEXTS, NO_EVIDENCE, TaskReply, _constraints
from rfa_mas.nemoclaw.ask_contract import ContextItem
from rfa_mas.nemoclaw.rank import Candidate, Composer, Ranker, no_answer, no_evidence_answer
from rfa_mas.nemoclaw.services.agents import ASSISTANT_ID
from rfa_mas.nemoclaw.services.conversations import ConversationService, TurnBuilder

ROLE_AUDIENCE = {"owner": "self", "guest": "public"}   # the only role → audience mapping
ROLE_LEVEL = {"owner": "company", "guest": "public"}   # guard level shown as 사내 / 사외
SHOW_SCORE = 0.3
GATEWAY_EMPTY = {*AGENT_FAILURE_TEXTS, "(empty reply)"}                                       # candidates below this are not shown at all
# a sentence ends at .!?。 followed by whitespace/end (so 88.5 or 0.7 never split), or at a newline
_SENTENCE = re.compile(r".+?(?:(?<!\d)[.!?。](?=\s|$)\n*|\n+|$)", re.DOTALL)  # "2. 결과" headings do not end a sentence


def sentences(text: str) -> list[str]:
    """Sentence-ish chunks for pseudo streaming (keeps trailing punctuation/newlines)."""
    out = []
    for m in _SENTENCE.finditer(text):
        chunk = m.group(0)
        if chunk.strip():
            out.append(chunk)
    return out


def first_sentence(text: str, limit: int = 140) -> str:
    chunks = sentences(" ".join(text.split()))
    head = chunks[0].strip() if chunks else ""
    return head if len(head) <= limit else head[: limit - 1] + "…"


class ChatError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class ChatService:
    def __init__(self, ask_service, me, *, tasks, conversations: ConversationService, ranker: Ranker,
                 composer: Composer):
        self.ask_service, self.me, self.tasks, self.conversations = ask_service, me, tasks, conversations
        self.ranker, self.composer = ranker, composer
        self.running: dict[str, str] = {}   # conversation id → run id (one run per conversation)

    def audience(self, role: str) -> str:
        return ROLE_AUDIENCE[role]

    def is_running(self, conversation_id: str | None) -> bool:
        return bool(conversation_id) and conversation_id in self.running

    # ---- stream -------------------------------------------------------------------------------

    async def stream(self, *, text: str, role: str, conversation_id: str | None, agent_id: str | None,
                     history: list, authorization: str | None,
                     request_id: str | None = None) -> AsyncIterator[tuple[str, dict]]:
        role = self.me.clamp_role(role, authorization)
        agent_id = None if agent_id in (None, "", ASSISTANT_ID) else agent_id
        audience = self.audience(role)
        profile = self.ask_service.deps.config.audiences[audience].profile
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        message_id = f"a_{run_id[4:]}"
        logs.bind(run_id=run_id, role=role, audience=audience, profile=profile)
        persisted = role == "owner" and bool(conversation_id)
        if persisted:
            if self.conversations.owner_of(conversation_id) is None:
                self.conversations.ensure(conversation_id, agent_id or ASSISTANT_ID)
            turns = self.conversations.history(conversation_id)
            self.conversations.add_user(conversation_id, text)
        else:
            turns = [{"role": h.role, "text": h.text} for h in history]
        context = [ContextItem(author=t["role"], text=t["text"]) for t in turns]
        if conversation_id:
            self.running[conversation_id] = run_id
        builder = TurnBuilder(message_id)
        queue: asyncio.Queue = asyncio.Queue()
        timer = logs.Timer()

        def emit(kind: str, data: dict) -> None:
            queue.put_nowait((kind, data))

        async def produce():
            try:
                await self._run(text=text, role=role, agent_id=agent_id, context=context, run_id=run_id, emit=emit)
                queue.put_nowait(("_end", {"status": "done"}))
            except ChatError as exc:
                queue.put_nowait(("_end", {"status": "error", "error": exc.message, "code": exc.code}))
            except Exception as exc:  # surfaced as run.end error, never a broken stream
                logs.error("sse", action="error", exception=type(exc).__name__)
                queue.put_nowait(("_end", {"status": "error", "error": f"내부 오류({type(exc).__name__})",
                                           "code": "chat_failed"}))

        logs.event("sse", action="start", conversation_id=conversation_id, agent=agent_id, history=len(context))
        producer = asyncio.create_task(produce())
        status, error = "stopped", None
        try:
            start = {"runId": run_id, "conversationId": conversation_id, "messageId": message_id, "role": role,
                     "agentId": agent_id, "level": ROLE_LEVEL[role], "requestId": request_id or f"chat-{run_id}"}
            builder.apply("run.start", start)
            yield "run.start", start
            while True:
                kind, data = await queue.get()
                if kind == "_end":
                    status, error = data["status"], data.get("error")
                    end = {"status": status, "durationMs": timer.ms, "messageId": message_id,
                           **({"error": error, "code": data.get("code")} if error else {})}
                    builder.apply("run.end", end)
                    yield "run.end", end
                    break
                builder.apply(kind, data)
                yield kind, data
        finally:
            if not producer.done():
                producer.cancel()
            builder.end(status, timer.ms, error)
            if persisted:
                self.conversations.add_turn(conversation_id, builder.turn)
            if conversation_id and self.running.get(conversation_id) == run_id:
                del self.running[conversation_id]
            logs.event("sse", action="end" if status != "stopped" else "stop", status=status, ms=timer.ms)

    # ---- one run ------------------------------------------------------------------------------

    def _catalog(self) -> tuple[list, dict[str, dict]]:
        """The head's catalogue with each task's tags (and its name, when it has tags) as routing keywords."""
        views = {t["id"]: t for t in self.tasks.list()}
        catalog = []
        for spec in self.ask_service.deps.tasks_catalog():
            view = views.get(spec.id) or {}
            extra = [*view.get("tags", []), *([view["name"]] if view.get("tags") else [])]
            catalog.append(spec.model_copy(update={"keywords": list(dict.fromkeys([*spec.keywords, *extra]))}))
        return catalog, views

    async def _run(self, *, text: str, role: str, agent_id: str | None, context: list[ContextItem], run_id: str,
                   emit: Callable[[str, dict], None]) -> None:
        deps = self.ask_service.deps
        cfg = deps.config
        audience = self.audience(role)
        spec = cfg.audiences[audience]
        profile, channel = spec.profile, spec.channel
        owner = role == "owner"
        catalog, views = self._catalog()

        def name_of(task_id: str) -> str:
            return (views.get(task_id) or {}).get("agentName") or task_id

        # 1) find the agents — pinned (D-5) or ranked (D-12)
        timer = logs.Timer()
        if agent_id:
            task = next((t for t in catalog if t.id == agent_id), None)
            if task is None:
                view = self.tasks.get(agent_id)
                if view and view["status"] == "applying":
                    raise ChatError("agent_not_ready", f"{view['agentName']}는 아직 만드는 중입니다. 잠시 뒤 다시 물어봐 주세요.")
                raise ChatError("unknown_agent", "없는 담당자입니다.")
            candidates = [Candidate(task, 1.0, "지정한 담당자", text)]
            selected, source = candidates, "pinned"
        else:
            candidates = await self.ranker.rank(text, context, catalog)
            selected = [c for c in candidates if c.score >= cfg.head.select_threshold][: cfg.head.max_parallel]
            source = self.ranker.source
        shown = [c for c in candidates if c.score >= SHOW_SCORE or c in selected]
        logs.stage("rank", timer.ms, f"{len(selected)} selected", source=source,
                   candidates=[(c.task.id, c.score) for c in shown])
        names = [name_of(c.task.id) for c in selected]
        if not selected:
            preface = "이 질문을 맡은 담당자가 없어 직접 답하겠습니다."
        elif len(selected) == 1:
            preface = f"{names[0]}에게 확인하겠습니다."
        else:
            preface = f"담당자 {len(selected)}명({', '.join(names)})에게 나눠 확인하겠습니다."
        emit("assistant.delta", {"text": preface})
        emit("agents.search", {"query": text.strip()[:40],
                               "candidates": [{"agentId": c.task.id, "reason": c.reason, "score": c.score} for c in shown]})
        emit("agents.select", {"selected": [{"agentId": c.task.id, "task": c.query} for c in selected],
                               "skipped": [{"agentId": c.task.id, "reason": c.reason} for c in shown if c not in selected]})

        # 2) delegate in parallel
        head_reasons = deps.learned.reasons(audience, any_task=True)

        async def call(n: int, cand: Candidate) -> tuple[Candidate, str, str]:
            call_id, task = f"c{n + 1}", cand.task
            sid = f"chat-{run_id}-{task.id}"
            audit.remember_session(sid, channel, profile)
            emit("delegate.start", {"callId": call_id, "agentId": task.id, "task": cand.query})
            emit("delegate.log", {"callId": call_id, "text": f"{task.agent} 에이전트에 위임" if owner else "담당 에이전트에 위임"})
            t = logs.Timer()
            try:
                reply = await asyncio.wait_for(deps.tasks.ask(task.agent, cand.query + _constraints(head_reasons), sid,
                                                              channel, task.id), cfg.server.timeout_seconds)
            except TimeoutError:
                reply = TaskReply("", False, {"error": "timeout"})
            body = (reply.text or "").strip()
            if reply.ok and body in GATEWAY_EMPTY:  # the gateway's text when the agent produced no answer
                reply = TaskReply("", False, {**reply.detail, "error": f"빈 응답({body})"})
                body = ""
            if not reply.ok:
                status, summary = "error", "응답하지 못했습니다"
                err = str(reply.detail.get("error") or "error")
                emit("delegate.log", {"callId": call_id, "text": f"실패 · {err[:80]}" if owner else "실패"})
            elif (not body or body == "(empty reply)" or body.startswith("NO_EVIDENCE")  # team supervisor: members found nothing
                  or (len(body) < 200 and NO_EVIDENCE.search(body))):
                status, summary = "none", "근거를 찾지 못했습니다"
                emit("delegate.log", {"callId": call_id, "text": "근거 없음"})
            else:
                status = "ok"
                summary = first_sentence(body) if owner else f"응답 수신 · {len(body)}자"
                route = reply.detail.get("route")
                emit("delegate.log", {"callId": call_id,
                                      "text": f"응답 수신 · {len(body)}자" + (f" · {route}" if owner and route else "")})
            logs.stage(f"task:{task.id}", t.ms, status, agent=task.agent, route=reply.detail.get("route"), chars=len(body))
            logs.raw("task_reply", body, task=task.id)
            emit("delegate.end", {"callId": call_id, "status": status, "summary": summary, "refs": [],
                                  "durationMs": t.ms})
            return cand, status, body

        results = await asyncio.gather(*(call(n, c) for n, c in enumerate(selected)))

        # 3) compose — replies only, never invented facts (D-13)
        ok = [(name_of(c.task.id), body) for c, status, body in results if status == "ok"]
        timer = logs.Timer()
        if not selected:
            answer, mode = await self.composer.direct(text, context), "direct"
        elif not ok:
            failed = all(status == "error" for _, status, _ in results)
            answer, mode = (no_answer(names), "no_answer") if failed else (no_evidence_answer(names), "no_evidence")
        elif len(ok) == 1:
            answer, mode = ok[0][1], "single"
        else:
            answer, mode = await self.composer.merge(text, ok), "merge"
        logs.stage("compose", timer.ms, mode, replies=len(ok))

        # 4) grade check
        level = ROLE_LEVEL[role]
        emit("guard.start", {"level": level})
        hints = list(dict.fromkeys(r for c in selected for r in deps.learned.reasons(audience, c.task.id))) \
            or deps.learned.reasons(audience, None)
        timer = logs.Timer()
        result = await asyncio.to_thread(deps.pipeline.run, answer, profile, None, hints)
        logs.stage("censor", timer.ms, result.verdict, redactions=list(result.redactions), blocked_by=result.blocked_by)
        count = sum(result.redactions.values())
        if result.verdict == "block":
            guard = {"status": "blocked", "note": "등급 검사에서 답변이 차단되었습니다", "redactions": count}
            final = "등급 검사에서 답변이 차단되어 보여 드릴 수 없습니다."
        elif count:
            why = f" ({', '.join(result.redactions)})" if owner else ""
            guard = {"status": "redacted", "note": f"{count}건을 답변에서 제외했습니다{why}", "redactions": count}
            final = result.text
        else:
            guard = {"status": "pass", "note": "제외한 내용이 없습니다", "redactions": 0}
            final = result.text
        emit("guard.end", guard)
        for chunk in sentences(final):
            emit("answer.delta", {"text": chunk})
        audit.record(kind="chat", verdict=result.verdict, action="chat", channel=channel, profile=profile,
                     agent=",".join(c.task.agent for c in selected) or None, session_id=run_id,
                     detail={"source": source, "selected": [c.task.id for c in selected], "compose": mode,
                             "calls": {c.task.id: s for c, s, _ in results}, "redactions": result.redactions,
                             "audience": audience})
