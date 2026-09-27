"""Small same-origin local UI over the existing core API and the local review stand-in.

Replaceable by the teammate UI. The UI owns no DB and never duplicates core API
logic: existing /ui/api routes map to fixed upstream calls; optional ChatPort routes
delegate conversation orchestration to the injected application consumer. There is no
user-supplied URL, no generic proxy, and upstream service tokens stay on the
server (never in HTML, JS, responses, URLs, cookies or browser storage).
"""

import hashlib
import hmac
import json
import re
import secrets
from collections.abc import AsyncIterator, Iterable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from starlette.exceptions import HTTPException as StarletteHTTPException

from rfa_mas.adapters.http import require_loopback_reference_url
from rfa_mas.application.chat import ChatMessage, ChatPort
from rfa_mas.contracts import (
    Audience,
    DirectWorkRequest,
    DomainId,
    DraftTarget,
    KnowledgeProvenance,
    KnowledgeWrite,
    ResumeRequest,
    ReviewStatus,
    StructuredError,
    new_id,
)
from rfa_mas.errors import RfaError
from rfa_mas.reference.local_response import LocalDecisionRequest, LocalPublicationRequest
from rfa_mas.reference.local_security import (
    LocalBoundaryMiddleware,
    LocalServiceBoundary,
    is_opaque_id,
)

STATIC_DIR = Path(__file__).parent / "static"
CSRF_COOKIE = "rfa_ui_csrf"
CSRF_HEADER = "x-rfa-csrf"
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)

_MESSAGES = {
    "csrf_rejected": "같은 출처의 유효한 CSRF 확인이 필요합니다.",
    "invalid_request": "요청 형식이 올바르지 않습니다.",
    "not_found": "리소스를 찾을 수 없습니다.",
    "method_not_allowed": "허용되지 않은 method입니다.",
    "idempotency_key_required": "유효한 Idempotency-Key가 필요합니다.",
    "review_not_configured": "검토 서비스가 연결되지 않았습니다.",
    "upstream_timeout": "응답 시간이 초과되었습니다. 상태를 다시 조회하세요.",
    "upstream_unavailable": "서비스에 연결할 수 없습니다. 상태를 다시 조회하세요.",
    "outcome_unknown": (
        "요청 결과를 확인할 수 없습니다. 자동 재시도/재게시하지 않습니다. 상태를 다시 조회하세요."
    ),
    "upstream_auth_failed": "서비스 인증이 거절되었습니다(권한 회수 가능). 설정을 확인하세요.",
    "upstream_contract_error": "서비스 응답이 안전한 계약을 충족하지 않습니다.",
    "upstream_error": "서비스 오류가 발생했습니다. 상태를 다시 조회하세요.",
    "upstream_rejected": "서비스가 요청을 거절했습니다.",
}
_QUERY_REQUIRED = frozenset({"upstream_timeout", "upstream_unavailable", "outcome_unknown"})


class UiError(Exception):
    def __init__(
        self, status_code: int, code: str, *, upstream: str | None = None, upstream_code: str = ""
    ) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.upstream = upstream
        self.upstream_code = upstream_code


def _error_json(error: UiError) -> JSONResponse:
    details: dict[str, Any] = {"query_required": error.code in _QUERY_REQUIRED}
    if error.upstream:
        details["upstream"] = error.upstream
    if error.upstream_code:
        details["upstream_code"] = error.upstream_code
    body = StructuredError(
        code=error.code,
        retryable=False,
        message=_MESSAGES.get(error.code, _MESSAGES["upstream_rejected"]),
        details=details,
    )
    return JSONResponse(status_code=error.status_code, content=body.model_dump(mode="json"))


