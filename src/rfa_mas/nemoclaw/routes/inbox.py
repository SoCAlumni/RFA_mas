from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse, Response

from rfa_mas.nemoclaw.routes._deps import services
from rfa_mas.nemoclaw.schemas.common import ErrorOut
from rfa_mas.nemoclaw.schemas.inbox import (
    InboxDetail,
    InboxItem,
    InboxSummary,
    RegenerateIn,
    RespondIn,
    SourceIn,
    SourceOut,
)
from rfa_mas.nemoclaw.services import FrontendServices
from rfa_mas.nemoclaw.services.approvals import ApprovalsError
from rfa_mas.nemoclaw.services.inbox import InboxError
from rfa_mas.nemoclaw.services.sources import SourceError

router = APIRouter(tags=["inbox"])
ERRORS = {403: {"model": ErrorOut}, 404: {"model": ErrorOut}, 409: {"model": ErrorOut}, 422: {"model": ErrorOut},
          502: {"model": ErrorOut}, 503: {"model": ErrorOut}}
Svc = Annotated[FrontendServices, Depends(services)]
Auth = Annotated[str | None, Header()]


def _err(exc) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content=ErrorOut(code=exc.code, message=exc.message).model_dump())


async def _run(coro):
    try:
        return await coro
    except (InboxError, ApprovalsError, SourceError) as exc:
        return _err(exc)


@router.get("/inbox", operation_id="inboxList", response_model=list[InboxItem], responses=ERRORS,
            description="결재함 목록: RFA_module 결재(:8790) + rfa_mas 접수 기록(작성 중 포함), 최신순. "
                        "guest 는 사외 항목만(D-23). status: all | needs_approval | drafting | pending | regenerating | "
                        "posting | publish_failed | posted | closed.")
async def inbox_list(svc: Svc, task: Annotated[str | None, Query(max_length=64)] = None,
                     status: Annotated[str | None, Query(max_length=32)] = None, authorization: Auth = None):
    return await _run(svc.inbox.list(authenticated=svc.me.authenticated(authorization), task=task, status=status))


@router.get("/inbox/summary", operation_id="inboxSummary", response_model=InboxSummary, responses=ERRORS,
            description="결재 필요 수(결재함 배지·「결재 필요 N」), 태스크별 결재 필요 수, 상태별 수.")
async def inbox_summary(svc: Svc, authorization: Auth = None):
    return await _run(svc.inbox.summary(authenticated=svc.me.authenticated(authorization)))


@router.get("/inbox/{item_id}", operation_id="inboxDetail", response_model=InboxDetail, responses=ERRORS,
            description="결재 상세: 원본 화면, 주입 의심 문장, 초안 카드(단계 RAG 검색·검증·LLM 초안, 재생성 이력, 게시 결과), 기록.")
async def inbox_detail(item_id: str, svc: Svc, authorization: Auth = None):
    return await _run(svc.inbox.detail(item_id, authenticated=svc.me.authenticated(authorization)))


@router.post("/inbox/{item_id}/respond", operation_id="inboxRespond", response_model=InboxDetail, responses=ERRORS,
             description="바로 응답 = RFA_module approve(게시). owner. 화면의 초안이 저장된 초안과 다르면 409 "
                         "`draft_edit_unsupported`(D-22). 게시 실패는 502, 다시 부르면 재시도.")
async def inbox_respond(item_id: str, svc: Svc, body: RespondIn | None = None, authorization: Auth = None):
    return await _run(svc.inbox.respond(item_id, (body or RespondIn()).draft,
                                        authenticated=svc.me.authenticated(authorization)))


@router.post("/inbox/{item_id}/regenerate", operation_id="inboxRegenerate", response_model=InboxDetail,
             responses=ERRORS, description="재생성 요청 = RFA_module reject(사유 = 요청 내용). owner. 대응 에이전트가 다시 써서 "
                                           "round+1 로 올린다. 3번째 요청이면 결재가 닫힌다(응답하지 않기).")
async def inbox_regenerate(item_id: str, body: RegenerateIn, svc: Svc, authorization: Auth = None):
    return await _run(svc.inbox.regenerate(item_id, body.request, authenticated=svc.me.authenticated(authorization)))


# ---- 소스 ------------------------------------------------------------------------------------------


@router.get("/tasks/{task_id}/sources", operation_id="listTaskSources", response_model=list[SourceOut],
            responses=ERRORS, tags=["inbox"], description="태스크 에이전트의 소스(문의가 들어오는 곳).")
async def list_sources(task_id: str, svc: Svc):
    try:
        return svc.sources.list(task_id)
    except SourceError as exc:
        return _err(exc)


@router.post("/tasks/{task_id}/sources", operation_id="addTaskSource", response_model=SourceOut, status_code=201,
             responses=ERRORS, description="소스 연결(owner): GitHub `owner/repo` 또는 Slack `#채널`·`DM · 팀이름`, 받을 범위, "
                                           "기본 등급. 이 소스로 들어온 요청은 이 등급·태스크를 쓴다(D-24).")
async def add_source(task_id: str, body: SourceIn, svc: Svc, authorization: Auth = None):
    try:
        return svc.sources.add(task_id, kind=body.kind, target=body.target, scopes=body.scopes, grade=body.grade,
                               authenticated=svc.me.authenticated(authorization))
    except SourceError as exc:
        return _err(exc)


@router.delete("/tasks/{task_id}/sources/{source_id}", operation_id="removeTaskSource", status_code=204,
               responses=ERRORS, description="소스 떼기(owner).")
async def remove_source(task_id: str, source_id: str, svc: Svc, authorization: Auth = None):
    try:
        svc.sources.remove(task_id, source_id, svc.me.authenticated(authorization))
    except SourceError as exc:
        return _err(exc)
    return Response(status_code=204)


