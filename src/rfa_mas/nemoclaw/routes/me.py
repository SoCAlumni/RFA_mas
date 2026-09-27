from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header

from rfa_mas.nemoclaw.routes._deps import services
from rfa_mas.nemoclaw.schemas import Me
from rfa_mas.nemoclaw.services import FrontendServices

router = APIRouter(tags=["frontend"])


@router.get("/me", operation_id="me", response_model=Me,
            description="Bearer RFA_ASK_TOKEN 이 맞으면 owner/admin, 아니면 guest (401 없이 항상 200).")
async def me(svc: Annotated[FrontendServices, Depends(services)],
             authorization: Annotated[str | None, Header()] = None):
    return Me(**svc.me.resolve(authorization))