@dataclass(frozen=True)
class UpstreamTarget:
    """Fixed server-side upstream. The browser can never choose or change it."""

    name: Literal["core", "review"]
    transport: httpx.AsyncBaseTransport = field(repr=False)
    base_url: str
    token: SecretStr | None = field(default=None, repr=False)
    timeout_seconds: float = 30.0
    lifespan_app: FastAPI | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        # Same loopback-only rule as the existing reference HTTP adapters.
        require_loopback_reference_url(self.base_url, setting_name=f"{self.name}_base_url")
        if self.token is not None and not isinstance(self.token, SecretStr):
            raise TypeError("token must be a SecretStr")

    @classmethod
    def in_process(
        cls,
        name: Literal["core", "review"],
        app: FastAPI,
        *,
        base_url: str,
        token: SecretStr | None = None,
        manage_lifespan: bool = False,
    ) -> "UpstreamTarget":
        return cls(
            name=name,
            transport=httpx.ASGITransport(app=app),
            base_url=base_url,
            token=token,
            lifespan_app=app if manage_lifespan else None,
        )


class UiWorkBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=10_000)
    domain_id: DomainId | None = None
    target_audience: Audience = Audience.OWNER
    client_request_id: str | None = Field(default=None, max_length=160)


class UiNoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain_id: DomainId
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=50_000)


class TeamKbLinkBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_ids: list[str] = Field(min_length=1, max_length=50)
    note: str = Field(default="", max_length=200)


class _Upstream:
    def __init__(self, target: UpstreamTarget) -> None:
        self.target = target
        self.client = httpx.AsyncClient(
            transport=target.transport,
            base_url=target.base_url,
            timeout=httpx.Timeout(target.timeout_seconds),
            follow_redirects=False,
        )

    async def call(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        mutation: bool = False,
    ) -> Any:
        name = self.target.name
        headers = {"Accept": "application/json"}
        if self.target.token is not None:
            headers["Authorization"] = f"Bearer {self.target.token.get_secret_value()}"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = await self.client.request(
                method, path, json=json_body, params=params, headers=headers
            )
        except httpx.TimeoutException:
            raise UiError(
                504, "outcome_unknown" if mutation else "upstream_timeout", upstream=name
            ) from None
        except httpx.HTTPError:
            raise UiError(
                502, "outcome_unknown" if mutation else "upstream_unavailable", upstream=name
            ) from None
        token = self.target.token.get_secret_value() if self.target.token else None
        if token and token in response.text:
            raise UiError(502, "upstream_contract_error", upstream=name)
        if response.status_code >= 500:
            raise UiError(502, "outcome_unknown" if mutation else "upstream_error", upstream=name)
        if response.status_code == 401:
            raise UiError(502, "upstream_auth_failed", upstream=name)
        try:
            payload = response.json()
        except ValueError:
            raise UiError(502, "upstream_contract_error", upstream=name) from None
        if response.status_code >= 400:
            code = payload.get("code") if isinstance(payload, dict) else None
            raise UiError(
                response.status_code if response.status_code < 500 else 502,
                "upstream_rejected",
                upstream=name,
                upstream_code=code if isinstance(code, str) and _SAFE_CODE.fullmatch(code) else "",
            )
        return payload


def _csrf_token(secret: bytes, nonce: str) -> str:
    return hmac.new(secret, nonce.encode(), hashlib.sha256).hexdigest()


def _path_id(value: str) -> str:
    if not is_opaque_id(value):
        raise UiError(404, "not_found")
    return value


def _idempotency(value: str | None) -> str:
    if value is None or not is_opaque_id(value):
        raise UiError(400, "idempotency_key_required")
    return value


