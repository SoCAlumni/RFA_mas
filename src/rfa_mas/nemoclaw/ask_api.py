"""HTTP for ``ask()``: ``POST /ask`` (bearer), ``GET /ask/{request_id}``, ``POST /chat`` (personal, loopback).

Admission: ``max_inflight`` requests run at once; the next ``max_queue`` wait in a FIFO and get
``202 {status: queued, position}``; beyond that the answer is a ``queue_full`` refusal. Every
outcome is cached by ``request_id`` for ``result_ttl_seconds`` (idempotent re-requests).
"""

from __future__ import annotations

import asyncio
import hmac
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Response
from fastapi.responses import JSONResponse

from rfa_mas.nemoclaw.ask import AskDeps, ask_with_timeout
from rfa_mas.nemoclaw.ask_contract import (
    ASK_CONTRACT_VERSION,
    AskRequest,
    AskResponse,
    ChatRequest,
    ErrorBody,
    QueuedResponse,
    Refusal,
    CensorSummary,
)


@dataclass
class _Entry:
    status: str            # queued | done
    payload: dict | None = None
    created: float = field(default_factory=time.time)


class AskService:
    def __init__(self, deps: AskDeps, token: str | None):
        self.deps = deps
        self.token = token
        self.cfg = deps.config.admission
        self.inflight = 0
        self.queue: deque[str] = deque()
        self.requests: dict[str, AskRequest] = {}
        self.entries: dict[str, _Entry] = {}
        self._wake: asyncio.Event | None = None
        self._worker: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # ---- bookkeeping ----------------------------------------------------------------------

    def _evict(self) -> None:
        cutoff = time.time() - self.cfg.result_ttl_seconds
        for rid in [r for r, e in self.entries.items() if e.status == "done" and e.created < cutoff]:
            self.entries.pop(rid, None)
            self.requests.pop(rid, None)

    def position(self, request_id: str) -> int:
        try:
            return list(self.queue).index(request_id) + 1
        except ValueError:
            return 0

    def _ensure_worker(self) -> None:
        if self._wake is None:
            self._wake = asyncio.Event()
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._worker_loop())

    async def _worker_loop(self) -> None:
        assert self._wake is not None
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self.queue and self.inflight < self.cfg.max_inflight:
                rid = self.queue.popleft()
                asyncio.create_task(self._process(rid))

    def _release(self, *_):
        self.inflight -= 1
        if self._wake is not None:
            self._wake.set()

    async def _process(self, request_id: str) -> dict:
        req = self.requests[request_id]
        self.inflight += 1
        outcome, pending = await ask_with_timeout(req, self.deps, self.cfg.timeout_seconds)
        if pending is not None:
            pending.add_done_callback(self._release)   # slot stays taken until the sandbox turn ends
        else:
            self._release()
        payload = outcome.response.model_dump(mode="json")
        self.entries[request_id] = _Entry("done", payload)
        return payload

    # ---- API ------------------------------------------------------------------------------

    async def submit(self, req: AskRequest) -> tuple[int, dict]:
        self._ensure_worker()
        async with self._lock:
            self._evict()
            entry = self.entries.get(req.request_id)
            if entry is not None:
                return self._view(req.request_id, entry)
            if self.inflight < self.cfg.max_inflight and not self.queue:
                self.requests[req.request_id] = req
                self.entries[req.request_id] = _Entry("running")
                inline = True
            elif len(self.queue) >= self.cfg.max_queue:
                payload = _refusal(req, self.deps, "queue_full", f"admission queue full ({self.cfg.max_queue})")
                self.entries[req.request_id] = _Entry("done", payload)
                return 200, payload
            else:
                self.requests[req.request_id] = req
                self.entries[req.request_id] = _Entry("queued")
                self.queue.append(req.request_id)
                inline = False
        if inline:
            return 200, await self._process(req.request_id)
        assert self._wake is not None
        self._wake.set()
        return 202, QueuedResponse(request_id=req.request_id, position=self.position(req.request_id)).model_dump()

    async def get(self, request_id: str) -> tuple[int, dict]:
        entry = self.entries.get(request_id)
        if entry is None:
            return 404, ErrorBody(code="unknown_request_id").model_dump()
        return self._view(request_id, entry)

    def _view(self, request_id: str, entry: _Entry) -> tuple[int, dict]:
        if entry.status == "done" and entry.payload is not None:
            return 200, entry.payload
        return 202, QueuedResponse(request_id=request_id, position=max(1, self.position(request_id))).model_dump()

    def authorized(self, header: str | None) -> bool:
        if not self.token or not header:
            return False
        return header.lower().startswith("bearer ") and hmac.compare_digest(header[7:].strip(), self.token)


