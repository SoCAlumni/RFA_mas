from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse

from rfa_mas.nemoclaw.routes._deps import services
from rfa_mas.nemoclaw.schemas.admin import (
    AdminAgentDetail,
    AdminAgentList,
    AgentActionResult,
    PromptUpdate,
    PromptUpdateResult,
    PromptView,
    SandboxDetail,
    SandboxList,
    SandboxPatch,
    SandboxUpdateResult,
    SourceToggle,
    SourceToggleResult,
)
from rfa_mas.nemoclaw.schemas.common import ErrorOut
from rfa_mas.nemoclaw.services import FrontendServices
from rfa_mas.nemoclaw.services.admin import AdminError

router = APIRouter(tags=["admin"], prefix="/admin")
ERRORS = {403: {"model": ErrorOut}, 404: {"model": ErrorOut}, 409: {"model": ErrorOut}, 422: {"model": ErrorOut},
          502: {"model": ErrorOut}}
Svc = Annotated[FrontendServices, Depends(services)]
Auth = Annotated[str | None, Header()]


def _err(exc: AdminError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content=ErrorOut(code=exc.code, message=exc.message).model_dump())


async def _call(fn, *args, **kwargs):
    """Service calls may run a sandbox CLI command: off the event loop, errors as ErrorOut."""
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except AdminError as exc:
        return _err(exc)


# ---- agents ---------------------------------------------------------------------------------------


@router.get("/agents", operation_id="adminListAgents", response_model=AdminAgentList,
            description="에이전트 목록: 백엔드 에이전트 전부, 태스크 에이전트(태스크 담당·팀 supervisor·멤버) 먼저, 관리 에이전트"
                        "(assistant·censor) 뒤(읽기 전용). `오늘 N회` 는 감사 기록의 LLM 호출 수. 사이드바 「에이전트 N」 = counts.total.")
async def list_agents(svc: Svc):
    return await _call(svc.admin.list_agents)


@router.get("/agents/{agent_id}", operation_id="adminGetAgent", response_model=AdminAgentDetail, responses=ERRORS,
            description="에이전트 상세: 컨텍스트(마지막 LLM 호출의 프록시 실측), 불러오는 자료, 오늘 호출 통계와 최근 7일.")
async def get_agent(agent_id: str, svc: Svc):
    return await _call(svc.admin.agent, agent_id)


@router.post("/agents/{agent_id}/compact", operation_id="adminCompactAgent", response_model=AgentActionResult,
             responses=ERRORS, description="대화 압축: 가장 최근 대화 기록만 남기고 이전 세션 기록을 지운다. owner, 태스크 에이전트만.")
async def compact(agent_id: str, svc: Svc, authorization: Auth = None):
    return await _call(svc.admin.session_action, agent_id, "compact", svc.me.authenticated(authorization))


@router.post("/agents/{agent_id}/clear-memory", operation_id="adminClearAgentMemory", response_model=AgentActionResult,
             responses=ERRORS, description="기억 비우기: 세션 기록과 워크스페이스 기억(MEMORY.md, memory/)을 지운다. owner, 태스크 에이전트만.")
async def clear_memory(agent_id: str, svc: Svc, authorization: Auth = None):
    return await _call(svc.admin.session_action, agent_id, "clear-memory", svc.me.authenticated(authorization))


@router.get("/agents/{agent_id}/prompt", operation_id="adminGetAgentPrompt", response_model=PromptView,
            responses=ERRORS, description="시스템 프롬프트 보기: IDENTITY.md(서명 마커 가림)·스킬·운영자 추가 지시.")
async def get_prompt(agent_id: str, svc: Svc):
    return await _call(svc.admin.prompt, agent_id)


@router.put("/agents/{agent_id}/prompt", operation_id="adminSetAgentPrompt", response_model=PromptUpdateResult,
            responses=ERRORS, description="운영자 추가 지시 저장 → IDENTITY.md 다시 올림(다음 대화부터). owner, 태스크 에이전트만.")
async def set_prompt(agent_id: str, body: PromptUpdate, svc: Svc, authorization: Auth = None):
    return await _call(svc.admin.set_prompt, agent_id, body.instructions, svc.me.authenticated(authorization))


@router.patch("/agents/{agent_id}/sources/{source_id}", operation_id="adminToggleAgentSource",
              response_model=SourceToggleResult, responses=ERRORS,
              description="불러오는 자료 켜기/끄기 → IDENTITY.md 에 반영(지시 수준). 사내 지식 경로가 없는 구획이면 409.")
async def toggle_source(agent_id: str, source_id: str, body: SourceToggle, svc: Svc, authorization: Auth = None):
    return await _call(svc.admin.set_source, agent_id, source_id, body.enabled, svc.me.authenticated(authorization))


# ---- sandboxes ------------------------------------------------------------------------------------


@router.get("/sandboxes", operation_id="adminListSandboxes", response_model=SandboxList,
            description="샌드박스 목록(assignments.yaml 선언 + 실시간 상태). 데모 한도 limit=2.")
async def list_sandboxes(svc: Svc):
    return await _call(svc.admin.list_sandboxes)


@router.post("/sandboxes", operation_id="adminAddSandbox", status_code=201, responses=ERRORS,
             description="샌드박스 추가: 데모 한도(2개) 때문에 항상 거절(409 sandbox_limit, 안내 문구 포함).")
async def add_sandbox(svc: Svc, authorization: Auth = None):
    return await _call(svc.admin.add_sandbox, svc.me.authenticated(authorization))


@router.get("/sandboxes/{sandbox_id}", operation_id="adminGetSandbox", response_model=SandboxDetail, responses=ERRORS,
            description="샌드박스 상세: 보안 그룹, LLM 추론(제공자·모델·컨텍스트 길이), 게이트웨이.")
async def get_sandbox(sandbox_id: str, svc: Svc):
    return await _call(svc.admin.sandbox, sandbox_id)


@router.patch("/sandboxes/{sandbox_id}", operation_id="adminUpdateSandbox", response_model=SandboxUpdateResult,
              responses=ERRORS, description="변경 적용: provider/model 은 바로 적용(모든 샌드박스가 같은 프록시 경로를 쓴다), "
                                            "contextLength/maxOutputTokens 는 저장 후 다시 만들 때 적용. owner 전용.")
async def update_sandbox(sandbox_id: str, body: SandboxPatch, svc: Svc, authorization: Auth = None):
    return await _call(svc.admin.update_sandbox, sandbox_id, provider=body.provider, model=body.model,
                       context_length=body.contextLength, max_output_tokens=body.maxOutputTokens,
                       authenticated=svc.me.authenticated(authorization))
