from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from rfa_mas.nemoclaw.routes._deps import services
from rfa_mas.nemoclaw.schemas import AgentView
from rfa_mas.nemoclaw.services import FrontendServices

router = APIRouter(tags=["frontend"])


@router.get("/agents", operation_id="listAgents", response_model=list[AgentView],
            description="대화 상대: 비서(assistant) + task 마다 에이전트 1개(D-10). 검열 에이전트는 제외. 등급 필드는 없다(등급은 요청 단위).")
async def list_agents(svc: Annotated[FrontendServices, Depends(services)]):
    await svc.inbox.refresh_counts()  # itemCount = pending approvals per task
    return [AgentView(**a) for a in svc.agents.list()]
