"""Shared demo harness: 30-second budget, live/replay modes, recorded logs, automatic fallback.

Live runs record every step to ``demo/replay/<nn>.jsonl`` (only what actually happened).
``--replay`` prints that recording instead of touching sandboxes. A live run that fails or
exceeds its budget falls back to the recording automatically and says so.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# RFA_DEMO_REPLAY_DIR: keep recordings made against a fake/alternate entry out of demo/replay (real recordings only)
REPLAY_DIR = Path(os.environ.get("RFA_DEMO_REPLAY_DIR") or ROOT / "demo" / "replay")
sys.path.insert(0, str(ROOT / "src"))

from rfa_mas.nemoclaw.runner import SubprocessRunner  # noqa: E402

BUDGET_SECONDS = 30.0


class DemoFailure(Exception):
    pass


@dataclass
class Step:
    name: str
    ok: bool
    ms: int
    detail: str
    argv: list[str] = field(default_factory=list)


@dataclass
class Demo:
    number: str
    title: str
    budget: float = BUDGET_SECONDS
    steps: list[Step] = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)
    runner: SubprocessRunner = field(default_factory=lambda: SubprocessRunner(cwd=ROOT))
    mode: str = "live"

    @property
    def replay_path(self) -> Path:
        return REPLAY_DIR / f"{self.number}.jsonl"

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def say(self, text: str) -> None:
        print(f"[{self.elapsed():5.1f}s] {text}", flush=True)

    def step(self, name: str, ok: bool, detail: str, argv: list[str] | None = None, ms: int | None = None) -> None:
        step = Step(name, ok, ms if ms is not None else 0, detail[:600], argv or [])
        self.steps.append(step)
        self.say(f"{'✓' if ok else '✗'} {name}: {detail[:300]}")
        if not ok:
            raise DemoFailure(name)

    def run(self, name: str, argv: list[str], *, expect_ok: bool = True, timeout: float = 60, env=None,
            excerpt=lambda r: (r.stdout or r.stderr).strip()[-300:]) -> str:
        started = time.monotonic()
        result = self.runner.run(argv, timeout=timeout, env=env)
        ms = int((time.monotonic() - started) * 1000)
        ok = result.ok if expect_ok else not result.ok
        self.step(name, ok, f"rc={result.returncode} {excerpt(result)}", argv, ms)
        return result.stdout

    def http(self, name: str, method: str, url: str, *, json_body=None, headers=None, timeout: float = 60,
             expect_status: int | tuple[int, ...] = 200, summarize=lambda data: str(data)[:300]) -> dict:
        import httpx

        started = time.monotonic()
        try:
            response = httpx.request(method, url, json=json_body, headers=headers, timeout=timeout)
        except httpx.HTTPError as exc:
            self.step(name, False, f"{type(exc).__name__}: {exc}", [method, url], int((time.monotonic() - started) * 1000))
            return {}
        ms = int((time.monotonic() - started) * 1000)
        statuses = expect_status if isinstance(expect_status, tuple) else (expect_status,)
        try:
            data = response.json()
        except ValueError:
            data = {"text": response.text[:300]}
        self.step(name, response.status_code in statuses, f"HTTP {response.status_code} {summarize(data)}", [method, url], ms)
        return data

    def check(self, name: str, condition: bool, detail: str) -> None:
        self.step(name, bool(condition), detail)

    # ---- recording / replay ---------------------------------------------------------------

    def record(self, final_ok: bool) -> None:
        REPLAY_DIR.mkdir(parents=True, exist_ok=True)
        with self.replay_path.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps({"demo": self.number, "title": self.title, "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                 "mode": "live", "ok": final_ok, "elapsed_s": round(self.elapsed(), 1)}, ensure_ascii=False) + "\n")
            for step in self.steps:
                fh.write(json.dumps({"step": step.name, "ok": step.ok, "ms": step.ms, "detail": step.detail,
                                     "argv": step.argv}, ensure_ascii=False) + "\n")

    def replay(self, reason: str | None = None) -> int:
        if not self.replay_path.exists():
            print(f"no recording at {self.replay_path.relative_to(ROOT)}; run once with --live first")
            return 2
        lines = [json.loads(line) for line in self.replay_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        header, steps = lines[0], lines[1:]
        if reason:
            print(f"!! live run {reason}; replaying recording from {header['recorded_at']} (elapsed then: {header['elapsed_s']}s)")
        print(f"== demo {self.number} (replay of {header['recorded_at']}): {self.title}")
        total = 0
        for step in steps:
            total += step["ms"]
            print(f"[{total/1000:5.1f}s] {'✓' if step['ok'] else '✗'} {step['step']}: {step['detail'][:300]}")
        verdict = "PASS" if header["ok"] else "FAIL"
        print(f"== {verdict} (recorded {header['elapsed_s']}s; budget {self.budget:.0f}s)")
        return 0 if header["ok"] else 1


def main(number: str, title: str, body, budget: float = BUDGET_SECONDS) -> int:
    parser = argparse.ArgumentParser(description=f"demo {number}: {title}")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--live", action="store_true", help="run against the live sandboxes (records a replay)")
    group.add_argument("--replay", action="store_true", help="print the recorded run (default when no --live)")
    parser.add_argument("--no-fallback", action="store_true", help="do not fall back to the recording on failure")
    args = parser.parse_args()
    demo = Demo(number, title, budget=budget)
    if not args.live:
        demo.mode = "replay"
        return demo.replay()
    print(f"== demo {number} (live): {title}")
    try:
        body(demo)
        ok = demo.elapsed() <= demo.budget
        demo.record(ok)
        print(f"== {'PASS' if ok else 'OVER BUDGET'} in {demo.elapsed():.1f}s (budget {demo.budget:.0f}s)")
        if ok:
            return 0
        reason = f"exceeded the {demo.budget:.0f}s budget ({demo.elapsed():.1f}s)"
    except DemoFailure as exc:
        demo.record(False)
        reason = f"failed at step '{exc}'"
    except KeyboardInterrupt:
        return 130
    if args.no_fallback or not demo.replay_path.exists():
        print(f"== FAIL: {reason}")
        return 1
    return demo.replay(reason)


def proxy_key() -> str:
    return (ROOT / ".local" / "sg" / "proxy-api.key").read_text(encoding="utf-8").strip()


def marker_secret() -> bytes:
    return (ROOT / ".local" / "sg" / "marker.key").read_bytes().strip()


def ask_token() -> str:
    """RFA_ASK_TOKEN from .env.dev (host-side; the value is never printed or recorded)."""
    for line in (ROOT / ".env.dev").read_text(encoding="utf-8").splitlines():
        if line.startswith("RFA_ASK_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise DemoFailure("RFA_ASK_TOKEN missing in .env.dev")


# RFA_ENTRY_URL: point the ask() demos at another entry (e.g. `serve --fake-agents` on a spare port) for a dry run
ENTRY = os.environ.get("RFA_ENTRY_URL", "http://127.0.0.1:8799")


def nemoclaw_bin() -> str:
    return os.environ.get("NEMOCLAW_BIN", "nemoclaw")
