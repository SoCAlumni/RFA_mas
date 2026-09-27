"""Personal chat: the same ``ask()`` as ``/ask``; role → audience mapped here and nowhere else.
Streams progress (``stage``), the guest guard verdict, sentence deltas of the censored answer, and
``done`` — as plain event tuples; ``routes/chat.py`` wraps them in the SSE envelope."""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import AsyncIterator

from rfa_mas.nemoclaw import logs
from rfa_mas.nemoclaw.ask_contract import AskRequest, ContextItem

ROLE_AUDIENCE = {"owner": "self", "guest": "public"}   # the only role → audience mapping
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


class ChatService:
    def __init__(self, ask_service, me):
        self.ask_service, self.me = ask_service, me

    def audience(self, role: str) -> str:
        return ROLE_AUDIENCE[role]

    async def stream(self, *, text: str, role: str, conversation_id: str | None, history: list,
                     authorization: str | None, request_id: str | None = None) -> AsyncIterator[tuple[str, dict]]:
        role = self.me.clamp_role(role, authorization)
        audience = self.audience(role)
        profile = self.ask_service.deps.config.audiences[audience].profile
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        req_id = request_id or f"chat-{run_id}"
        req = AskRequest(request_id=f"chat-{run_id}", question=text, channel="web", audience=audience,
                         target=f"chat:{conversation_id or 'anon'}",
                         context=[ContextItem(author=h.role, text=h.text) for h in history])
        logs.bind(run_id=run_id, role=role, audience=audience, profile=profile)
        queue: asyncio.Queue = asyncio.Queue()
        timer = logs.Timer()

        async def produce():
            try:
                _, payload = await self.ask_service.submit(req, emit=lambda t, d: queue.put_nowait((t, d)))
                queue.put_nowait(("_done", payload))
            except Exception as exc:  # surfaced as an error event, never a broken stream
                logs.error("sse", action="error", exception=type(exc).__name__)
                queue.put_nowait(("_error", {"code": "chat_failed", "message": type(exc).__name__}))

        producer = asyncio.create_task(produce())
        logs.event("sse", action="start", conversation_id=conversation_id, history=len(history))
        yield "run.started", {"role": role, "audience": audience, "profile": profile,
                              "conversationId": conversation_id, "requestId": req_id}
        try:
            while True:
                kind, data = await queue.get()
                if kind == "_error":
                    yield "error", data
                    break
                if kind != "_done":
                    yield kind, data
                    continue
                payload = data
                censor = payload.get("censor") or {}
                refusal = payload.get("refusal")
                if role == "guest":
                    yield "guard.final", {"verdict": "block" if refusal else censor.get("verdict", "allow"),
                                          "redactions": [r["reason"] for r in censor.get("redactions", [])],
                                          "blocked": bool(refusal), **({"reason": refusal["code"]} if refusal else {})}
                for chunk in sentences(payload.get("knowledge") or ""):
                    yield "delta", {"text": chunk}
                yield "done", {"requestId": payload.get("request_id"), "task": payload.get("task"), "refusal": refusal,
                               "censor": censor, "ms": timer.ms}
                break
        finally:
            if not producer.done():
                producer.cancel()
                logs.event("sse", action="stop", ms=timer.ms)
            else:
                logs.event("sse", action="end", ms=timer.ms)
