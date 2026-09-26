"""E2E-05 OpenShell allow/deny evidence (P1-007C).

Default runs execute only deterministic checks: the classification rules that keep a
missing file, a down endpoint or a generic error from being counted as a policy denial,
and the application SupervisorBus boundary (no OpenShell involved).

The real OpenShell matrix is opt-in and needs a running local gateway (v0.1.1, VM driver)
plus the stand-in rootfs from deploy/openshell/build_rootfs.sh. A skipped live test is
not_run, never a pass:

    RFA_OPENSHELL_LIVE=1 RFA_OPENSHELL_ROOTFS=/abs/rfa-e2e05-rootfs.tar \
    RFA_OPENSHELL_EVIDENCE_OUT=/abs/out-dir \
    .venv/bin/python -m pytest -q tests/integration/test_openshell_live.py
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "openshell_e2e05.py"
_spec = importlib.util.spec_from_file_location("openshell_e2e05", SCRIPT)
assert _spec and _spec.loader
e2e05 = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = e2e05  # dataclasses resolve their module during class creation
_spec.loader.exec_module(e2e05)

LIVE_ENABLED = os.environ.get("RFA_OPENSHELL_LIVE") == "1"
ROOTFS = os.environ.get("RFA_OPENSHELL_ROOTFS")
EVIDENCE_OUT = os.environ.get("RFA_OPENSHELL_EVIDENCE_OUT")

LOG = "NET:OPEN [MED] DENIED /usr/local/bin/python3.12(0) -> 192.0.2.10:9 [reason:x]"


def test_network_deny_requires_policy_record_and_healthy_endpoint() -> None:
    failed = {"ok": False, "errno": 13}
    assert e2e05.classify_network("deny", failed, LOG, True) == "denied_by_policy"
    # Generic failure without an OpenShell DENIED record is not a policy denial.
    assert e2e05.classify_network("deny", failed, None, True) == "inconclusive"
    # A down endpoint makes the attempt inconclusive even if a record exists.
    assert e2e05.classify_network("deny", failed, LOG, False) == "inconclusive"
    # A deny probe that succeeds is reported as allowed (a security failure).
    assert e2e05.classify_network("deny", {"ok": True}, None, True) == "allowed"


def test_network_allow_requires_openshell_allowed_record() -> None:
    assert e2e05.classify_network("allow", {"ok": True}, "ALLOWED ...", True) == "allowed"
    assert e2e05.classify_network("allow", {"ok": True}, None, True) == "inconclusive"


def test_filesystem_absence_or_unproven_permission_error_is_inconclusive() -> None:
    assert e2e05.classify_filesystem({"ok": False, "errno": 2}, True) == "inconclusive"
    assert e2e05.classify_filesystem({"ok": False, "errno": 13}, False) == "inconclusive"
    assert e2e05.classify_filesystem({"ok": False, "errno": 13}, True) == "denied_by_policy"
    assert e2e05.classify_filesystem({"ok": True}, True) == "allowed"


def test_supervisor_bus_boundary_is_measured_without_openshell() -> None:
    bus = e2e05.supervisor_bus_decisions()
    assert "no OpenShell" in bus["layer"]
    assert all(row["met"] for row in bus["rows"])
    decisions = {(r["sender"], r["recipient"]): r["decision"] for r in bus["rows"]}
    assert decisions[("paper_scout", "experiment_runner")] == "denied"
    assert decisions[("paper_scout", "supervisor")] == "delivered"
    assert bus["delivered_log"] == [
        ["paper_scout", "supervisor"],
        ["supervisor", "experiment_runner"],
    ]


@pytest.mark.skipif(
    not (LIVE_ENABLED and ROOTFS),
    reason="not_run: opt-in real OpenShell E2E-05 (set RFA_OPENSHELL_LIVE=1 and "
    "RFA_OPENSHELL_ROOTFS); a skipped live test is not a pass",
)
def test_live_e2e05_matrix(tmp_path: Path) -> None:
    if shutil.which("openshell") is None:
        pytest.fail("RFA_OPENSHELL_LIVE=1 but the openshell CLI is not installed")
    out = Path(EVIDENCE_OUT) if EVIDENCE_OUT else tmp_path
    args = argparse.Namespace(
        rootfs=ROOTFS,
        out=str(out),
        host_ip=None,
        iterations=3,
        warm_repeats=5,
        deny_repeats=3,
        openshell=shutil.which("openshell"),
    )
    report = e2e05.run(args)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    (out / "matrix.md").write_text(e2e05.matrix_markdown(report))
    assert report["status"] != "not_run", report.get("reasons")
    assert "local standalone" in report["label"] and "P1-008B" in report["label"]
    assert report["canary_absent_from_report"] is True
    assert report["controls"]["research_control"]["proves"] is True
    assert report["controls"]["execution_control"]["proves_tool"] is True
    assert report["controls"]["execution_control"]["proves_tmp"] is True
    unmet = [(r["step"], r["expected"], r["observed"]) for r in report["matrix"] if not r["met"]]
    assert not unmet, unmet
    assert all(r["met"] for r in report["supervisor_bus"]["rows"])
    assert report["status"] == "passed"
