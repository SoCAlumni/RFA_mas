from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse

from rfa_mas.nemoclaw.routes._deps import services
from rfa_mas.nemoclaw.schemas.chat import (
    ConversationCreate,
    ConversationDetail,
    ConversationSummary,
)
from rfa_mas.nemoclaw.schemas.common import ErrorOut
from rfa_mas.nemoclaw.services import FrontendServices

router = APIRouter(tags=["frontend"])


def _not_found() -> JSONResponse:
    return JSONResponse(status_code=404, content=ErrorOut(code="unknown_conversation", message="없는 대화입니다.").model_dump())


@router.get("/conversations", operation_id="listConversations", response_model=list[ConversationSummary],
            description="대화 내역(최신순). owner 대화만 저장되므로 guest 는 항상 빈 목록(D-7).")
async def list_conversations(svc: Annotated[FrontendServices, Depends(services)],
                             agentId: Annotated[str | None, Query(max_length=64)] = None,  # noqa: N803
                             authorization: Annotated[str | None, Header()] = None):
    if not svc.me.authenticated(authorization):
        return []
    return [ConversationSummary(**c) for c in svc.conversations.list(agentId)]


@router.post("/conversations", operation_id="createConversation", response_model=ConversationSummary, status_code=201,
             responses={404: {"model": ErrorOut}},
             description="새 대화. owner 는 저장, guest 는 id 만 받는다(persisted=false, 대화는 클라이언트가 들고 `history` 로 보냄).")
async def create_conversation(body: ConversationCreate, svc: Annotated[FrontendServices, Depends(services)],
                              authorization: Annotated[str | None, Header()] = None):
    if svc.agents.get(body.agentId) is None:
        return JSONResponse(status_code=404, content=ErrorOut(code="unknown_agent", message="없는 담당자입니다.").model_dump())
    role = "owner" if svc.me.authenticated(authorization) else "guest"
    return ConversationSummary(**svc.conversations.create(body.agentId, role))


@router.get("/conversations/{conversation_id}", operation_id="getConversation", response_model=ConversationDetail,
            responses={404: {"model": ErrorOut}}, description="대화 다시 열기: 사용자 메시지와 완성된 비서 턴(진행 과정 포함). owner 전용.")
async def get_conversation(conversation_id: str, svc: Annotated[FrontendServices, Depends(services)],
                           authorization: Annotated[str | None, Header()] = None):
    if not svc.me.authenticated(authorization):
        return _not_found()
    conv = svc.conversations.get(conversation_id)
    return ConversationDetail(**conv) if conv else _not_found()
