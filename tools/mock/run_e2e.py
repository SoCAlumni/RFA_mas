#!/usr/bin/env python3
"""``make mock-e2e``: run the four desk scenarios end to end and print the result table.

Default (``--fake-agents``): start an in-process ``/ask`` server whose head/tasks/censor are fixed
stand-ins (no sandbox, no model) plus the auto-deciding approval mock, then run the desk. This
verifies the contract and the feedback loop only — it is not a live NemoClaw result.

``--ask-url http://127.0.0.1:8799`` runs the same desk against the running ``make serve`` entry
(real head/broker/sandboxes); the token comes from ``RFA_ASK_TOKEN`` in ``.env.dev`` unless given.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools" / "mock"))

from approval import DEFAULT_REGEX  # noqa: E402
from approval import create_app as create_approval_app
from desk import Desk, load_scenarios, table  # noqa: E402

SCENARIOS = sorted(str(p) for p in (ROOT / "tools" / "mock" / "scenarios").glob("*.yaml"))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Background:
    """uvicorn in a thread, stopped on exit."""

    def __init__(self, app, port: int):
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", loop="asyncio"))
        self.server.install_signal_handlers = lambda: None
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.url = f"http://127.0.0.1:{port}"

    def __enter__(self):
        self.thread.start()
        for _ in range(100):
            try:
                httpx.get(f"{self.url}/healthz", timeout=1)
                return self
            except httpx.HTTPError:
                time.sleep(0.05)
        raise RuntimeError(f"server on {self.url} did not start")

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=5)


def fake_ask_app(learned_path: Path, task_delay: float):
    from rfa_mas.nemoclaw.ask import build_fake_deps
    from rfa_mas.nemoclaw.ask_api import AskService, create_ask_app
    from rfa_mas.nemoclaw.config import load_ask, load_censors

    from rfa_mas.nemoclaw.config import load_assignments
    from rfa_mas.nemoclaw.services import build_frontend_services
    from rfa_mas.nemoclaw.store import Store

    deps = build_fake_deps(load_ask(), load_censors(), learned_path, task_delay=task_delay)
    token = "mock-" + os.urandom(12).hex()
    assignments = load_assignments()
    frontend = build_frontend_services(assignments=lambda: assignments, ask_deps=deps, token=token,
                                       store=Store(learned_path.parent / "frontend.db"))
    return create_ask_app(AskService(deps, token), frontend=frontend), token, deps


def read_env_token(env_file: Path, key: str) -> str | None:
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    except OSError:
        return None
    return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fake-agents", action="store_true", help="in-process /ask with fixed head/tasks/censor (default when no --ask-url)")
    parser.add_argument("--ask-url", default=None, help="use a running knowledge server instead of the fake one")
    parser.add_argument("--token-env", default="RFA_ASK_TOKEN")
    parser.add_argument("--env-file", default=str(ROOT / ".env.dev"))
    parser.add_argument("--approval-url", default=None, help="use a running approval mock instead of starting one")
    parser.add_argument("--auto-regex", default=DEFAULT_REGEX, help="auto reject-if-regex for the started approval mock")
    parser.add_argument("--learned-file", default=None, help="learned.yaml for the fake server (default: fresh temp file)")
    parser.add_argument("--task-delay", type=float, default=0.0, help="fake task agent latency (s)")
    parser.add_argument("--ask-timeout", type=float, default=300.0, help="client-side wait for the synchronous /ask")
    parser.add_argument("scenarios", nargs="*", default=SCENARIOS)
    args = parser.parse_args(argv)
    fake = args.fake_agents or not args.ask_url
    if fake and args.ask_url:
        print("error: --fake-agents and --ask-url are exclusive", file=sys.stderr)
        return 2

    os.environ.setdefault("RFA_SG_AUDIT_DB", str(Path(tempfile.mkdtemp(prefix="rfa-mock-")) / "audit.db")) if fake else None
    scenarios = load_scenarios(args.scenarios)
    stack: list[Background] = []
    try:
        if fake:
            learned = Path(args.learned_file) if args.learned_file else Path(tempfile.mkdtemp(prefix="rfa-mock-")) / "learned.yaml"
            app, token, deps = fake_ask_app(learned, args.task_delay)
            ask_server = Background(app, free_port()).__enter__()
            stack.append(ask_server)
            ask_url = ask_server.url
            print(f"[mock-e2e] fake /ask server {ask_url} (head=keywords, tasks=KB seed, censor=regex+hint judge) learned={learned}")
        else:
            ask_url = args.ask_url
            token = os.environ.get(args.token_env) or read_env_token(Path(args.env_file), args.token_env)
            if not token:
                print(f"error: {args.token_env} not found in env or {args.env_file}", file=sys.stderr)
                return 2
            print(f"[mock-e2e] live /ask server {ask_url} (token from {args.token_env})")
        if args.approval_url:
            approval_url = args.approval_url
        else:
            approval = Background(create_approval_app(args.auto_regex), free_port()).__enter__()
            stack.append(approval)
            approval_url = approval.url
            print(f"[mock-e2e] approval mock {approval_url} auto=reject-if-regex {args.auto_regex!r}")
        desk = Desk(ask_url, token, approval_url, ask_timeout=args.ask_timeout)
        tag = time.strftime("%H%M%S")
        outcomes = [desk.run(s, run_tag=tag) for s in scenarios]
    finally:
        for server in reversed(stack):
            server.__exit__(None, None, None)
    print()
    print(f"mock-e2e ({'fake agents' if fake else 'live ' + args.ask_url}) — {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(table(outcomes))
    failed = [o for o in outcomes if o.check()]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
