"""Client of RFA_module's approvals server (contract ``RFA_module/contracts/approvals.openapi.yaml``
v0.2.0, default ``http://127.0.0.1:8790``). It owns the approval state and publishes on approve
(D-20); rfa_mas only reads it and forwards the owner's approve / reject."""

from __future__ import annotations

import httpx

from rfa_mas.nemoclaw import logs


class ApprovalsError(Exception):
    def __init__(self, status: int, code: str, message: str, detail: str = ""):
        super().__init__(message)
        self.status, self.code, self.message, self.detail = status, code, message, detail


class ApprovalsBackend:
    def __init__(self, base_url: str, *, transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = 10.0, publish_timeout: float = 90.0):
        self.base_url = base_url.rstrip("/")
        self.transport, self.timeout, self.publish_timeout = transport, timeout, publish_timeout

    async def _call(self, method: str, path: str, *, seconds: float | None = None, **kwargs):
        timer = logs.Timer()
        try:
            async with httpx.AsyncClient(base_url=self.base_url, transport=self.transport,
                                         timeout=seconds or self.timeout) as client:
                response = await client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            logs.external("approvals", type(exc).__name__, timer.ms, path=path)
            raise ApprovalsError(503, "approvals_unavailable", "결재 서버에 연결할 수 없습니다.", type(exc).__name__) from exc
        logs.external("approvals", response.status_code, timer.ms, path=path)
        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = {}
            detail = str(body.get("detail") or response.text)[:300] if isinstance(body, dict) else response.text[:300]
            code = body.get("error") if isinstance(body, dict) and isinstance(body.get("error"), str) else "approvals_error"
            message = {404: "없는 결재입니다.", 409: "지금 상태에서는 할 수 없습니다.",
                       502: "게시하지 못했습니다. 다시 시도해 주세요."}.get(response.status_code, "결재 서버가 요청을 거절했습니다.")
            raise ApprovalsError(response.status_code, code, message, detail)
        return response.json()

    async def list(self, *, status: str | None = None, task: str | None = None) -> list[dict]:
        params = {k: v for k, v in (("status", status), ("task", task)) if v}
        return await self._call("GET", "/approvals", params=params)

    async def get(self, approval_id: int) -> dict:
        return await self._call("GET", f"/approvals/{approval_id}")

    async def approve(self, approval_id: int) -> dict:
        return await self._call("POST", f"/approvals/{approval_id}/approve", seconds=self.publish_timeout)

    async def reject(self, approval_id: int, reason: str) -> dict:
        return await self._call("POST", f"/approvals/{approval_id}/reject", json={"reason": reason})
