from __future__ import annotations

import json
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse

from rfa_mas.nemoclaw import logs
from rfa_mas.nemoclaw.routes._deps import services
from rfa_mas.nemoclaw.schemas.chat import CHAT_EVENTS, ChatStreamRequest
from rfa_mas.nemoclaw.schemas.common import SseEnvelope
from rfa_mas.nemoclaw.services import FrontendServices

router = APIRouter(tags=["frontend"])


def frame(env: SseEnvelope) -> bytes:
    return f"id: {env.seq}\nevent: {env.type}\ndata: {json.dumps(env.model_dump(), ensure_ascii=False)}\n\n".encode()


@router.post("/chat", operation_id="chatStream", status_code=200,
             description="개인 채팅 SSE. 이벤트 봉투 `{type, runId, seq, ts, data}`; type: "
                         + ", ".join(f"`{k}` {v}" for k, v in CHAT_EVENTS.items()),
             responses={200: {"content": {"text/event-stream": {"schema": SseEnvelope.model_json_schema()}},
                              "description": "SSE stream of SseEnvelope frames"}})
async def chat_stream(body: ChatStreamRequest, request: Request, svc: Annotated[FrontendServices, Depends(services)],
                      authorization: Annotated[str | None, Header()] = None):
    async def events():
        seq = 0
        run_id = None
        async for kind, data in svc.chat.stream(text=body.text, role=body.role, conversation_id=body.conversationId,
                                                history=body.history, authorization=authorization,
                                                request_id=logs.context().get("request_id")
                                                or request.headers.get("x-request-id")):
            run_id = run_id or logs.context().get("run_id") or "run"
            yield frame(SseEnvelope(type=kind, runId=run_id, seq=seq, ts=round(time.time(), 3), data=data))
            seq += 1

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"cache-control": "no-cache", "x-accel-buffering": "no"})
