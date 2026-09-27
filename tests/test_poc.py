"""Real single-process PoC, safe defaults, restart and publication regression."""

import os
import secrets
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from rfa_mas.poc.bootstrap import create_poc_app, data_lock, local_settings


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def clean_env():
    return {
        "PATH": os.environ.get("PATH", ""),
        "LANG": "en_US.UTF-8",
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
    }


@contextmanager
def server(root, port):
    command = [sys.executable, "-m", "rfa_mas.poc", "--data-dir", str(root), "--port", str(port)]
    process = subprocess.Popen(
        command, env=clean_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    base = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(
            base_url=base,
            trust_env=False,
            timeout=5,
            headers={"Origin": base, "Sec-Fetch-Site": "same-origin"},
        ) as client:
            for _ in range(150):
                if process.poll() is not None:
                    output = process.communicate()
                    pytest.fail(f"PoC startup exited {process.returncode}: {output}")
                try:
                    ready = client.get("/ui/api/status")
                    if ready.status_code == 200 and ready.json()["review"].get("reachable"):
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.05)
            else:
                pytest.fail("PoC did not become ready")
            client.headers["X-RFA-CSRF"] = client.get("/ui/api/csrf").json()["csrf_token"]
            yield client, process
    finally:
        process.terminate()
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)


def post(client, path, body=None, key=None):
    response = client.post(
        path, json=body or {}, headers={"Idempotency-Key": key or secrets.token_hex(12)}
    )
    assert response.status_code in (200, 201), response.text
    return response.json()


def pending(client):
    session = post(client, "/ui/api/sessions")
    run = post(
        client,
        f"/ui/api/sessions/{session['session_id']}/work",
        {
            "query": "TRIV3 공개 트랙",
            "domain_id": "triv3",
            "target_audience": "public",
        },
    )
    assert run["status"] == "waiting_approval", run
    return session, run


def approve(client, draft_id):
    view = client.get(f"/ui/api/reviews/{draft_id}").json()
    assert view["contract"] == "1.1" and view["decision"] == "pending"
    return post(
        client,
        f"/ui/api/reviews/{draft_id}/decision",
        {
            "draft_version": view["version"],
            "content_hash": view["content_hash"],
            "payload_hash": view["payload_hash"],
            "target": view["target"],
            "decision": "approved",
        },
    )


def publication_body(view):
    return {
        "approval_id": view["approval"]["approval_id"],
        "draft_id": view["draft_id"],
        "version": view["version"],
        "payload_hash": view["payload_hash"],
    }


def test_poc_process_manual_approval_publish_restart_and_security(tmp_path):
    root, port = tmp_path / "poc", free_port()
    with server(root, port) as (client, _):
        assert "로컬 UI" in client.get("/ui/").text
        status = client.get("/ui/api/status").json()
        assert status["review"]["mode"] == "mock"
        assert status["features"]["openshell"] == "disabled"
        assert client.post("/ui/api/sessions", headers={"X-RFA-CSRF": ""}).status_code == 403
        assert client.get("/ui/", headers={"Host": "attacker.example"}).status_code == 400
        assert client.get("/ui/", headers={"Origin": "https://attacker.example"}).status_code == 403
        # Only the UI is exposed; internal core/service APIs cannot bypass its boundary.
        assert client.get("/v1/sessions").status_code == 404
        note = post(
            client,
            "/ui/api/notes",
            {"domain_id": "triv3", "title": "POC 비공개", "content": "POC-PRIVATE-CANARY-901"},
        )
        assert note["document"]["audience"] == "private"
        session, run = pending(client)
        assert "POC-PRIVATE-CANARY-901" not in str(run)
        draft_id, run_id = run["draft"]["draft_id"], run["run_id"]
        denied = client.post(
            "/ui/api/publications",
            json={
                "draft_id": draft_id,
                "version": 1,
                "payload_hash": "a" * 64,
                "approval_id": "unapproved",
            },
            headers={"Idempotency-Key": "premature"},
        )
        assert denied.status_code == 409
        # Persist an unapproved interrupted run, then restart the actual Python server.
    with server(root, port) as (client, _):
        assert session["session_id"] in [
            s["session_id"] for s in client.get("/ui/api/sessions").json()
        ]
        assert client.get(f"/ui/api/runs/{run_id}").json()["status"] == "waiting_approval"
        approved = approve(client, draft_id)
        done = post(client, f"/ui/api/runs/{run_id}/refresh-review")
        assert done["status"] == "completed", done
        body = publication_body(approved)
        wrong = client.post(
            "/ui/api/publications",
            json=body | {"version": 2},
            headers={"Idempotency-Key": "wrong-version"},
        )
        assert wrong.status_code == 409
        receipt = post(client, "/ui/api/publications", body, "poc-publication")
        assert receipt["receipt"]["status"] == "succeeded", receipt
        assert receipt["receipt"]["mode"] == "mock" and not receipt["external_write_performed"]
        again = post(client, "/ui/api/publications", body, "poc-publication")
        assert receipt["receipt"]["publication_id"] == again["receipt"]["publication_id"]
        # Wrong request key must not create another publication.
        assert (
            client.post(
                "/ui/api/publications", json=body, headers={"Idempotency-Key": "different-key"}
            ).status_code
            == 409
        )
    with server(root, port) as (client, _):
        stored = client.get(f"/ui/api/publications/{receipt['receipt']['publication_id']}")
        assert stored.status_code == 200 and stored.json()["receipt"] == receipt["receipt"]
        assert (
            post(client, "/ui/api/publications", body, "poc-publication")["receipt"]
            == receipt["receipt"]
        )
        detail = client.get(f"/ui/api/sessions/{session['session_id']}").json()
        assert any(r["run_id"] == run_id and r["status"] == "completed" for r in detail["runs"])
    assert (root / "core" / "rfa.db").is_file() and (root / "review" / "review.db").is_file()
    assert "POC-PRIVATE-CANARY-901" not in "".join(
        p.read_text() for p in (root / "traces").glob("*.jsonl")
    )


