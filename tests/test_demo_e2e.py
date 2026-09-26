"""P0-026 V1: `rfa demo --full` as a real subprocess on a temp data dir (offline, mock/local).

The child gets a minimal environment (no inherited RFA_/MODEL_/NVIDIA_ variables, no .env in
its working directory) and a sitecustomize that refuses and logs every IP socket connect and
DNS lookup, so "no network" is checked, not assumed. Assertions read the printed report, the
temp trace files and the child's own network log; nothing here reads private material text
from the report because the report must not contain any.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MATERIALS = REPO / "fixtures" / "demo" / "materials.jsonl"
TIMEOUT_SECONDS = 90
STEPS = [
    "ingest",
    "derive_and_candidates",
    "benchmark_team_task",
    "follow_up_reuses_team",
    "owner_internal_status_answer",
    "external_public_draft",
    "edit_invalidates_approval",
    "re_review",
    "publication",
    "scheduled_briefing",
    "restart_same_database",
]
NO_NETWORK = """
import os, socket
_log = os.environ["RFA_DEMO_NET_LOG"]
def _refuse(kind):
    with open(_log, "a", encoding="utf-8") as handle:
        handle.write(kind + "\\n")
    raise OSError("network disabled for the offline demo test: " + kind)
_connect, _connect_ex = socket.socket.connect, socket.socket.connect_ex
def connect(sock, address, *args):
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        _refuse("connect")
    return _connect(sock, address, *args)
def connect_ex(sock, address, *args):
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        _refuse("connect_ex")
    return _connect_ex(sock, address, *args)
