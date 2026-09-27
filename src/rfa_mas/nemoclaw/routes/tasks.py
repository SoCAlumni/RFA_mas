from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse, StreamingResponse

from rfa_mas.nemoclaw.routes._deps import services
from rfa_mas.nemoclaw.routes.chat import SSE_HEADERS, sse
from rfa_mas.nemoclaw.schemas import TaskView
from rfa_mas.nemoclaw.schemas.common import TASK_EVENTS, ErrorOut, SseEnvelope, TaskCreateRequest
from rfa_mas.nemoclaw.services import FrontendServices
from rfa_mas.nemoclaw.services.task_create import TaskCreateError

router = APIRouter(tags=["frontend"])


@router.get("/tasks", operation_id="listTasks", response_model=list[TaskView],
            description="좌측 태스크 목록: ask.yaml 카탈로그 + 만든 태스크(팀). 생성 중(applying)도 포함, 실패는 제외.")
async def list_tasks(svc: Annotated[FrontendServices, Depends(services)]):
    await svc.inbox.refresh_counts()  # itemCount = pending approvals per task
    return [TaskView(**t) for t in svc.tasks.list()]


@router.get("/tasks/{task_id}", operation_id="getTask", response_model=TaskView,
            responses={404: {"model": ErrorOut}},
            description="태스크 하나. 생성 스트림이 끊겼을 때 applying → ready|failed 를 확인한다(failed 도 돌려준다).")
async def get_task(task_id: str, svc: Annotated[FrontendServices, Depends(services)]):
    await svc.inbox.refresh_counts()
    task = svc.tasks.get(task_id)
    if task is None:
        return JSONResponse(status_code=404, content=ErrorOut(code="unknown_task", message="없는 태스크입니다.").model_dump())
    return TaskView(**task)


@router.post("/tasks", operation_id="createTask", status_code=200,
             description="태스크 추가(태스크 1 = 에이전트 1). owner 전용. 검증 실패는 스트림 전에 JSON 오류(403/409/422/503), "
                         "통과하면 SSE 로 요구사항 분석 → 팀 설계(패턴 선택) → spawning 을 보낸다. 연결이 끊겨도 생성은 계속되며 "
                         "`GET /tasks/{id}` 로 확인한다. 봉투 `{type, runId, seq, ts, data}`; type: "
                         + ", ".join(f"`{k}` {v}" for k, v in TASK_EVENTS.items()),
             responses={200: {"content": {"text/event-stream": {"schema": SseEnvelope.model_json_schema()}},
                              "description": "SSE stream of SseEnvelope frames"},
                        403: {"model": ErrorOut}, 409: {"model": ErrorOut}, 422: {"model": ErrorOut},
                        503: {"model": ErrorOut}})
async def create_task(body: TaskCreateRequest, svc: Annotated[FrontendServices, Depends(services)],
                      authorization: Annotated[str | None, Header()] = None):
    try:
        row = svc.task_create.prepare(name=body.name, agent_name=body.agentName, description=body.description,
                                      tags=body.tags, authenticated=svc.me.authenticated(authorization))
    except TaskCreateError as exc:
        return JSONResponse(status_code=exc.status, content=ErrorOut(code=exc.code, message=exc.message).model_dump())
    return StreamingResponse(sse(svc.task_create.run(row), run_id=f"task-{row['id']}"), media_type="text/event-stream",
                             headers=SSE_HEADERS)
