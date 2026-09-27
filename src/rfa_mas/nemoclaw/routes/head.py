from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from rfa_mas.nemoclaw.routes._deps import services
from rfa_mas.nemoclaw.schemas.common import ErrorOut
from rfa_mas.nemoclaw.schemas.head import HeadAskIn, HeadAskOut
from rfa_mas.nemoclaw.services import FrontendServices

router = APIRouter(tags=["head"], prefix="/v1/head")
LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}


@router.post("/ask", operation_id="headAsk", response_model=HeadAskOut, responses={403: {"model": ErrorOut}},
             description="RFA_module head 계약 v0.2.0 호환(D-21): RFA_module `.env` 의 `HEAD_URL=http://127.0.0.1:8799/v1/head`. "
                         "인증 없이 루프백만 받는다. request_id 는 url+차수(거절 수+1)로 서버가 만들어 재시도에 멱등, "
                         "refusal 은 한국어 문자열, 크기 초과는 잘라서 처리, 서버 한도 110초. 들어오는 즉시 결재함에 「작성 중」 항목이 생긴다.")
async def head_ask(body: HeadAskIn, request: Request, svc: Annotated[FrontendServices, Depends(services)]):
    host = request.client.host if request.client else ""
    if host not in LOOPBACK:
        return JSONResponse(status_code=403, content=ErrorOut(code="loopback_only",
                                                              message="이 엔드포인트는 같은 호스트에서만 받습니다.").model_dump())
    return HeadAskOut(**await svc.intake.handle(body))