def _refusal(req: AskRequest, deps: AskDeps, code: str, message: str) -> dict:
    profile = deps.config.audiences[req.audience].profile
    return AskResponse(request_id=req.request_id, knowledge="", task=None, refusal=Refusal(code=code, message=message),
                       censor=CensorSummary(profile=profile, verdict="allow", redactions=[])).model_dump(mode="json")


def create_ask_app(service: AskService) -> FastAPI:
    app = FastAPI(
        title="RFA knowledge server — /ask",
        version=ASK_CONTRACT_VERSION,
        summary="desk(대응 에이전트) ↔ 지식 서버 계약",
        description=("head → broker → task agent → censor 를 수행하는 `ask()` 하나를 노출한다. "
                     "`audience` 가 검열 프로파일을 정한다(public→public, company/self→internal). "
                     "슬롯이 없으면 202 로 큐에 넣고 `GET /ask/{request_id}` 로 폴링한다. "
                     "`feedback[].reason` 은 사람 입력으로 신뢰되어 검열 규칙(learned.yaml)으로 되먹임된다. "
                     "인증: `Authorization: Bearer <RFA_ASK_TOKEN>` (.env.dev)."),
    )
    app.state.service = service

    async def authorize(authorization: Annotated[str | None, Header()] = None) -> None:
        if not service.token:
            raise _Http(503, "ask_token_unavailable", "RFA_ASK_TOKEN is not configured on the server")
        if not service.authorized(authorization):
            raise _Http(401, "unauthorized", "bearer token required")

    @app.exception_handler(_Http)
    async def _handle(request, exc: _Http):
        return JSONResponse(status_code=exc.status, content=ErrorBody(code=exc.code, detail=exc.detail).model_dump())

    @app.post("/ask", operation_id="ask", dependencies=[Depends(authorize)], response_model=AskResponse,
              responses={202: {"model": QueuedResponse, "description": "admission queue 대기"},
                         401: {"model": ErrorBody}, 503: {"model": ErrorBody}},
              tags=["ask"])
    async def post_ask(body: AskRequest, response: Response):
        status, payload = await service.submit(body)
        return JSONResponse(status_code=status, content=payload)

    @app.get("/ask/{request_id}", operation_id="getAsk", dependencies=[Depends(authorize)], response_model=AskResponse,
             responses={202: {"model": QueuedResponse}, 404: {"model": ErrorBody}, 401: {"model": ErrorBody}},
             tags=["ask"])
    async def get_ask(request_id: str):
        status, payload = await service.get(request_id)
        return JSONResponse(status_code=status, content=payload)

    @app.post("/chat", operation_id="chat", response_model=AskResponse, tags=["personal"],
              description="개인 채팅(audience self). 진입점은 loopback 전용이라 bearer 없이 같은 ask() 를 호출한다.")
    async def post_chat(body: ChatRequest):
        rid = f"chat-{body.session_id or uuid.uuid4().hex[:10]}-{uuid.uuid4().hex[:6]}"
        req = AskRequest(request_id=rid, question=body.question, channel=body.channel, audience="self",
                         target=f"self:{body.session_id or 'anon'}")
        status, payload = await service.submit(req)
        return JSONResponse(status_code=status, content=payload)

    return app


class _Http(Exception):
    def __init__(self, status: int, code: str, detail: str | None = None):
        self.status, self.code, self.detail = status, code, detail


__all__ = ["AskService", "create_ask_app"]