socket.socket.connect, socket.socket.connect_ex = connect, connect_ex
socket.getaddrinfo = lambda *a, **k: _refuse("getaddrinfo")
socket.create_connection = lambda *a, **k: _refuse("create_connection")
"""


def materials() -> list[dict]:
    return [json.loads(line) for line in MATERIALS.read_text(encoding="utf-8").splitlines() if line]


def markers(kind: str) -> list[str]:
    return sorted({m for item in materials() for m in item.get("markers", {}).get(kind, [])})


def run_demo(root: Path, *args: str) -> tuple[subprocess.CompletedProcess, float]:
    site = root / "site"
    site.mkdir(exist_ok=True)
    (site / "sitecustomize.py").write_text(NO_NETWORK, encoding="utf-8")
    env = {
        "PATH": os.defpath,
        "HOME": str(root),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(site),
        "RFA_DEMO_NET_LOG": str(root / "network.log"),
    }
    start = time.perf_counter()
    process = subprocess.run(
        [sys.executable, "-m", "rfa_mas", "demo", *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )
    return process, time.perf_counter() - start


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    root = tmp_path_factory.mktemp("demo").resolve()
    data = root / "data"
    process, elapsed = run_demo(root, "--full", "--data-dir", str(data))
    report = json.loads(process.stdout) if process.stdout.strip().startswith("{") else None
    return {"process": process, "report": report, "data": data, "root": root, "elapsed": elapsed}


def steps(demo) -> dict[str, dict]:
    return {step["name"]: step for step in demo["report"]["steps"]}


def test_full_demo_runs_offline_on_a_temp_data_dir(demo):
    process, report = demo["process"], demo["report"]
    assert process.returncode == 0, process.stdout[-2000:]
    assert report is not None and report["ok"] is True
    assert report["checks"] and all(report["checks"].values()), report["checks"]
    assert [step["name"] for step in report["steps"]] == STEPS
    assert Path(report["data_dir"]) == demo["data"] and (demo["data"] / "rfa.db").is_file()
    assert not (demo["root"] / "network.log").exists()  # no IP socket or DNS in the child
    assert demo["elapsed"] < TIMEOUT_SECONDS


def test_every_step_is_labelled_mock_local_or_simulated_honestly(demo):
    report = demo["report"]
    assert {step["mode"] for step in report["steps"]} <= {"local", "mock", "simulated"}
    adapters = {a["port"]: a for a in report["adapters"]}
    assert adapters["model"]["adapter"] == "mock-model" and adapters["model"]["simulated"]
    assert adapters["response"]["simulated"]
    by_name = steps(demo)
    for name in ("owner_internal_status_answer", "external_public_draft"):
        assert by_name[name]["mode"] == "mock" and by_name[name]["run"]["simulated"] is True
    for name in ("benchmark_team_task", "follow_up_reuses_team"):
        team = by_name[name]["team"]
        assert by_name[name]["mode"] == "simulated"
        assert team["experiment"]["simulated_experiment"] is True
        assert team["experiment"]["mode"] == "fixture_log_parse"
        assert team["analysis_simulated"] is True and team["tokens"] is None  # unknown, not 0
    assert by_name["publication"]["receipt"]["mode"] == "mock"
    assert by_name["publication"]["receipt"]["external_result_ref"].startswith("local-artifact:")
    assert "manual" in by_name["scheduled_briefing"]["clock"]
    assert {"real_model", "real_publication_or_teammate_service", "ui"} <= set(report["not_run"])


def test_benchmark_team_is_reused_by_the_follow_up(demo):
    by_name = steps(demo)
    first, follow = by_name["benchmark_team_task"], by_name["follow_up_reuses_team"]
    assert first["team"]["pattern"] == "benchmark" and first["team"]["status"] == "completed"
    assert (follow["team"]["task_id"], follow["team"]["team_id"]) == (
        first["team"]["task_id"],
        first["team"]["team_id"],
    )
    assert follow["run"]["run_id"] != first["run"]["run_id"]
    assert follow["run"]["status"] == first["run"]["status"] == "completed"
    runs = {r["label"]: r for r in first["team"]["experiment"]["runs"]}
    assert (runs["A"]["latency_ms"], runs["A"]["accuracy_pct"]) == (10.0, 81.0)
    assert (runs["B"]["latency_ms"], runs["B"]["accuracy_pct"]) == (8.2, 80.8)
    assert first["team"]["comparisons"] == [
        {"latency_change_pct": -18.0, "accuracy_delta_pp": -0.2, "same_environment": True}
    ]


def test_candidates_and_owner_answer_stay_inside_their_audiences(demo):
    by_name = steps(demo)
    candidates = by_name["derive_and_candidates"]["candidates"]
    assert any(
        c["parents"] == ["issue_export"] and c["due_date"] == "2026-10-02" and c["blocker"]
        for c in candidates
    )
    evidence = by_name["owner_internal_status_answer"]["run"]["evidence"]
    assert any(e["audience"] == "owner" for e in evidence)
    assert all(e["audience"] != "private" for e in evidence)
    assert not any("private" in e["material"] for e in evidence)


def test_public_draft_uses_only_public_faq_evidence(demo):
    step = steps(demo)["external_public_draft"]
    assert step["intent"] == "external_draft" and step["run"]["target"] == "public"
    evidence = step["run"]["evidence"]
    assert evidence and all(e["audience"] == "public" for e in evidence)
    assert {e["material"] for e in evidence} <= {"public_faq", "derived:public_faq"}


def test_edit_invalidates_the_approval_then_exactly_one_publication(demo):
    by_name = steps(demo)
    edit = by_name["edit_invalidates_approval"]
    assert edit["before"]["approval_valid"] is True
    assert edit["after"] == {
        "version": edit["before"]["version"] + 1,
        "approval_valid": False,
        "invalid_reason": "draft_changed",
    }
    assert edit["publish_attempt"] == {"http_status": 409, "code": "approval_required"}
    review = by_name["re_review"]
    assert review["approval_valid"] is True
    assert review["approval"]["draft_version"] == review["version"] == edit["after"]["version"]
    publication = by_name["publication"]
    assert publication["receipt"]["status"] == "succeeded"
    assert publication["receipt"]["version"] == review["version"]
    assert publication["replay_same_receipt"] is True
    assert publication["second_key"] == {"http_status": 409, "code": "publication_exists"}
    counts = by_name["restart_same_database"]["database_counts"]
    assert counts["publications"] == counts["publication_ledger_entries"] == 1


def test_scheduled_briefing_fires_once_despite_a_duplicate_tick(demo):
    step = steps(demo)["scheduled_briefing"]
    assert [r["status"] for r in step["runs"]] == ["succeeded"]
    assert any(i["due_date"] == "2026-10-02" and i["blocker"] for i in step["briefing_items"])


def test_restart_on_the_same_database_preserves_state(demo):
    by_name = steps(demo)
    after = by_name["restart_same_database"]["after_restart"]
    first = by_name["benchmark_team_task"]["team"]
    assert after["session_listed"] is True and after["benchmark_run_status"] == "completed"
    assert after["team_ref"] == [first["task_id"], first["team_id"]]
    assert after["publication_status"] == "succeeded"
    assert after["publication_id"] == by_name["publication"]["receipt"]["publication_id"]
    assert after["replay_same_receipt"] is True
    assert after["draft_version"] == by_name["re_review"]["version"]
    assert after["schedule_runs"] == 1


def test_no_private_canary_or_material_text_in_any_output(demo):
    process, report = demo["process"], demo["report"]
    traces = "".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in sorted((demo["data"] / "traces").rglob("*"))
        if p.is_file()
    )
    outputs = process.stdout + process.stderr + traces
    assert private_free(outputs)
    # The report carries IDs, statuses and refs only: no material text at all.
    assert "[합성 demo]" not in process.stdout
    assert report["privacy"]["private_marker_hits"] == 0
    assert report["privacy"]["public_output_internal_or_private_hits"] == 0
    assert report["privacy"]["responses_scanned"] > 0 and report["privacy"]["trace_files_scanned"]


def private_free(text: str) -> bool:
    return bool(markers("private")) and not any(m in text for m in markers("private"))


def test_the_child_network_block_is_active(tmp_path):
    """Guard self-test: the same child environment refuses DNS before any lookup."""
    root = tmp_path.resolve()
    run_demo(root, "--help")  # writes the sitecustomize into root/site
    probe = subprocess.run(
        [sys.executable, "-c", "import socket; socket.getaddrinfo('example.invalid', 443)"],
        cwd=root,
        env={
            "PATH": os.defpath,
            "PYTHONPATH": str(root / "site"),
            "RFA_DEMO_NET_LOG": str(root / "network.log"),
        },
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )
    assert probe.returncode != 0 and "network disabled" in probe.stderr
    assert (root / "network.log").read_text(encoding="utf-8") == "getaddrinfo\n"


def test_full_demo_refuses_a_non_empty_data_dir_and_an_env_file(tmp_path):
    root = tmp_path.resolve()
    used = root / "used"
    used.mkdir()
    (used / "keep.txt").write_text("user data", encoding="utf-8")
    refused, _ = run_demo(root, "--full", "--data-dir", str(used))
    assert refused.returncode == 2
    assert json.loads(refused.stdout)["code"] == "demo_data_dir_not_empty"
    assert (used / "keep.txt").read_text(encoding="utf-8") == "user data"
    env_file = root / "synthetic.env"
    env_file.write_text("MODEL_PROVIDER=mock\n", encoding="utf-8")
    rejected = subprocess.run(
        [sys.executable, "-m", "rfa_mas", "--env-file", str(env_file), "demo", "--full"],
        cwd=root,
        env={"PATH": os.defpath, "HOME": str(root)},
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )
    assert rejected.returncode == 2
    assert json.loads(rejected.stdout)["code"] == "demo_configuration_rejected"