def test_ambient_env_cannot_enable_real_providers(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "nvidia")
    monkeypatch.setenv("ALLOW_EXTERNAL_WRITES", "true")
    monkeypatch.setenv("ALLOW_EXTERNAL_EGRESS", "true")
    monkeypatch.setenv("RUNTIME_BACKEND", "http")
    monkeypatch.setenv("NVIDIA_API_KEY", "synthetic-not-a-key")
    config = local_settings(tmp_path, SecretStr(secrets.token_urlsafe(32)))
    assert config.model_provider == "mock" and config.runtime_backend == "local"
    assert not config.allow_external_writes and not config.allow_external_egress
    assert config.nvidia_api_key is None


def test_data_lock_rejects_concurrent_owner_and_releases(tmp_path):
    root = tmp_path / "lock"
    with data_lock(root), pytest.raises(RuntimeError, match="poc_data_dir_in_use"):  # noqa: SIM117
        with data_lock(root):
            pytest.fail("duplicate process allowed")
    with data_lock(root):
        pass


def test_port_collision_fails_without_creating_storage(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "rfa_mas.poc",
                "--port",
                str(port),
                "--data-dir",
                str(tmp_path / "unused"),
            ],
            env=clean_env(),
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert process.returncode == 2 and "poc_port_unavailable" in process.stderr
        assert not (tmp_path / "unused").exists()


def test_launcher_help_and_invalid_port():
    result = subprocess.run(
        [sys.executable, "-m", "rfa_mas.poc", "--help"],
        env=clean_env(),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0 and "--data-dir" in result.stdout
    with pytest.raises(ValueError, match="invalid_port"):
        create_poc_app(Path("unused"), port=0)


async def test_acl_revocation_blocks_publication_at_core_boundary(tmp_path):
    app = create_poc_app(tmp_path / "guard")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8780",
            headers={"Origin": "http://127.0.0.1:8780"},
        ) as client,
    ):
        client.headers["X-RFA-CSRF"] = (await client.get("/ui/api/csrf")).json()["csrf_token"]
        session = (await client.post("/ui/api/sessions", json={})).json()
        run = (
            await client.post(
                f"/ui/api/sessions/{session['session_id']}/work",
                json={
                    "query": "TRIV3 공개 트랙",
                    "domain_id": "triv3",
                    "target_audience": "public",
                },
            )
        ).json()
        assert run["status"] == "waiting_approval", run
        draft_id = run["draft"]["draft_id"]
        view = (await client.get(f"/ui/api/reviews/{draft_id}")).json()
        approval = (
            await client.post(
                f"/ui/api/reviews/{draft_id}/decision",
                json={
                    "draft_version": view["version"],
                    "content_hash": view["content_hash"],
                    "payload_hash": view["payload_hash"],
                    "target": view["target"],
                    "decision": "approved",
                },
                headers={"Idempotency-Key": "approve"},
            )
        ).json()
        # A trusted policy version change after approval must be checked at publication.
        app.state.container.policy.policy_version = "revoked-policy-v2"
        response = await client.post(
            "/ui/api/publications",
            json=publication_body(approval),
            headers={"Idempotency-Key": "after-revoke"},
        )
        assert response.status_code == 409, response.text
        owner = await app.state.container.repository.local_principal()
        assert await app.state.container.repository.get_publication(run["run_id"], owner) is None
