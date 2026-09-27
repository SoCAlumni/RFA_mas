"""Run the host services in one process: egress-proxy (HTTP, all interfaces, bearer),
broker (HTTPS for managed MCP + REST fallback, bearer), channel entry + audit UI (loopback)."""

from __future__ import annotations

import asyncio
import signal
from pathlib import Path

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.routing import Route

from rfa_mas.nemoclaw import bootstrap as bs
from rfa_mas.nemoclaw.broker import Broker
from rfa_mas.nemoclaw.censor import CensorPipeline, JudgeError, JudgeVerdict, SandboxAgentJudge, parse_judge_output
from rfa_mas.nemoclaw.config import LlmStage, Routing, cross_check, load_assignments, load_censors, load_routing
from rfa_mas.nemoclaw.entry import Entry
from rfa_mas.nemoclaw.markers import load_or_create_secret
from rfa_mas.nemoclaw.proxy import EgressProxy
from rfa_mas.nemoclaw.runner import SubprocessRunner


class DirectJudge:
    """LLM stage straight against the censor alias backend (local Ollama). Used when the stage
    declares ``runner: direct``: same local model, same egress-0 property, no OpenClaw prompt."""

    def __init__(self, routing: Routing, backend_keys: dict[str, str]):
        self.routing = routing
        self.backend_keys = backend_keys

    def classify(self, text: str, stage: LlmStage) -> JudgeVerdict:
        from rfa_mas.nemoclaw.censor import JUDGE_PROMPT

        alias = self.routing.aliases[stage.alias]
        backend = self.routing.backends[alias.backend]
        headers = {}
        if backend.auth == "bearer":
            headers["authorization"] = f"Bearer {self.backend_keys.get(alias.backend, '')}"
        try:
            response = httpx.post(
                f"{backend.url.rstrip('/')}/chat/completions",
                json={"model": alias.model, "temperature": 0, "max_tokens": 300,
                      "messages": [{"role": "user", "content": JUDGE_PROMPT.format(
                          categories=", ".join(stage.categories), text=text)}]},
                headers=headers, timeout=stage.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise JudgeError(f"direct judge transport: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise JudgeError(f"direct judge HTTP {response.status_code}")
        content = ((response.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        return parse_judge_output(content)


class CompositeJudge:
    def __init__(self, sandbox_judge, direct_judge):
        self.sandbox_judge, self.direct_judge = sandbox_judge, direct_judge

    def classify(self, text: str, stage: LlmStage) -> JudgeVerdict:
        judge = self.direct_judge if stage.runner == "direct" else self.sandbox_judge
        return judge.classify(text, stage)


def _split(bind: str) -> tuple[str, int]:
    host, _, port = bind.rpartition(":")
    return host, int(port)


def build(replay: bool = False):
    assignments, routing, censors = load_assignments(), load_routing(), load_censors()
    problems = cross_check(assignments, routing, censors)
    if problems:
        raise SystemExit("configuration problems: " + "; ".join(problems))
    secret = load_or_create_secret(bs.ROOT / routing.proxy.marker_key_file)
    host_secrets = bs.ensure_host_secrets(routing)
    backend_keys: dict[str, str] = {}
    for name, backend in routing.backends.items():
        if backend.auth == "bearer" and backend.env_file and backend.credential_env:
            path = bs.ROOT / backend.env_file
            try:
                bs.check_env_file(path, backend.credential_env)
                backend_keys[name] = bs.read_env_value(path, backend.credential_env)
            except Exception as exc:  # the alias will answer 502 until fixed; never crash the proxy
                bs.log(f"backend {name}: credential unavailable ({exc}); alias calls will fail closed")
    runner = SubprocessRunner(cwd=bs.ROOT)
    judge = CompositeJudge(SandboxAgentJudge(runner, assignments.host.nemoclaw_bin), DirectJudge(routing, backend_keys))
    pipeline = CensorPipeline(censors, judge)
    proxy = EgressProxy(routing, pipeline, secret, host_secrets.values[routing.proxy.credential_env], backend_keys,
                        replay=replay)
    broker = Broker(assignments, routing, secret, host_secrets.values[routing.broker.credential_env], runner,
                    replay=replay)
    entry = Entry(assignments, routing, pipeline, broker, secret, runner, replay=replay)
    broker_app = Starlette(routes=[
        Route("/healthz", proxy.healthz, methods=["GET"]),
        Route(routing.broker.path, broker.mcp, methods=["GET", "POST", "DELETE"]),
        Route("/broker/agents", broker.rest_agents, methods=["GET"]),
        Route("/broker/ask", broker.rest_ask, methods=["POST"]),
    ])
    return assignments, routing, proxy, broker_app, entry.app()


async def _run_all(servers: list[uvicorn.Server]) -> None:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    tasks = [asyncio.create_task(s.serve()) for s in servers]
    await stop.wait()
    for s in servers:
        s.should_exit = True
    await asyncio.gather(*tasks, return_exceptions=True)


def serve(replay: bool = False) -> int:
    assignments, routing, proxy, broker_app, entry_app = build(replay=replay)
    proxy_host, proxy_port = _split(routing.proxy.bind)
    broker_host, broker_port = _split(routing.broker.bind)
    entry_host, entry_port = _split(routing.entry.bind)
    tls_dir = bs.ROOT / routing.broker.tls_dir
    cert, key = tls_dir / "server.pem", tls_dir / "server.key"
    tls = cert.exists() and key.exists()
    if not tls:
        bs.log("broker: TLS material missing; serving REST fallback over plain HTTP (managed MCP needs HTTPS)")
    common = {"access_log": False, "log_level": "warning", "proxy_headers": False, "loop": "asyncio"}
    servers = [
        uvicorn.Server(uvicorn.Config(proxy.app, host=proxy_host, port=proxy_port, **common)),
        uvicorn.Server(uvicorn.Config(broker_app, host=broker_host, port=broker_port,
                                      ssl_certfile=str(cert) if tls else None, ssl_keyfile=str(key) if tls else None,
                                      **common)),
        uvicorn.Server(uvicorn.Config(entry_app, host=entry_host, port=entry_port, **common)),
    ]
    for s in servers:
        s.install_signal_handlers = lambda: None  # one shared handler in _run_all
    bs.log(f"egress-proxy http://{routing.proxy.bind}/v1 (route {routing.proxy.route_url}, mode {routing.proxy.default_mode})")
    bs.log(f"broker {'https' if tls else 'http'}://{routing.broker.bind}{routing.broker.path} (+ REST /broker/*)")
    bs.log(f"entry/audit http://{routing.entry.bind}/audit/  replay={replay}")
    asyncio.run(_run_all(servers))
    return 0


__all__ = ["serve", "build", "DirectJudge", "Path"]