def create_local_ui_app(
    *,
    allowed_hosts: Iterable[str],
    core: UpstreamTarget,
    review: UpstreamTarget | None = None,
    chat: ChatPort | None = None,
    teams: Any | None = None,
    model_info: dict[str, Any] | None = None,
) -> FastAPI:
    """Build the UI with fixed, server-injected upstreams only.

    allowed_hosts are the exact loopback host:port values the browser uses.
    """

    if core.name != "core" or (review is not None and review.name != "review"):
        raise ValueError("upstream names must match their role")
    # Only Host/Origin/body checks are used here; the UI has no bearer auth, so a
    # random per-process value fills the boundary's unused token slot.
    boundary = LocalServiceBoundary.create(
        owner_id="local-ui",
        service_token=SecretStr(secrets.token_urlsafe(32)),
        allowed_hosts=allowed_hosts,
    )
    csrf_secret = secrets.token_bytes(32)
    core_upstream = _Upstream(core)
    review_upstream = _Upstream(review) if review is not None else None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            for target in (core, review):
                if target is not None and target.lifespan_app is not None:
                    inner = target.lifespan_app
                    await stack.enter_async_context(inner.router.lifespan_context(inner))
            try:
                yield
            finally:
                await core_upstream.client.aclose()
                if review_upstream is not None:
                    await review_upstream.client.aclose()

    app = FastAPI(
        title="RFA local UI (replaceable stand-in)",
        version="0.1-local",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(
        LocalBoundaryMiddleware,
        boundary=boundary,
        require_bearer=False,
        extra_headers=((b"content-security-policy", CONTENT_SECURITY_POLICY.encode()),),
    )

    @app.exception_handler(UiError)
    async def _ui_error(request: Request, exc: UiError) -> JSONResponse:
        return _error_json(exc)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error_json(UiError(422, "invalid_request"))

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "invalid_request")
        return _error_json(UiError(exc.status_code, code))

    async def require_csrf(request: Request) -> None:
        origin = request.headers.get("origin")
        host = request.headers.get("host", "")
        nonce = request.cookies.get(CSRF_COOKIE, "")
        supplied = request.headers.get(CSRF_HEADER, "")
        if (
            origin is None
            or origin.lower() != f"http://{host.lower()}"
            or not nonce
            or not supplied
            or not hmac.compare_digest(supplied.encode(), _csrf_token(csrf_secret, nonce).encode())
        ):
            raise UiError(403, "csrf_rejected")

    Csrf = Annotated[None, Depends(require_csrf)]
    IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]

    def reviews() -> _Upstream:
        if review_upstream is None:
            raise UiError(503, "review_not_configured")
        return review_upstream

    # ---------- static ----------
    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/ui/", status_code=307)

    @app.get("/ui/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html; charset=utf-8")

    @app.get("/ui/static/app.js", include_in_schema=False)
    async def script() -> FileResponse:
        return FileResponse(STATIC_DIR / "app.js", media_type="text/javascript; charset=utf-8")

    @app.get("/ui/static/app.css", include_in_schema=False)
    async def style() -> FileResponse:
        return FileResponse(STATIC_DIR / "app.css", media_type="text/css; charset=utf-8")

    # ---------- UI API: each route is one fixed upstream call ----------
    @app.get("/ui/api/csrf")
    async def csrf() -> JSONResponse:
        nonce = secrets.token_urlsafe(32)
        response = JSONResponse({"csrf_token": _csrf_token(csrf_secret, nonce)})
        response.set_cookie(
            CSRF_COOKIE, nonce, httponly=True, samesite="strict", path="/ui", secure=False
        )
        return response

    @app.get("/ui/api/status")
    async def status() -> dict[str, Any]:
        core_state: dict[str, Any] = {"reachable": False}
        try:
            health = await core_upstream.call("GET", "/healthz")
            core_state = {"reachable": True, "schema_version": health.get("schema_version")}
        except UiError as exc:
            core_state["error"] = exc.code
        review_state: dict[str, Any] = {"configured": review_upstream is not None}
        if review_upstream is not None:
            try:
                capabilities = await review_upstream.call("GET", "/v1/capabilities")
                review_state |= {
                    "reachable": True,
                    "mode": capabilities.get("mode"),
                    "authority": capabilities.get("authority"),
                    "simulated": capabilities.get("simulated"),
                }
            except UiError as exc:
                review_state |= {"reachable": False, "error": exc.code}
        review_on = review_state.get("reachable") is True
        return {
            "ui": {"mode": "local", "framework": "none", "teammate_ui": False},
            "core": core_state,
            "review": review_state,
            # P1-008K: adapter name / simulated flag / model id only (never endpoint or key).
            "model": model_info or {"adapter": "unknown", "simulated": True, "model_id": None},
            "features": {
                "sessions": "enabled",
                "notes": "enabled",
                "work": "enabled",
                "manual_review": "enabled" if review_on else "disabled",
                "mock_publication": "mock" if review_on else "disabled",
                "real_publication": "disabled",
                "schedules": "disabled",
                "team_role_execution": "disabled",
                "openshell": "disabled",
                "experiments": "not_run",
            },
            "gates": {
                "real_ui_integration": "not_run",
                "teammate_services": "not_run",
                "real_publication": "not_run",
                "openshell": "not_run",
            },
        }

    @app.get("/ui/api/sessions")
    async def list_sessions() -> Any:
        return await core_upstream.call("GET", "/v1/sessions")

    @app.post("/ui/api/sessions", status_code=201)
    async def create_session(_: Csrf) -> Any:
        return await core_upstream.call("POST", "/v1/sessions", json_body={}, mutation=True)

    @app.get("/ui/api/sessions/{session_id}")
    async def get_session(session_id: str) -> Any:
        return await core_upstream.call("GET", f"/v1/sessions/{_path_id(session_id)}")

    @app.post("/ui/api/sessions/{session_id}/work", status_code=201)
    async def submit_work(session_id: str, body: UiWorkBody, _: Csrf) -> Any:
        checked = _path_id(session_id)
        extra: dict[str, Any] = {}
        if body.client_request_id is not None:
            extra["request_id"] = _idempotency(body.client_request_id)
        work = DirectWorkRequest(
            query=body.query,
            domain_id=body.domain_id,
            target=DraftTarget(audience=body.target_audience),
            session_id=checked,
            **extra,
        )
        return await core_upstream.call(
            "POST",
            f"/v1/sessions/{checked}/work",
            json_body=work.model_dump(mode="json"),
            mutation=True,
        )

    @app.get("/ui/api/runs/{run_id}")
    async def get_run(run_id: str) -> Any:
        return await core_upstream.call("GET", f"/v1/runs/{_path_id(run_id)}")

    @app.post("/ui/api/runs/{run_id}/refresh-review")
    async def refresh_review(run_id: str, _: Csrf) -> Any:
        # A wakeup that QUERIES the review source; it never grants approval.
        wakeup = ResumeRequest(event_id=new_id("ui_refresh"))
        return await core_upstream.call(
            "POST",
            f"/v1/runs/{_path_id(run_id)}/resume",
            json_body=wakeup.model_dump(mode="json"),
            mutation=True,
        )

    @app.get("/ui/api/notes")
    async def list_notes(domain_id: DomainId | None = None) -> Any:
        params = {"domain_id": domain_id.value} if domain_id else None
        return await core_upstream.call("GET", "/v1/knowledge/sources", params=params)

    @app.post("/ui/api/notes", status_code=201)
    async def create_note(body: UiNoteBody, _: Csrf, idempotency_key: IdempotencyKey = None) -> Any:
        key = _idempotency(idempotency_key)
        # Unclassified user notes stay private (owner only) by default.
        write = KnowledgeWrite(
            domain_id=body.domain_id,
            provenance=KnowledgeProvenance(provider="note", namespace="local-ui", external_id=key),
            provider_revision="1",
            title=body.title,
            content=body.content,
        )
        return await core_upstream.call(
            "POST", "/v1/knowledge/sources", json_body=write.model_dump(mode="json"), mutation=True
        )

    if chat is not None:

        @app.get("/ui/api/chat/sessions")
        async def chat_sessions() -> Any:
            try:
                return await chat.sessions()
            except RfaError as exc:
                raise UiError(409, "upstream_rejected", upstream_code=exc.code) from None

        @app.get("/ui/api/chat/assignees")
        async def chat_assignees() -> Any:
            """Owner's Task teams for the assignee picker (read-only; ownership re-checked)."""
            try:
                return await chat.router.assignees()
            except RfaError as exc:
                raise UiError(409, "upstream_rejected", upstream_code=exc.code) from None

        @app.get("/ui/api/sessions/{session_id}/chat")
        async def chat_history(session_id: str) -> Any:
            try:
                return await chat.history(_path_id(session_id))
            except RfaError as exc:
                raise UiError(
                    404 if exc.code == "not_found" else 409,
                    "upstream_rejected",
                    upstream_code=exc.code,
                ) from None

        @app.post("/ui/api/sessions/{session_id}/chat", status_code=201)
        async def chat_send(session_id: str, body: ChatMessage, _: Csrf) -> Any:
            try:
                return await chat.send(_path_id(session_id), body)
            except RfaError as exc:
                raise UiError(
                    404 if exc.code == "not_found" else 409,
                    "outcome_unknown" if exc.code == "outcome_unknown" else "upstream_rejected",
                    upstream_code=exc.code,
                ) from None

        @app.get("/ui/api/notes/{source_id}")
        async def read_note(source_id: str) -> Any:
            return await core_upstream.call("GET", f"/v1/knowledge/sources/{_path_id(source_id)}")

        @app.post("/ui/api/sessions/{session_id}/chat/stream")
        async def chat_stream(session_id: str, body: ChatMessage, _: Csrf) -> Any:
            try:
                events = await chat.stream(_path_id(session_id), body)
            except RfaError as exc:
                raise UiError(
                    404 if exc.code == "not_found" else 409,
                    "upstream_rejected",
                    upstream_code=exc.code,
                ) from None

            async def encode():
                try:
                    async for event in events:
                        yield json.dumps(event, ensure_ascii=False) + "\n"
                finally:
                    await events.aclose()

            return StreamingResponse(
                encode(),
                media_type="application/x-ndjson",
                headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
            )

    if teams is not None:

        def _team_error(exc: RfaError) -> UiError:
            return UiError(
                404 if exc.code == "not_found" else 409,
                "upstream_rejected",
                upstream_code=exc.code,
            )

        @app.get("/ui/api/teams")
        async def list_teams() -> Any:
            """Owner's Task teams: composition, state, recent runs and linked KB."""
            try:
                return await teams.list()
            except RfaError as exc:
                raise _team_error(exc) from None

        @app.get("/ui/api/teams/{task_id}")
        async def get_team(task_id: str) -> Any:
            try:
                return await teams.get(_path_id(task_id))
            except RfaError as exc:
                raise _team_error(exc) from None

        @app.post("/ui/api/teams/{task_id}/kb")
        async def link_team_kb(task_id: str, body: TeamKbLinkBody, _: Csrf) -> Any:
            if not all(is_opaque_id(source_id) for source_id in body.source_ids):
                raise UiError(404, "not_found")
            try:
                return await teams.link(_path_id(task_id), body.source_ids, note=body.note)
            except RfaError as exc:
                raise _team_error(exc) from None

        @app.delete("/ui/api/teams/{task_id}/kb/{source_id}")
        async def unlink_team_kb(task_id: str, source_id: str, _: Csrf) -> Any:
            try:
                return await teams.unlink(_path_id(task_id), _path_id(source_id))
            except RfaError as exc:
                raise _team_error(exc) from None

    @app.get("/ui/api/reviews")
    async def list_reviews(
        decision: ReviewStatus | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> Any:
        params: dict[str, Any] = {"limit": limit}
        if decision is not None:
            params["decision"] = decision.value
        return await reviews().call("GET", "/v1/local/reviews", params=params)

    @app.get("/ui/api/reviews/{draft_id}")
    async def get_review(draft_id: str) -> Any:
        return await reviews().call("GET", f"/v1/local/reviews/{_path_id(draft_id)}")

    @app.post("/ui/api/reviews/{draft_id}/decision")
    async def decide(
        draft_id: str,
        body: LocalDecisionRequest,
        _: Csrf,
        idempotency_key: IdempotencyKey = None,
    ) -> Any:
        upstream = reviews()
        return await upstream.call(
            "POST",
            f"/v1/local/reviews/{_path_id(draft_id)}/decision",
            json_body=body.model_dump(mode="json"),
            idempotency_key=_idempotency(idempotency_key),
            mutation=True,
        )

    @app.post("/ui/api/publications")
    async def mock_publish(
        body: LocalPublicationRequest, _: Csrf, idempotency_key: IdempotencyKey = None
    ) -> Any:
        upstream = reviews()
        return await upstream.call(
            "POST",
            "/v1/local/publications",
            json_body=body.model_dump(mode="json"),
            idempotency_key=_idempotency(idempotency_key),
            mutation=True,
        )

    @app.get("/ui/api/publications/{publication_id}")
    async def get_publication(publication_id: str) -> Any:
        return await reviews().call("GET", f"/v1/local/publications/{_path_id(publication_id)}")

    return app


__all__ = ["UpstreamTarget", "create_local_ui_app"]
