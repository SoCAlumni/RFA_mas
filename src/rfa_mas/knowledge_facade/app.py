"""HTTP provider for 승희's knowledge contract: `GET /tasks`, `POST /tasks/{task_id}/ask`.

Replace RFA_module's `services/knowledge_stub` by pointing `KNOWLEDGE_URL` at this app.
The route shapes, status codes and the 404 body follow `contracts/knowledge.openapi.yaml`
(pinned copy: `fixtures/contracts/rfa_module/knowledge.openapi.yaml`).

Identity: the facade answers as the installation owner's 실무대장 with a FIXED audience
chosen at startup. A public facade needs no caller credential because it only serves
public, shareable evidence; any other audience requires a bearer key on every call.
"""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import SecretStr

from rfa_mas.bootstrap import Container
from rfa_mas.contracts import SCHEMA_VERSION, Audience
from rfa_mas.errors import RfaError
from rfa_mas.knowledge_facade.contract import (
    KNOWLEDGE_CONTRACT_SOURCE,
    KNOWLEDGE_CONTRACT_VERSION,
    AskRequest,
    KnowledgeNotFound,
    KnowledgeResult,
    KnowledgeUnauthorized,
    TaskInfo,
)
from rfa_mas.knowledge_facade.service import KnowledgeFacadeService

FACADE_AUDIENCES = (Audience.PUBLIC, Audience.COMPANY, Audience.BUSINESS_UNIT)


def create_knowledge_facade_app(
    container: Container,
    *,
    audience: Audience = Audience.PUBLIC,
    api_key: SecretStr | None = None,
    service: KnowledgeFacadeService | None = None,
) -> FastAPI:
    if audience not in FACADE_AUDIENCES:
        raise ValueError("facade audience must be public, company or business_unit")
    if audience != Audience.PUBLIC and api_key is None:
        raise ValueError("non-public facade audience requires an api key")
    facade = service or KnowledgeFacadeService.from_container(container, audience=audience)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owns_container = not container.ready
        if owns_container:
            await container.startup()
        app.state.container = container
        try:
            yield
        finally:
            if owns_container:
                await container.shutdown()

    app = FastAPI(
        title="RFA knowledge contract (실무대장 ↔ 언론사) — rfa_mas provider",
        version=KNOWLEDGE_CONTRACT_VERSION,
        summary="RFA_module knowledge stub replacement backed by the RFA core",
        description=(
            f"Implements {KNOWLEDGE_CONTRACT_SOURCE}. Audience: {audience.value}. "
            "Answers use only evidence the audience may read and share; empty answer = no "
            "knowledge. No review, approval or publication happens here."
        ),
        lifespan=lifespan,
    )
    app.state.container = container
    app.state.facade_audience = audience

    async def authorize(
        request: Request, authorization: Annotated[str | None, Header()] = None
    ) -> None:
        if api_key is None:
            return
        expected = f"Bearer {api_key.get_secret_value()}".encode()
        if authorization is None or not hmac.compare_digest(authorization.encode(), expected):
            raise RfaError("authentication_required", "유효한 API 인증이 필요합니다.")

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "detail": [{"loc": ["body"], "msg": "Invalid request", "type": "value_error"}]
            },
        )

    @app.exception_handler(RfaError)
    async def handle_domain_error(request: Request, exc: RfaError) -> JSONResponse:
        if exc.code == "authentication_required":
            return JSONResponse(
                status_code=401, content=KnowledgeUnauthorized().model_dump(mode="json")
            )
        # Any other core failure is a safe 503; never a partial or unscreened answer.
        return JSONResponse(status_code=503, content={"error": "unavailable"})

    @app.get("/healthz", tags=["operations"], include_in_schema=False)
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "service": "rfa-mas-knowledge-facade",
            "schema_version": SCHEMA_VERSION,
            "contract": KNOWLEDGE_CONTRACT_SOURCE,
            "audience": audience.value,
        }

    @app.get(
        "/tasks",
        response_model=list[TaskInfo],
        operation_id="listTasks",
        summary="지금 살아 있는 task 목록 (동적)",
        dependencies=[Depends(authorize)],
        responses={401: {"model": KnowledgeUnauthorized}},
    )
    async def list_tasks() -> list[TaskInfo]:
        return await facade.list_tasks()

    @app.post(
        "/tasks/{task_id}/ask",
        response_model=KnowledgeResult,
        operation_id="askTask",
        summary="task supervisor 에게 질문",
        dependencies=[Depends(authorize)],
        responses={404: {"model": KnowledgeNotFound}, 401: {"model": KnowledgeUnauthorized}},
    )
    async def ask_task(
        task_id: str,
        body: AskRequest,
        x_rfa_actor: Annotated[str | None, Header(alias="X-RFA-Actor")] = None,
    ):
        # X-RFA-Actor is informational (writer node name); it grants nothing.
        if not facade.has_task(task_id):
            return JSONResponse(
                status_code=404, content=KnowledgeNotFound(id=task_id).model_dump(mode="json")
            )
        return await facade.ask(task_id, body.question)

    return app
