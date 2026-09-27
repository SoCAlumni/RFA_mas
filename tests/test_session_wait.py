"""Session waiter: the in-sandbox poll script (run for real under this python) and the host wrapper."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from rfa_mas.nemoclaw.runner import CommandResult
from rfa_mas.nemoclaw.session_wait import WAIT_SCRIPT, SessionWaiter, parse_wait_output, session_key

AGENT, KEY, SID = "npu-sdk", "agent:npu-sdk:broker-s_1", "5c180a07-e0e4"


def entry(role: str, ts_ms: int, *, text: str | None = None, call: str | None = None, stop: str = "stop") -> str:
    parts = []
    if text is not None:
        parts.append({"type": "text", "text": text})
    if call:
        parts.append({"type": "toolCall", "id": "call_1", "name": call, "arguments": {}})
        stop = "toolUse"
    message = {"role": role, "content": parts, "stopReason": stop, "timestamp": ts_ms}
    return json.dumps({"type": "message", "id": "x", "timestamp": "2026-09-27T17:33:36.000Z", "message": message},
                      ensure_ascii=False)


def sessions_dir(root: Path, agent: str = AGENT, key: str = KEY, sid: str = SID) -> Path:
    d = root / agent / "sessions"
    d.mkdir(parents=True)
    (d / "sessions.json").write_text(json.dumps({key: {"sessionId": sid, "updatedAt": 1}}), encoding="utf-8")
    return d


def run_script(root: Path, since_ms: int, timeout_s: float, poll_s: float = 0.05, agent: str = AGENT, key: str = KEY) -> dict:
    proc = subprocess.run([sys.executable, "-", str(root), agent, key, str(since_ms), str(timeout_s), str(poll_s)],
                          input=WAIT_SCRIPT, capture_output=True, text=True, timeout=timeout_s + 10, check=True)
    return parse_wait_output(proc.stdout)


def test_script_waits_through_a_yield_until_the_final_assistant_text():
    root = Path(__import__("tempfile").mkdtemp())
    d = sessions_dir(root)
    since = 1_000_000
    transcript = d / f"{SID}.jsonl"
    transcript.write_text("\n".join([
        entry("assistant", since - 60_000, text="old answer from an earlier request"),
        entry("user", since + 100, text="⟦rfa-channel …⟧\n질문"),
        entry("assistant", since + 900, call="sessions_spawn"),
        entry("assistant", since + 1_500, call="sessions_yield"),
    ]) + "\n", encoding="utf-8")

    def finish():
        time.sleep(0.4)
        with transcript.open("a", encoding="utf-8") as fh:
            fh.write(entry("assistant", since + 20_000, text="최종 답 ⟦rfa-agent v1 …⟧\n근거: s1\n검증: pass") + "\n")

    threading.Thread(target=finish, daemon=True).start()
    out = run_script(root, since, timeout_s=5)
    assert out["state"] == "final" and out["text"].startswith("최종 답") and out["turns"] == 3
    assert 300 <= out["waited_ms"] < 5_000


def test_script_reports_timeout_while_yielded_and_ignores_answers_before_the_turn():
    root = Path(__import__("tempfile").mkdtemp())
    d = sessions_dir(root)
    since = 2_000_000
    (d / f"{SID}.jsonl").write_text("\n".join([
        entry("assistant", since - 5_000, text="previous final"),
        entry("assistant", since + 300, call="sessions_yield"),
    ]) + "\n", encoding="utf-8")
    out = run_script(root, since, timeout_s=0.3)
    assert out["state"] == "timeout" and out["last"] == "sessions_yield" and out["text"] == ""


def test_script_reports_missing_session_and_failed_turns():
    root = Path(__import__("tempfile").mkdtemp())
    sessions_dir(root)
    assert run_script(root, 0, timeout_s=0.2, key="agent:npu-sdk:broker-unknown")["state"] == "missing"
    d = root / AGENT / "sessions"
    (d / f"{SID}.jsonl").write_text(entry("assistant", 500, text="[assistant turn failed before producing content]",
                                          stop="error") + "\n", encoding="utf-8")
    out = run_script(root, 0, timeout_s=0.2)
    assert out["state"] == "error" and "failed" in out["text"]


class FakeRunner:
    def __init__(self, stdout: str, rc: int = 0):
        self.stdout, self.rc, self.calls = stdout, rc, []

    def run(self, argv, *, timeout=300, env=None, input_text=None, check=False):
        self.calls.append((list(argv), timeout, input_text))
        return CommandResult(list(argv), self.rc, self.stdout, "" if self.rc == 0 else "boom")


def test_waiter_runs_the_script_through_openshell_exec_and_parses_the_last_json_line():
    runner = FakeRunner('✓ connected\n{"state": "final", "text": "답", "turns": 2, "last": "text", "waited_ms": 8100}\n')
    waiter = SessionWaiter(runner, poll_seconds=0.5)
    out = waiter.wait_final("rfa-main", "npu-sdk", session_key("npu-sdk", "s_1"), 1234, 90)
    assert out["state"] == "final" and out["text"] == "답"
    argv, timeout, script = runner.calls[0]
    assert argv[:6] == ["openshell", "sandbox", "exec", "--name", "rfa-main", "--no-tty"]
    assert argv[argv.index("--") + 1:argv.index("--") + 3] == ["python3", "-"]
    assert argv[-5:] == ["npu-sdk", "agent:npu-sdk:broker-s_1", "1234", "90", "0.5"]
    assert script == WAIT_SCRIPT and timeout == 120 and argv[argv.index("--timeout") + 1] == "105"
    failed = SessionWaiter(FakeRunner("", rc=1)).wait_final("rfa-main", "npu-sdk", "k", 0, 30)
    assert failed["state"] == "error" and "rc=1" in failed["error"]
    assert parse_wait_output("garbage")["state"] == "error"
