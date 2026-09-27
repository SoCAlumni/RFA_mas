from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from rfa_mas.nemoclaw.routes._deps import services
from rfa_mas.nemoclaw.schemas import AgentRef
from rfa_mas.nemoclaw.services import FrontendServices

router = APIRouter(tags=["frontend"])


@router.get("/agents", operation_id="listAgents", response_model=list[AgentRef],
            description="대화 가능한 에이전트(assistant + task 에이전트 + 팀 supervisor). censor 는 제외.")
async def list_agents(svc: Annotated[FrontendServices, Depends(services)]):
    return [AgentRef(**a) for a in svc.agents.list()]
