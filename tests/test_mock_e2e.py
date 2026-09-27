"""`make mock-e2e` with fake agents: the four desk scenarios pass end to end, in-process."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "mock"))


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("RFA_SG_AUDIT_DB", str(tmp_path / "audit.db"))


def test_four_scenarios_pass_with_fake_agents(tmp_path, capsys):
    import run_e2e

    rc = run_e2e.main(["--fake-agents", "--learned-file", str(tmp_path / "learned.yaml")])
    out = capsys.readouterr().out
    assert rc == 0, out
    for sid in ("01_public_ok", "02_public_date", "03_injection", "04_feedback"):
        assert f"{sid} " in out and "FAIL" not in out
    learned = (tmp_path / "learned.yaml").read_text(encoding="utf-8")
    assert "aurora-dash.intra.local" in learned and "audience: public" in learned and "task: triv3" in learned


def test_approval_mock_auto_and_manual_paths():
    import asyncio

    import httpx
    from approval import create_app

    async def run():
        auto = create_app(r"20\d\d-\d\d-\d\d")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=auto), base_url="http://a") as c:
            rejected = (await c.post("/approvals", json={"request_id": "r", "draft": "마감 2026-10-02"})).json()
            approved = (await c.post("/approvals", json={"request_id": "r2", "draft": "마감은 다음 분기"})).json()
            assert rejected["status"] == "rejected" and "2026-10-02" in rejected["reason"]
            assert approved["status"] == "approved"
        manual = create_app(None)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=manual), base_url="http://a") as c:
            item = (await c.post("/approvals", json={"request_id": "r", "draft": "x"})).json()
            assert item["status"] == "pending"
            assert (await c.post(f"/approvals/{item['id']}/reject", json={"reason": ""})).status_code == 422
            done = (await c.post(f"/approvals/{item['id']}/reject", json={"reason": "내부 주소 노출"})).json()
            assert done["status"] == "rejected" and done["reason"] == "내부 주소 노출"
            listed = (await c.get("/approvals", params={"status": "rejected"})).json()["approvals"]
            assert [i["id"] for i in listed] == [item["id"]]

    asyncio.run(run())
