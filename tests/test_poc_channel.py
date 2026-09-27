"""Channel gateway (P1-008M): auth, allowlist, loopback rewrite, forwarded-header strip."""

import json

import httpx
import pytest

from rfa_mas.poc.bootstrap import create_poc_app
from rfa_mas.poc.channel import ChannelGateway, load_or_create_key, route_allowed

KEY = "SYNTHETIC_CHANNEL_KEY"


def test_key_file_is_created_once_with_owner_only_mode(tmp_path):
    path = tmp_path / "keys" / "channel-api.key"
    first = load_or_create_key(path)
    assert path.stat().st_mode & 0o777 == 0o600 and len(first) >= 32
    assert load_or_create_key(path) == first
    path.write_text("")
    with pytest.raises(ValueError):
        load_or_create_key(path)


def test_route_allowlist_is_assistant_scoped():
    assert route_allowed("POST", "/channel/chat") and route_allowed("GET", "/healthz")
    for method, path in (
        ("POST", "/v1/assistant"),
        ("POST", "/v1/sessions"),
        ("POST", "/v1/sessions/session_1/work"),
        ("GET", "/v1/runs/run_1"),
        ("GET", "/v1/sessions"),
        ("POST", "/v1/knowledge/sources"),
        ("DELETE", "/v1/knowledge/sources/x"),
        ("GET", "/v1/knowledge/derived"),
        ("POST", "/v1/runs/run_1/resume"),
        ("GET", "/v1/runs/run_1/team"),
        ("POST", "/v1/sessions/../work"),
    ):
        assert not route_allowed(method, path), (method, path)


async def test_gateway_authenticates_filters_and_presents_loopback_peer():
    seen = []

    async def core(scope, receive, send):
        seen.append(scope)
        body = json.dumps({"path": scope["path"]}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})

    gateway = ChannelGateway(core, None, KEY)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway, client=("192.168.50.9", 40000)),
        base_url="http://192.168.50.1:8010",
    ) as c:
        assert (await c.get("/healthz")).status_code == 401
        assert (
            await c.get("/healthz", headers={"Authorization": "Bearer nope"})
        ).status_code == 401
        auth = {"Authorization": "Bearer " + KEY}
        denied = await c.get("/v1/sessions", headers=auth)
        assert denied.status_code == 403 and denied.json()["code"] == "channel_route_denied"
        assert seen == []  # nothing reached the core
        ok = await c.get(
            "/healthz",
            headers=auth
            | {"X-Forwarded-For": "10.0.0.1", "Forwarded": "for=10.0.0.1", "X-Real-IP": "10.0.0.1"},
        )
        assert ok.status_code == 200 and ok.json() == {"path": "/healthz"}
        scope = seen[-1]
        names = {name for name, _ in scope["headers"]}
        assert scope["client"] == ("127.0.0.1", 0)
        assert b"authorization" not in names and b"x-forwarded-for" not in names
        assert b"forwarded" not in names and b"x-real-ip" not in names


async def test_poc_serves_channel_only_on_its_listener(tmp_path):
    app = create_poc_app(tmp_path / "poc", channel=("192.168.50.1", 8010, KEY))
    async with app.router.lifespan_context(app):
        # Requests arriving on the UI listener are never routed to the gateway.
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8780",
            headers={"Origin": "http://127.0.0.1:8780"},
        ) as ui:
            assert (await ui.get("/ui/api/status")).status_code == 200
            assert (await ui.get("/healthz")).status_code == 404

        # Requests on the channel listener go through the gateway to the real core.
        async def channel_app(scope, receive, send):
            scope = {**scope, "server": ("192.168.50.1", 8010), "client": ("192.168.50.9", 1)}
            await app(scope, receive, send)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=channel_app), base_url="http://192.168.50.1:8010"
        ) as c:
            assert (await c.get("/healthz")).status_code == 401
            auth = {"Authorization": "Bearer " + KEY}
            assert (await c.get("/healthz", headers=auth)).json()["service"] == "rfa-mas"
            assert (await c.post("/channel/chat", headers=auth, json={})).status_code == 422
            stored = await c.post(
                "/channel/chat", headers=auth, json={"text": "메모: 채널 시연 노트입니다."}
            )
            assert stored.status_code == 201, stored.text
            turn = stored.json()
            assert turn["status"] == "stored" and turn["channel"] == "nemoclaw"
            assert turn["stages"][0] == "입력 이해 중" and turn["source_id"]
            asked = await c.post(
                "/channel/chat",
                headers=auth,
                json={"text": "채널 시연 노트 알려줘", "session_id": turn["session_id"]},
            )
            assert asked.status_code == 201 and "채널 시연 노트" in asked.json()["reply"]
            # The same conversation is visible in the owner's UI history.
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1:8780",
                headers={"Origin": "http://127.0.0.1:8780"},
            ) as ui:
                history = (await ui.get(f"/ui/api/sessions/{turn['session_id']}/chat")).json()
                assert [t["message_id"] for t in history] == [
                    turn["message_id"],
                    asked.json()["message_id"],
                ]
            assert (await c.get("/v1/sessions", headers=auth)).status_code == 403
            assert (
                await c.post("/v1/assistant", headers=auth, json={"text": "x"})
            ).status_code == 403
            assert (await c.get("/v1/knowledge/derived", headers=auth)).status_code == 403
