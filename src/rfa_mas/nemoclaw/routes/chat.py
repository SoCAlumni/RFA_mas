from __future__ import annotations

import json
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse

from rfa_mas.nemoclaw import logs
from rfa_mas.nemoclaw.routes._deps import services
from rfa_mas.nemoclaw.schemas.chat import CHAT_EVENTS, ChatStreamRequest
from rfa_mas.nemoclaw.schemas.common import ErrorOut, SseEnvelope
from rfa_mas.nemoclaw.services import FrontendServices

router = APIRouter(tags=["frontend"])


def frame(env: SseEnvelope) -> bytes:
    return f"id: {env.seq}\nevent: {env.type}\ndata: {json.dumps(env.model_dump(), ensure_ascii=False)}\n\n".encode()


async def sse(stream, run_id: str | None = None):
    """``(type, data)`` tuples → SSE frames in the ``{type, runId, seq, ts, data}`` envelope."""
    seq = 0
    async for kind, data in stream:
        run_id = run_id or data.get("runId") or logs.context().get("run_id") or "run"
        yield frame(SseEnvelope(type=kind, runId=run_id, seq=seq, ts=round(time.time(), 3), data=data))
        seq += 1


SSE_HEADERS = {"cache-control": "no-cache", "x-accel-buffering": "no"}


@router.post("/chat", operation_id="chatStream", status_code=200,
             description="검색하거나 물어보기 SSE. 담당자 탐색(점수 ≥ head.select_threshold 전부 병렬 위임) → 위임 → 합치기/직접 답변 → "
                         "등급 검사(owner 사내, guest 사외) → 답변. 봉투 `{type, runId, seq, ts, data}`; type: "
                         + ", ".join(f"`{k}` {v}" for k, v in CHAT_EVENTS.items()),
             responses={200: {"content": {"text/event-stream": {"schema": SseEnvelope.model_json_schema()}},
                              "description": "SSE stream of SseEnvelope frames"},
                        409: {"model": ErrorOut, "description": "a run is already streaming in this conversation"}})
async def chat_stream(body: ChatStreamRequest, request: Request, svc: Annotated[FrontendServices, Depends(services)],
                      authorization: Annotated[str | None, Header()] = None):
    if svc.chat.is_running(body.conversationId):
        return JSONResponse(status_code=409, content=ErrorOut(code="conversation_busy",
                                                              message="이 대화에서 아직 답변 중입니다.").model_dump())
    stream = svc.chat.stream(text=body.text, role=body.role, conversation_id=body.conversationId, agent_id=body.agentId,
                             history=body.history, authorization=authorization,
                             request_id=logs.context().get("request_id") or request.headers.get("x-request-id"))
    return StreamingResponse(sse(stream), media_type="text/event-stream", headers=SSE_HEADERS)
