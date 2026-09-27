"""Opt-in live replay: NemoClaw sandbox agent calls this service's restricted API (P1-007A).

Requires an onboarded NemoClaw sandbox with the `rfa-api-minimal` preset applied and the
RFA API running on the host at the preset's non-loopback address (see
docs/evidence/nemoclaw.md §8A). Probes run inside the sandbox through `nemoclaw <name> exec`;
a denial counts only when the OpenShell proxy answers `policy_denied`. Nothing here sandboxes
the RFA backend itself.

Run explicitly (skip is not success):
    RFA_NEMOCLAW_LIVE=1 RFA_NEMOCLAW_SANDBOX=rfa-demo RFA_P1007A_ENV_FILE=/abs/.env.p1007a \
    .venv/bin/python -m pytest -q tests/integration/test_nemoclaw_live.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from rfa_mas.settings import Settings

LIVE = os.environ.get("RFA_NEMOCLAW_LIVE") == "1"
SANDBOX = os.environ.get("RFA_NEMOCLAW_SANDBOX")
ENV_FILE = os.environ.get("RFA_P1007A_ENV_FILE")
EVIDENCE_OUT = os.environ.get("RFA_NEMOCLAW_EVIDENCE_OUT")

pytestmark = pytest.mark.skipif(
    not (LIVE and SANDBOX and ENV_FILE and shutil.which("nemoclaw")),
    reason="opt-in live NemoClaw sandbox replay; skipped run is not evidence",
)

ALLOWED = ("healthz", "session_create", "work_owner", "work_public", "run_get")
DENIED = (
    "knowledge_write",
    "sessions_list",
    "knowledge_delete",
    "candidate_decision",
    "knowledge_derived",
    "example_com",
    "other_port",
)
PROBES_SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "nemoclaw" / "probes.sh"


def _nemoclaw(*args: str, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["nemoclaw", SANDBOX or "", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


@pytest.fixture(scope="module")
def replay() -> dict:
    settings = Settings(_env_file=Path(ENV_FILE or ""))
    if settings.is_loopback_bind or settings.app_api_key is None:
        pytest.fail("the P1-007A profile must bind a non-loopback host with APP_API_KEY")
    base = f"http://{settings.app_host}:{settings.app_port}"
    other_port = settings.app_port + 1 if settings.app_port != 8000 else 8001
    other = f"http://{settings.app_host}:{other_port}/healthz"
    key = settings.app_api_key.get_secret_value()
    with tempfile.TemporaryDirectory() as tmp:
        header = Path(tmp) / "rfa-auth.hdr"
        header.write_text(f"Authorization: Bearer {key}\n")
        header.chmod(0o600)
        script = Path(tmp) / "probes.sh"
        script.write_text(
            PROBES_SCRIPT.read_text().replace("__BASE__", base).replace("__OTHER__", other)
        )
        for src, dst in ((header, "/sandbox/rfa-auth.hdr"), (script, "/sandbox/rfa-probes.sh")):
            up = _nemoclaw("upload", str(src), dst, timeout=120)
            if up.returncode != 0:
                pytest.fail(f"upload of {dst} failed (rc={up.returncode})")
    _nemoclaw("exec", "--timeout", "30", "--", "chmod", "600", "/sandbox/rfa-auth.hdr", timeout=60)
    run = _nemoclaw("exec", "--timeout", "300", "--", "sh", "/sandbox/rfa-probes.sh")
    if run.returncode != 0:
        pytest.fail(f"probe run failed rc={run.returncode}")
    rows = {}
    for line in run.stdout.splitlines():
        if line.startswith("{"):
            row = json.loads(line)
            rows[row["probe"]] = row
    for row in rows.values():
        if key in json.dumps(row):
            pytest.fail("credential leaked into probe output")
    logs = _nemoclaw("logs", "--tail", "200", timeout=120).stdout
    result = {
        "sandbox": SANDBOX,
        "base": base,
        "rows": rows,
        "ocsf": [line for line in logs.splitlines() if "[OCSF ]" in line and "HTTP:" in line],
    }
    if EVIDENCE_OUT:
        text = json.dumps(result, ensure_ascii=False, indent=2)
        if key in text:
            pytest.fail("evidence would contain the credential; not written")
        Path(EVIDENCE_OUT).write_text(text + "\n", encoding="utf-8")
    return result


def test_allowed_routes_reach_the_service(replay: dict) -> None:
    rows = replay["rows"]
    assert rows["healthz"]["http"] == "200" and '"service": "rfa-mas"' in rows["healthz"][
        "body"
    ].replace('":"', '": "')
    assert rows["session_create"]["http"] == "201"
    for name in ("work_owner", "work_public"):
        assert rows[name]["http"] == "201", name
        assert (
            '"status":"completed"' in rows[name]["body"]
            or '"status": "completed"' in rows[name]["body"]
        )
    assert "SYNTHETIC_PRIVATE_CANARY" not in rows["work_public"]["body"]
    assert rows["run_get"]["http"] == "200"


def test_other_routes_and_hosts_are_denied_by_openshell_policy(replay: dict) -> None:
    rows = replay["rows"]
    for name in DENIED[:5]:
        assert rows[name]["http"] == "403" and "policy_denied" in rows[name]["body"], name
    assert rows["example_com"]["http"] == "000" and rows["example_com"]["curl_exit"] != 0
    assert rows["other_port"]["http"] in {"403", "000"}


def test_ocsf_log_records_matching_decisions(replay: dict) -> None:
    ocsf = replay["ocsf"]
    allowed = [line for line in ocsf if "ALLOWED" in line and "/healthz" in line]
    denied = [
        line for line in ocsf if "DENIED" in line and "/v1/sessions" in line and "GET" in line
    ]
    assert allowed and denied
    assert all("nemoclaw_custom__rfa-api-minimal" in line for line in allowed)
