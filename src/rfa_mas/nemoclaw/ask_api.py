"""HTTP for ``ask()``: ``POST /ask`` (bearer, synchronous) and ``POST /chat`` (personal, loopback).

Synchronous: a request runs ``ask()`` under the server-side timeout (``server.timeout_seconds``,
180 s) and answers 200 with the outcome or a ``no_knowledge``/"timeout" refusal. Every outcome is
cached by ``request_id`` for ``server.result_ttl_seconds`` (idempotent re-requests: same answer,
no second pipeline run). The admission queue / ``202 queued`` path was retired to ``legacy/``.
"""

from __future__ import annotations

import asyncio
import hmac
import time
import uuid
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, FastAPI, Header
from fastapi.responses import JSONResponse

from rfa_mas.nemoclaw.ask import AskDeps, ask_with_timeout
from rfa_mas.nemoclaw.ask_contract import (
    ASK_CONTRACT_VERSION,
    AskRequest,
    AskResponse,
    ChatRequest,
    ErrorBody,
    TeamCreateRequest,
    TeamList,
    TeamResponse,
)


@dataclass
class _Entry:
    payload: dict | None = None            # None while the request is still running
    created: float = field(default_factory=time.time)
    done: asyncio.Event = field(default_factory=asyncio.Event)


class AskService:
    def __init__(self, deps: AskDeps, token: str | None):
        self.deps = deps
        self.token = token
        self.cfg = deps.config.server
        self.entries: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()

    def _evict(self) -> None:
        cutoff = time.time() - self.cfg.result_ttl_seconds
        for rid in [r for r, e in self.entries.items() if e.payload is not None and e.created < cutoff]:
            self.entries.pop(rid, None)

    async def submit(self, req: AskRequest) -> tuple[int, dict]:
        """Run ``ask()`` synchronously; a repeated ``request_id`` waits for / returns the first outcome."""
        async with self._lock:
            self._evict()
            entry = self.entries.get(req.request_id)
            if entry is None:
                entry = self.entries[req.request_id] = _Entry()
                owner = True
            else:
                owner = False
        if not owner:
            await entry.done.wait()
            return 200, entry.payload or {}
        try:
            outcome, pending = await ask_with_timeout(req, self.deps, self.cfg.timeout_seconds)
            if pending is not None:  # the timed-out sandbox turn keeps running; retrieve its result silently
                pending.add_done_callback(lambda t: t.cancelled() or t.exception())
            entry.payload = outcome.response.model_dump(mode="json")
        except Exception:
            self.entries.pop(req.request_id, None)
            raise
        finally:
            entry.done.set()
        return 200, entry.payload

    def authorized(self, header: str | None) -> bool:
        if not self.token or not header:
            return False
        return header.lower().startswith("bearer ") and hmac.compare_digest(header[7:].strip(), self.token)


def create_ask_app(service: AskService, teams=None, frontend=None) -> FastAPI:
    app = FastAPI(
        title="RFA knowledge server — /ask",
        version=ASK_CONTRACT_VERSION,
        summary="desk(대응 에이전트) ↔ 지식 서버 계약",
        description=("head → broker → task agent → censor 를 수행하는 `ask()` 하나를 노출한다. "
                     "`audience` 가 검열 프로파일을 정한다(public→public, company/self→internal). "
                     "동기 응답(서버 타임아웃 180초 → refusal no_knowledge/timeout). 같은 request_id 는 캐시 응답. "
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
              responses={401: {"model": ErrorBody}, 503: {"model": ErrorBody}}, tags=["ask"])
    async def post_ask(body: AskRequest):
        status, payload = await service.submit(body)
        return JSONResponse(status_code=status, content=payload)

    @app.post("/chat", operation_id="chat", response_model=AskResponse, tags=["personal"],
              description="개인 채팅(audience self). 진입점은 loopback 전용이라 bearer 없이 같은 ask() 를 호출한다.")
    async def post_chat(body: ChatRequest):
        rid = f"chat-{body.session_id or uuid.uuid4().hex[:10]}-{uuid.uuid4().hex[:6]}"
        req = AskRequest(request_id=rid, question=body.question, channel=body.channel, audience="self",
                         target=f"self:{body.session_id or 'anon'}")
        status, payload = await service.submit(req)
        return JSONResponse(status_code=status, content=payload)

    if teams is not None:
        @app.post("/teams", operation_id="createTeam", dependencies=[Depends(authorize)], response_model=TeamResponse,
                  status_code=201, tags=["teams"],
                  responses={200: {"model": TeamResponse, "description": "같은 task_id 의 기존 팀 (멱등)"},
                             409: {"model": ErrorBody}, 422: {"model": ErrorBody}, 401: {"model": ErrorBody}},
                  description=("요구사항(자연어 name/description) → 승인된 역할 카탈로그(roles.yaml) 안에서 패터닝 → 기본 샌드박스에 "
                               "supervisor(secondary) + 멤버를 선언(teams.yaml)·적용(agents apply)·시드하고 /ask 카탈로그에 등록한다."))
        async def post_team(body: TeamCreateRequest):
            status, payload = await teams.create(name=body.name, description=body.description, task_id=body.task_id,
                                                 sandbox=body.sandbox)
            return JSONResponse(status_code=status, content=payload)

        @app.get("/teams", operation_id="listTeams", dependencies=[Depends(authorize)], response_model=TeamList, tags=["teams"])
        async def list_teams():
            return JSONResponse(content={"teams": [teams._view(t) for t in teams.list()]})

        @app.get("/teams/{team_id}", operation_id="getTeam", dependencies=[Depends(authorize)], response_model=TeamResponse,
                 responses={404: {"model": ErrorBody}}, tags=["teams"])
        async def get_team(team_id: str):
            decl = teams.get(team_id)
            if decl is None:
                return JSONResponse(status_code=404, content=ErrorBody(code="unknown_team").model_dump())
            return JSONResponse(content=teams._view(decl))

        @app.delete("/teams/{team_id}", operation_id="removeTeam", dependencies=[Depends(authorize)], status_code=202,
                    responses={404: {"model": ErrorBody}}, tags=["teams"])
        async def delete_team(team_id: str):
            status, payload = await teams.remove(team_id)
            return JSONResponse(status_code=status, content=payload)

    if frontend is not None:  # front-end contract: routes/ (HTTP) → services/ → store/
        from rfa_mas.nemoclaw.routes import mount_frontend

        mount_frontend(app, frontend)
    return app


class _Http(Exception):
    def __init__(self, status: int, code: str, detail: str | None = None):
        self.status, self.code, self.detail = status, code, detail


__all__ = ["AskService", "create_ask_app"]
