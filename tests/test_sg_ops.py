"""Ops helpers: blocked-request parsing/approval presets, relocation policy, assignments editing."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from rfa_mas.nemoclaw import audit
from rfa_mas.nemoclaw import config as cfg
from rfa_mas.nemoclaw.censor import CensorPipeline
from rfa_mas.nemoclaw.relocate import direction_for, rewrite_agent_groups, scan_workspace, target_sandbox
from rfa_mas.nemoclaw.requests import approve, deny, list_requests, parse_denied, sync
from rfa_mas.nemoclaw.runner import CommandResult

OCSF = [
    "[OCSF ] NET:OPEN [MED] DENIED /usr/bin/curl(4711) -> example.com:443 [reason:not_allowed_by_any_policy]",
    "[OCSF ] HTTP:REQUEST [MED] DENIED /usr/bin/curl(4720) -> GET http://192.168.123.191:8000/healthz [reason:endpoint_not_allowed]",
    "[OCSF ] HTTP:REQUEST [LOW] ALLOWED /usr/bin/curl(4654) -> GET http://192.168.123.191:8010/healthz [policy:x engine:l7]",
    "[OCSF ] NET:OPEN [MED] DENIED /usr/bin/curl(4711) -> example.com:443 [reason:not_allowed_by_any_policy]",
]


class LogRunner:
    def __init__(self):
        self.calls = []

    def run(self, argv, *, timeout=300, env=None, input_text=None, check=False):
        self.calls.append(list(argv))
        if argv[2:4] == ["logs", "--tail"]:
            return CommandResult(list(argv), 0, "\n".join(OCSF), "")
        return CommandResult(list(argv), 0, "applied", "")


@pytest.fixture(autouse=True)
def audit_db(tmp_path, monkeypatch):
    monkeypatch.setenv("RFA_SG_AUDIT_DB", str(tmp_path / "audit.db"))


def test_parse_denied_handles_l4_and_l7_lines():
    l4 = parse_denied(OCSF[0], "sb")
    assert (l4["host"], l4["port"], l4["method"], l4["path"], l4["binary"], l4["reason"]) == (
        "example.com", 443, "", "", "/usr/bin/curl", "not_allowed_by_any_policy")
    l7 = parse_denied(OCSF[1], "sb")
    assert (l7["host"], l7["port"], l7["method"], l7["path"]) == ("192.168.123.191", 8000, "GET", "/healthz")
    assert parse_denied(OCSF[2], "sb") is None


def test_sync_dedupes_and_records_then_approve_builds_a_preset(tmp_path, monkeypatch):
    monkeypatch.setattr("rfa_mas.nemoclaw.bootstrap.SG_DIR", tmp_path / "sg")
    runner = LogRunner()
    new = sync(runner, ["rfa-tasks-none"], "nemoclaw", tail=100)
    assert sorted(n["host"] for n in new) == ["192.168.123.191", "example.com"]
    assert sync(runner, ["rfa-tasks-none"], "nemoclaw") == []  # second sync: hits counted, nothing new
    pending = list_requests()
    assert len(pending) == 2 and {p["hits"] for p in pending} == {2, 4}  # duplicate line counted twice per sync
    assert [e["verdict"] for e in audit.query(kind="request")] == ["denied", "denied"]
    private = next(p for p in pending if p["host"].startswith("192.168."))
    result = approve(private["id"], "demo host", runner, "nemoclaw", "192.168.123.191")
    assert result["status"] == "approved" and result["preset"] == f"sg-approved-{private['id']}"
    argv = runner.calls[-1]
    assert argv[2:5] == ["policy", "add", "--from-file"] and argv[-2:] == ["--trusted-private-host", "192.168.123.191"]
    preset = yaml.safe_load(Path(argv[5]).read_text())
    entry = preset["network_policies"][f"sg_approved_{private['id']}"]
    assert entry["endpoints"][0]["rules"] == [{"allow": {"method": "GET", "path": "/healthz"}}]
    assert entry["binaries"] == [{"path": "/usr/bin/curl"}]
    public = next(p for p in pending if p["host"] == "example.com")
    assert deny(public["id"], "not needed")["status"] == "denied"
    assert [p["status"] for p in list_requests(None)] == ["approved", "denied"] or sorted(p["status"] for p in list_requests(None)) == ["approved", "denied"]
    assert {e["verdict"] for e in audit.query(kind="approval")} == {"approved", "denied"}


def test_relocation_direction_and_target():
    a = cfg.load_assignments()
    assert direction_for(a, "rfa-tasks-intranet", "rfa-tasks-none") == "demote"
    assert direction_for(a, "rfa-tasks-none", "rfa-tasks-intranet") == "promote"
    assert direction_for(a, "rfa-tasks-none", "rfa-tasks-none") == "lateral"
    assert target_sandbox(a, ["egress-none"]) == "rfa-tasks-none"
    with pytest.raises(cfg.ConfigError):
        target_sandbox(a, ["control-plane"])  # fixed sandbox, not a task target


def test_rewrite_agent_groups_keeps_comments_and_other_agents(tmp_path):
    src = cfg.DEPLOY_DIR / "assignments.yaml"
    copy = tmp_path / "assignments.yaml"
    copy.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    rewrite_agent_groups(copy, "research", ["egress-none"])
    text = copy.read_text(encoding="utf-8")
    assert "# 보안 그룹 배치 선언" in text  # header comment preserved
    loaded = cfg.load_assignments(copy)
    assert loaded.agents["research"].groups == ["egress-none"] and loaded.agents["benchmark"].groups == ["intranet-ro"]
    assert loaded.sandbox_for("research") == "rfa-tasks-none"
    with pytest.raises(cfg.ConfigError):
        rewrite_agent_groups(copy, "assistant", ["x"])  # fixed agents have no groups line


def test_scan_workspace_redacts_text_and_reports_blocked_files(tmp_path):
    ws = tmp_path / "workspace"
    (ws / "memory").mkdir(parents=True)
    (ws / "memo.md").write_text("오로라 P95 12.5 ms 예산 1,200,000 원", encoding="utf-8")
    (ws / "memory" / "2026-09-27.md").write_text("평범한 일지", encoding="utf-8")
    (ws / "IDENTITY.md").write_text("marker file must not be rewritten 12.5 ms", encoding="utf-8")
    (ws / "blob.bin").write_bytes(b"\x00\x01")
    pipeline = CensorPipeline(cfg.load_censors(), None)
    report = scan_workspace(ws, pipeline)
    assert report.files == 4 and report.scanned == 2 and report.redacted_files == 1 and report.redactions == 3
    assert "[REDACTED:project]" in (ws / "memo.md").read_text() and "12.5" in (ws / "IDENTITY.md").read_text()
    (ws / "secret.txt").write_text("token nvapi-abcdefghijklmnop123456", encoding="utf-8")
    assert scan_workspace(ws, pipeline).blocked == ["secret.txt"]
