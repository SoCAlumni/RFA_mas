"""PoC composition only: existing core, UI and review authority stay replaceable.

Only the UI listens on TCP. Internal HTTP contracts use ASGI transports; no service
credentials leave the process. The normal application bootstrap remains unchanged.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from pathlib import Path

import httpx
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount

from rfa_mas.adapters.http import ReferenceHttpClient, local_review_draft
from rfa_mas.api.app import create_app
from rfa_mas.application.observations import ObservedPort
from rfa_mas.application.resume_policy import ResumePolicy
from rfa_mas.bootstrap import build_container
from rfa_mas.contracts import PublicationStatus, ReviewDecision, sha256_text
from rfa_mas.errors import RfaError
from rfa_mas.reference.local_response import create_local_response_app
from rfa_mas.reference.local_security import LocalServiceBoundary
from rfa_mas.settings import Settings
from rfa_mas.ui.app import UpstreamTarget, create_local_ui_app

REVIEW_URL = "http://127.0.0.1:18781"
CORE_URL = "http://127.0.0.1:18782"


def local_settings(root: Path, token: SecretStr) -> Settings:
    # Every field is explicitly initialized, so neither .env nor ambient provider
    # variables can change this keyless PoC into a real/cloud configuration.
    values = {
        name: field.get_default(call_default_factory=True)
        for name, field in Settings.model_fields.items()
    }
    values.update(
        database_url=f"sqlite:///{root / 'core' / 'rfa.db'}",
        checkpoint_path=root / "core" / "checkpoints.db",
        trace_dir=root / "traces",
        response_backend="http",
        response_base_url=REVIEW_URL,
        response_api_token=token,
        log_level="WARNING",
    )
    return Settings(_env_file=None, **values)


@contextmanager
def data_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Do not follow a symlink for the process lock or change permissions on user files.
    fd = os.open(root / "poc.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("poc_data_dir_in_use") from None
        yield
    finally:
        os.close(fd)


class ReviewTransport(httpx.AsyncBaseTransport):
    """Fixed in-process authority; never routes arbitrary hosts or falls back to TCP."""

    inner: httpx.ASGITransport | None = None

    async def handle_async_request(self, request):
        if str(request.url).split("/", 3)[:3] != REVIEW_URL.split("/")[:3]:
            raise httpx.ConnectError("poc_unknown_upstream")
        if self.inner is None:
            raise httpx.ConnectError("poc_review_not_ready")
        return await self.inner.handle_async_request(request)

    async def aclose(self):
        if self.inner is not None:
            await self.inner.aclose()


class PocResponse:
    """Explicit 1.0 core -> 1.1 manual authority mapper, not an auto-approver.

    The 1.1 approval includes payload/source/policy binding needed by the publisher.
    The policy reference is minted only AFTER the real local ResumePolicy check.
    """

    adapter_name = "poc-reference-response-v11"
    simulated = True

    def __init__(self, client, container):
        self.client, self.container = client, container

    @staticmethod
    def decision(draft, view):
        return ReviewDecision(
            request_id=draft.request_id,
            trace_id=draft.trace_id,
            run_id=draft.run_id,
            agent_id=draft.agent_id,
            domain_id=draft.domain_id,
            draft_id=view["draft_id"],
            draft_version=view["version"],
            content_hash=view["content_hash"],
            target=view["target"],
            decision=view["decision"],
            publication_status=PublicationStatus.NOT_REQUESTED,
            safe_reason="로컬 수동 검토 결과; 실제 외부 게시 아님",
            simulated=True,
            adapter=PocResponse.adapter_name,
        )

    async def submit_draft(self, draft, *, idempotency_key, **_kwargs):
        owner = await self.container.repository.local_principal()
        await ResumePolicy(self.container.repository, self.container.policy)(draft, owner)
        policy_ref = (
            "poc-policy:"
            + sha256_text(
                f"{draft.draft_id}:{draft.version}:{draft.content_hash}:{draft.policy_version}"
            )[:32]
        )
        mapped = local_review_draft(draft, policy_decision_id=policy_ref)
        response = await self.client.request(
            "POST",
            "/v1/local/reviews",
            json_body={"draft": mapped.model_dump(mode="json")},
            idempotency_key=idempotency_key,
            retry_read=False,
        )
        return self.decision(draft, response.json())

    async def get_decision(self, draft_id):
        response = await self.client.request(
            "GET", f"/v1/local/reviews/{draft_id}", retry_read=True
        )
        view = response.json()
        owner = await self.container.repository.local_principal()
        versions = await self.container.repository.draft_versions(view["run_id"], owner)
        draft = next((d for d, _ in versions if d.draft_id == draft_id), None)
        if draft is None:
            raise RfaError("not_found", "초안 기록을 찾을 수 없습니다.")
        return self.decision(draft, view)


class UiReviewTransport(httpx.AsyncBaseTransport):
    """UI publications pass through the core's current ACL/policy + durable ledger.

    Approval remains in the separate review store. This bridge translates only the
    fixed local publication request; no arbitrary proxy or second approval authority.
    """

    def __init__(self, review, core):
        self.review, self.core = review, core

    async def handle_async_request(self, request):
        if request.method != "POST" or request.url.path != "/v1/local/publications":
            return await self.review.send(request)
        body = json.loads(request.content)
        view_response = await self.review.get(f"/v1/local/reviews/{body['draft_id']}")
        if view_response.status_code != 200:
            return view_response
        view = view_response.json()
        approval = view.get("approval")
        if (
            approval is None
            or approval["decision"] != "approved"
            or approval["approval_id"] != body["approval_id"]
            or (view["version"], view["payload_hash"]) != (body["version"], body["payload_hash"])
            or body.get("simulate_outcome", "succeeded") != "succeeded"
        ):
            return httpx.Response(409, json={"code": "approval_required"})
        response = await self.core.post(
            f"/v1/runs/{view['run_id']}/publication",
            json={"idempotency_key": request.headers["Idempotency-Key"]},
        )
        if response.status_code != 200:
            return response
        receipt = response.json()
        ref = receipt.get("external_result_ref") or ""
        if receipt["status"] != "succeeded" or not ref.startswith("local-artifact:"):
            # No retry/second POST for an uncertain outcome. Core retains its journal.
            return httpx.Response(503, json={"code": "outcome_unknown"})
        return await self.review.get(f"/v1/local/publications/{ref.split(':', 1)[1]}")


def create_poc_app(data_dir: Path, *, port: int = 8780):
    if not 1 <= port <= 65535:
        raise ValueError("invalid_port")
    root = data_dir.expanduser().resolve()
    if root == Path(root.anchor) or root == Path.home() or root == Path.cwd().resolve():
        raise ValueError("dedicated_data_dir_required")
    holder = {}

    async def delegate(scope, receive, send):
        ui = holder.get("ui")
        if ui is None:
            await JSONResponse({"code": "poc_not_ready"}, status_code=503)(scope, receive, send)
            return
        await ui(scope, receive, send)

    @asynccontextmanager
    async def lifespan(app):
        with data_lock(root):
            token = SecretStr(secrets.token_urlsafe(32))
            transport = ReviewTransport()
            container = build_container(local_settings(root, token), http_transport=transport)
            try:
                await container.startup()
                owner = await container.service_owner_id()
                review_app = create_local_response_app(
                    db_path=root / "review" / "review.db",
                    boundary=LocalServiceBoundary.create(
                        owner_id=owner,
                        service_token=token,
                        allowed_hosts=["127.0.0.1:18781"],
                    ),
                )
                transport.inner = httpx.ASGITransport(app=review_app)
                core_app = create_app(container=container)
                async with (
                    httpx.AsyncClient(
                        transport=transport,
                        base_url=REVIEW_URL,
                        headers={"Authorization": f"Bearer {token.get_secret_value()}"},
                        trust_env=False,
                    ) as review_client,
                    httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=core_app),
                        base_url=CORE_URL,
                        trust_env=False,
                    ) as core_client,
                ):
                    response = PocResponse(
                        ReferenceHttpClient(review_client, token=token, max_read_retries=1),
                        container,
                    )
                    container.service._dependencies = replace(
                        container.service._dependencies,
                        response=ObservedPort(
                            response,
                            container.service.observations,
                            "approval",
                            mode="mock",
                            provider_kind="reference_http",
                        ),
                    )
                    container.response = response
                    container.service.start(container.checkpoints.saver)
                    ui = create_local_ui_app(
                        allowed_hosts=[f"127.0.0.1:{port}", f"localhost:{port}"],
                        core=UpstreamTarget.in_process("core", core_app, base_url=CORE_URL),
                        review=UpstreamTarget(
                            "review",
                            UiReviewTransport(review_client, core_client),
                            REVIEW_URL,
                            token=token,
                        ),
                    )
                    async with ui.router.lifespan_context(ui):
                        holder["ui"] = ui
                        app.state.container = container
                        yield
            finally:
                holder.clear()
                await container.shutdown()

    return Starlette(routes=[Mount("/", app=delegate)], lifespan=lifespan)
