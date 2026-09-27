"""Wait for a task agent's *final* answer after its gateway turn ended early.

A team supervisor spawns its members with OpenClaw ``sessions_spawn`` and ends its turn with
``sessions_yield``: subagent completion is push-based, so the gateway ``/v1/chat/completions``
request (and ``nemoclaw agent``) returns at the yield with no payload ("No response from OpenClaw.")
while the real answer is written to the session transcript up to ~30 s later, in follow-up runs the
announce events start. There is no HTTP/CLI call that waits for those runs, so the broker watches the
transcript: ``WAIT_SCRIPT`` runs inside the sandbox (``openshell sandbox exec``, gRPC, ~50 ms, no
NemoClaw host lock, safe to run concurrently) and polls
``/sandbox/.openclaw/agents/<agent>/sessions/<session>.jsonl`` until the last assistant message after
the turn started is a plain text reply (no pending tool call, not a yield), then prints it as JSON.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from rfa_mas.nemoclaw.runner import Runner

AGENTS_ROOT = "/sandbox/.openclaw/agents"
NO_RESPONSE = "No response from OpenClaw."   # gateway text when a run ends without payloads (e.g. after a yield)

# Runs under the sandbox python3 (stdlib only). argv: root agent_id session_key since_ms timeout_s poll_s.
# One JSON line on stdout: {"state": final|error|timeout|missing, "text", "waited_ms", "turns", "last"}.
WAIT_SCRIPT = r'''
import json, sys, time
from datetime import datetime, timezone

root, agent, key, since_ms, timeout_s, poll_s = sys.argv[1:7]
since_ms, timeout_s, poll_s = int(since_ms) - 1500, float(timeout_s), float(poll_s)
deadline = time.monotonic() + timeout_s
started = time.monotonic()


def transcript_path():
    try:
        with open(f"{root}/{agent}/sessions/sessions.json", encoding="utf-8") as fh:
            entry = json.load(fh).get(key) or {}
    except (OSError, ValueError):
        return None
    sid = entry.get("sessionId")
    return f"{root}/{agent}/sessions/{sid}.jsonl" if sid else None


def stamp(d, m):
    ts = m.get("timestamp")
    if isinstance(ts, (int, float)):
        return int(ts)
    raw = d.get("timestamp") or ""
    try:
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp() * 1000)
    except ValueError:
        return 0


def inspect(path):
    """(state, text, turns, last): the last assistant message after since_ms decides."""
    last = None
    turns = 0
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return "missing", "", 0, ""
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue  # a line still being written
        m = d.get("message") or {}
        if d.get("type") != "message" or m.get("role") != "assistant" or stamp(d, m) < since_ms:
            continue
        turns += 1
        last = m
    if last is None:
        return "pending", "", 0, ""
    content = last.get("content")
    parts = content if isinstance(content, list) else [{"type": "text", "text": str(content or "")}]
    text = "\n".join((p.get("text") or "") for p in parts if p.get("type") == "text").strip()
    calls = [p.get("name") or "" for p in parts if p.get("type") == "toolCall"]
    if last.get("stopReason") == "error":
        return "error", text or "[assistant turn failed]", turns, "error"
    if calls:
        return "pending", text, turns, calls[-1]   # yield or any tool call: the run is not finished
    if text:
        return "final", text, turns, "text"
    return "pending", "", turns, "empty"


state, text, turns, last = "missing", "", 0, ""
while True:
    path = transcript_path()
    if path:
        state, text, turns, last = inspect(path)
        if state in ("final", "error"):
            break
    if time.monotonic() >= deadline:
        if state == "pending":
            state = "timeout"
        break
    time.sleep(poll_s)
print(json.dumps({"state": state, "text": text, "turns": turns, "last": last,
                  "waited_ms": int((time.monotonic() - started) * 1000)}, ensure_ascii=False))
'''


def session_key(agent_id: str, sid: str) -> str:
    """OpenClaw session key the broker pins its turns to (gateway header / ``--session-id``)."""
    return f"agent:{agent_id}:broker-{sid}"


def parse_wait_output(stdout: str) -> dict:
    """The script's JSON line is the last non-empty line (openshell may print status lines before it)."""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict) and "state" in data:
                return data
    return {"state": "error", "text": "", "error": "no JSON from wait script"}


@dataclass
class SessionWaiter:
    """Host side: run ``WAIT_SCRIPT`` in the agent's sandbox through ``openshell sandbox exec``."""

    runner: Runner
    agents_root: str = AGENTS_ROOT
    poll_seconds: float = 1.0
    openshell_bin: str = "openshell"

    def argv(self, sandbox: str, agent_id: str, key: str, since_ms: int, timeout_s: float) -> list[str]:
        return [self.openshell_bin, "sandbox", "exec", "--name", sandbox, "--no-tty",
                "--timeout", str(int(timeout_s) + 15), "--", "python3", "-",
                self.agents_root, agent_id, key, str(int(since_ms)), str(int(timeout_s)), str(self.poll_seconds)]

    def wait_final(self, sandbox: str, agent_id: str, key: str, since_ms: int, timeout_s: float) -> dict:
        result = self.runner.run(self.argv(sandbox, agent_id, key, since_ms, timeout_s),
                                 timeout=timeout_s + 30, input_text=WAIT_SCRIPT)
        if not result.ok and not (result.stdout or "").strip():
            tail = " ".join((result.stderr or result.stdout or "").split())[-240:]
            return {"state": "error", "text": "", "error": f"wait exec rc={result.returncode}: {tail}"}
        return parse_wait_output(result.stdout)


__all__ = ["SessionWaiter", "WAIT_SCRIPT", "NO_RESPONSE", "session_key", "parse_wait_output"]
