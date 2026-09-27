from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from rfa_mas.nemoclaw.routes._deps import services
from rfa_mas.nemoclaw.schemas import TaskView
from rfa_mas.nemoclaw.services import FrontendServices

router = APIRouter(tags=["frontend"])


@router.get("/tasks", operation_id="listTasks", response_model=list[TaskView],
            description="head 가 라우팅할 수 있는 task 목록: ask.yaml 카탈로그 + 스폰된 팀. gradeLabel 은 배치 샌드박스의 보안 등급.")
async def list_tasks(svc: Annotated[FrontendServices, Depends(services)]):
    return [TaskView(**t) for t in svc.tasks.list()]
