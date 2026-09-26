"""Controlled-mode acceptance harness for RFA_E2E_Test_Scenarios_10_ko.md (P0-026).

Controlled mode runs the real FastAPI app, application services, LangGraph Supervisor,
SQLite repository and local lexical retrieval in-process. The model, review and team
runtime use the repository's deterministic mock/local adapters. Nothing here calls NVIDIA,
NeMo Retriever, teammate services, OpenShell or any network, so a controlled pass is never
a real-model or real-integration result. Every attempt gets its own temporary database,
checkpoint file and trace directory.

Identity: the product authenticates exactly one installation owner (keyless loopback). The
harness binds that owner record, in the temporary DB only, to the fixture "owner" persona so
the owner's calls go through the real auth dependency. The other three personas have no
product provisioning path yet, so each gets its own app instance whose auth dependency
returns that fixed fixture principal. Identity never comes from request text.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import platform
import re
import socket
import sqlite3
import statistics
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

import fixture_pack as pack
import httpx
from fastapi import FastAPI

from rfa_mas.api.app import create_app, resolve_principal
from rfa_mas.bootstrap import Container, build_container
from rfa_mas.contracts import SCHEMA_VERSION, TrustedPrincipal
from rfa_mas.settings import Settings

MODE = "controlled"
REPO_ROOT = pack.PACK.parents[1]
MANIFEST = pack.read_json("manifest.json")
CONFIG = pack.read_json("acceptance_config.json")
CLOCK = datetime.fromisoformat(MANIFEST["clock"])
DOMAIN = MANIFEST["domain_id"]
TARGETS = CONFIG["latency_targets_seconds"]
REPEATS = CONFIG["sampling"]["controlled_core_repeats"]
PRIVATE_CANARY = re.compile(r"SYNTHETIC_PRIVATE_CANARY_[A-Z0-9_]+")
# Tokens named in the scenario document; the fixtures embed them in longer markers.
DOC_CANARIES = ("CANARY_1ON1_Q7", "CANARY_GPU_R9")
NOTE_ROUTE = "/v1/knowledge/sources"
IMPORT_ROUTE = "/v1/knowledge/imports"

# Explicit controlled composition. Init values win over process environment variables.
CONTROLLED_SETTINGS: dict[str, Any] = {
    "app_api_key": None,
    "model_provider": "mock",
    "retriever_backend": "local",
    "response_backend": "mock",
    "tool_backend": "mock",
    "runtime_backend": "local",
    "policy_backend": "local",
    "trace_backend": "local",
    "enable_judge": False,
    "enable_nat": False,
    "scheduler_enabled": False,
    "enable_debate": False,
    "enable_auto_domain_creation": False,
    "allow_external_writes": False,
    "allow_external_egress": False,
}

# Owner task for a failing functional/security gate, and for the real-integration gate
# that controlled mode never runs. Pending features name their own tasks at call sites.
FUNCTIONAL_OWNERS = {
    "E2E-01": "P1-001",
    "E2E-02": "P1-001A",
    "E2E-03": "P0-020",
    "E2E-04": "P0-024",
    "E2E-05": "P1-007B",
    "E2E-06": "P1-005",
    "E2E-07": "P1-006E",
    "E2E-08": "P1-005A",
    "E2E-09": "P1-001A",
    "E2E-10": "P0-021",
}
REAL_INTEGRATION_TASKS = {
    "E2E-01": ("P1-008A", "teammate/connector services are not connected in controlled mode"),
    "E2E-02": ("P1-002A", "mock model and local lexical search only; no NVIDIA model/Retriever"),
    "E2E-03": ("P1-007B", "local role runtime only; no OpenShell sandbox or real runner"),
    "E2E-04": (
        "P0-023",
        "manual-clock ticks; the dedicated scheduler process was not run on wall-clock time",
    ),
    "E2E-05": (
        "P1-007B",
        "controlled mode is not applicable; the real OpenShell gate is separate evidence",
    ),
    "E2E-06": ("P1-008B", "fixture authentication; no runtime identity/membership service"),
    "E2E-07": ("P1-002A", "port test doubles and the mock model; no real model/tool boundary"),
    "E2E-08": (
        "P1-008A",
        "mock publisher or the P1-008C local stand-in; no teammate Response "
        "service or real channel",
    ),
    "E2E-09": ("P1-002A", "mock model answers; no real model/embedding refresh"),
    "E2E-10": (
        "P1-008B",
        "local runtime and mock adapters; no real runtime or model under restart/load",
    ),
}
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


# -- network guard ---------------------------------------------------------------------


class NetworkBlocked(RuntimeError):
    """Controlled-mode code tried to open an IP connection or resolve a host."""


class NetworkGuard:
    """Blocks IP sockets and DNS for the test; AF_UNIX (event loop self-pipe) is allowed.

    allow_loopback() lets a test reach its own local server subprocess on 127.0.0.1/::1;
    those connections are counted separately and anything else is still blocked.
    allow_host() is for the opt-in real-model gate only: DNS for exactly that host name and
    connections only to the addresses it resolved to; every other destination stays blocked.
    """

    def __init__(self) -> None:
        self.attempts: list[str] = []
        self.loopback = False
        self.loopback_connections = 0
        self.allowed_hosts: set[str] = set()
        self.allowed_addresses: set[str] = set()
        self.external_connections = 0

    def allow_loopback(self) -> None:
        self.loopback = True

    def allow_host(self, host: str) -> None:
        self.allowed_hosts.add(host.lower())

    @staticmethod
    def _name(host: Any) -> str | None:
        """Host as text: anyio passes getaddrinfo an ASCII/IDNA-encoded bytes name."""
        if isinstance(host, bytes):
            try:
                return host.decode("ascii").lower()
            except UnicodeDecodeError:
                return None
        return host.lower() if isinstance(host, str) else None

    def _is_loopback(self, host: Any) -> bool:
        return self.loopback and self._name(host) in LOOPBACK_HOSTS

    def _permitted(self, host: Any) -> bool:
        if self._is_loopback(host):
            self.loopback_connections += 1
            return True
        if isinstance(host, str) and host in self.allowed_addresses:
            self.external_connections += 1
            return True
        return False

    def install(self, monkeypatch) -> None:
        guard = self
        connect, connect_ex = socket.socket.connect, socket.socket.connect_ex
        getaddrinfo = socket.getaddrinfo

        def blocked(kind: str):
            guard.attempts.append(kind)
            raise NetworkBlocked(f"controlled E2E blocks network access ({kind})")

        def guarded_connect(sock, address, *args):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                if guard._permitted(address[0]):
                    return connect(sock, address, *args)
                blocked("connect")
            return connect(sock, address, *args)

        def guarded_connect_ex(sock, address, *args):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                if guard._permitted(address[0]):
                    return connect_ex(sock, address, *args)
                blocked("connect_ex")
            return connect_ex(sock, address, *args)

        def guarded_getaddrinfo(host, *args, **kwargs):
            if guard._is_loopback(host):
                return getaddrinfo(host, *args, **kwargs)
            if guard._name(host) in guard.allowed_hosts:
                resolved = getaddrinfo(host, *args, **kwargs)
                guard.allowed_addresses.update(str(entry[4][0]) for entry in resolved)
                return resolved
            return blocked("getaddrinfo")

        monkeypatch.setattr(socket.socket, "connect", guarded_connect)
        monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
        monkeypatch.setattr(socket, "create_connection", lambda *a, **k: blocked("create"))
        monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)


# -- isolated product stack ----------------------------------------------------------


def controlled_settings(root: Path, **overrides: Any) -> Settings:
    root = root.resolve()
    values = {
        "_env_file": None,
        "database_url": f"sqlite:///{root / 'rfa.db'}",
        "trace_dir": root / "traces",
        **CONTROLLED_SETTINGS,
        **overrides,
    }
    return Settings(**values)


async def bind_installation_owner(container: Container, principal: TrustedPrincipal) -> None:
    """Test-only identity setup in the temporary DB; see module docstring."""

    def update() -> int:
        with contextlib.closing(sqlite3.connect(container.repository.path)) as db, db:
            return db.execute(
                "UPDATE local_identity SET principal_json=? WHERE singleton=1",
                (principal.model_dump_json(),),
            ).rowcount

    if await asyncio.to_thread(update) != 1:
        raise RuntimeError("installation identity row is missing")
    if await container.repository.local_principal() != principal:
        raise RuntimeError("installation identity binding did not persist")


@dataclass(frozen=True)
class Ingested:
    fixture_id: str
    channel: str
    status: str
    source_id: str | None
    source_revision: str | None
    error_code: str | None
    elapsed_ms: float


@dataclass
class Stack:
    root: Path
    settings: Settings
    container: Container
    people: dict[str, TrustedPrincipal]
    sources: dict[str, Ingested] = field(default_factory=dict)
    _apps: dict[str, FastAPI] = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.container.repository.path

    @property
    def trace_dir(self) -> Path:
        return self.settings.trace_dir

    def app(self, persona: str = "owner") -> FastAPI:
        if persona not in self._apps:
            app = create_app(container=self.container)
            if persona != "owner":
                principal = self.people[persona]

                async def fixed_identity() -> TrustedPrincipal:
                    return principal

                app.dependency_overrides[resolve_principal] = fixed_identity
            self._apps[persona] = app
        return self._apps[persona]

    @contextlib.asynccontextmanager
    async def http(self, persona: str = "owner") -> AsyncIterator[httpx.AsyncClient]:
        transport = httpx.ASGITransport(app=self.app(persona), client=("127.0.0.1", 1))
        async with httpx.AsyncClient(transport=transport, base_url="http://rfa.test") as client:
            yield client

    def source_id(self, fixture_id: str) -> str:
        receipt = self.sources[fixture_id]
        assert receipt.source_id is not None
        return receipt.source_id

    def fixture_of(self, source_id: str) -> str | None:
        for fid, receipt in self.sources.items():
            if receipt.source_id == source_id:
                return fid
        return None

    def rows(self, sql: str, *args: Any) -> list[tuple]:
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            return db.execute(sql, args).fetchall()

    def trace_text(self) -> str:
        if not self.trace_dir.exists():
            return ""
        return "".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in sorted(self.trace_dir.rglob("*"))
            if path.is_file()
        )


@contextlib.asynccontextmanager
async def open_stack(
    root: Path,
    *,
    bind_owner: bool = True,
    http_transport: httpx.AsyncBaseTransport | None = None,
    model_transport: httpx.AsyncBaseTransport | None = None,
    **overrides: Any,
):
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
    settings = controlled_settings(root, **overrides)
    container = build_container(
        settings, http_transport=http_transport, model_transport=model_transport
    )
    await container.startup()
    try:
        people = pack.personas()
        if bind_owner:
            await bind_installation_owner(container, people["owner"])
        yield Stack(root=root, settings=settings, container=container, people=people)
    finally:
        await container.shutdown()


async def ingest(stack: Stack, fixture_ids: Iterable[str], *, persona: str = "owner"):
    """Ingest fixtures through the HTTP API as the given persona (their owner)."""
    wanted = list(dict.fromkeys(fixture_ids))
    payloads = pack.payloads()
    results: dict[str, Ingested] = {}
    async with stack.http(persona) as client:
        for fid in wanted:
            channel, payload = payloads[fid]
            if channel != "note":
                continue
            start = perf_counter()
            response = await client.post(NOTE_ROUTE, json=payload)
            elapsed = (perf_counter() - start) * 1000
            if response.status_code == 201:
                document = response.json()["document"]
                results[fid] = Ingested(
                    fid,
                    channel,
                    "accepted",
                    document["source_id"],
                    document["source_revision"],
                    None,
                    elapsed,
                )
            else:
                results[fid] = Ingested(
                    fid, channel, "rejected", None, None, response.json().get("code"), elapsed
                )
        for ids, batch in pack.export_batches(ids=wanted):
            start = perf_counter()
            response = await client.post(IMPORT_ROUTE, json=batch.model_dump(mode="json"))
            elapsed = (perf_counter() - start) * 1000
            response.raise_for_status()
            for fid, row in zip(ids, response.json()["rows"], strict=True):
                results[fid] = Ingested(
                    fid,
                    batch.provider,
                    row["status"],
                    row.get("source_id"),
                    row.get("source_revision"),
                    row.get("error_code"),
                    elapsed / len(ids),
                )
    stack.sources.update({fid: r for fid, r in results.items() if r.status == "accepted"})
    return results


def work_body(query: str, *, audience: str = "owner", **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"query": query, "domain_id": DOMAIN, "target": {"audience": audience}}
    body.update(extra)
    return body


def team_body(
    query: str, goal: str, outputs: tuple[str, ...], pattern: str | None = None, **extra: Any
) -> dict[str, Any]:
    team: dict[str, Any] = {"goal": goal, "outputs": list(outputs)}
    if pattern is not None:
        team["requested_pattern"] = pattern
    return work_body(query, schema_version="1.1", team=team, **extra)


def evidence_fixtures(stack: Stack, result: dict[str, Any]) -> list[str]:
    """Fixture IDs (or 'seed:<source_id>') cited by a RunResult JSON draft."""
    draft = result.get("draft") or {}
    return [
        stack.fixture_of(ref["source_id"]) or f"seed:{ref['source_id']}"
        for ref in draft.get("allowed_evidence", [])
    ]


def leaked_canaries(text: str) -> list[str]:
    found = set(PRIVATE_CANARY.findall(text))
    found.update(token for token in DOC_CANARIES if token in text)
    return sorted(found)


def summarize(samples: list[float]) -> dict[str, Any]:
    result = {
        "n": len(samples),
        "median": round(statistics.median(samples), 3),
        "max": round(max(samples), 3),
        "min": round(min(samples), 3),
        "samples": [round(value, 3) for value in samples],
    }
    if len(samples) >= 20:
        result.update(percentiles(samples))
    return result


def percentiles(samples: list[float]) -> dict[str, float]:
    """Nearest-rank p50/p95 (reported only for n >= 20; small samples use median/max)."""
    ordered = sorted(samples)

    def rank(p: float) -> float:
        index = max(0, min(len(ordered) - 1, -(-len(ordered) * p // 100) - 1))
        return round(ordered[int(index)], 3)

    return {"p50": rank(50), "p95": rank(95)}


# -- attempt recorder ----------------------------------------------------------------


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(*args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(REPO_ROOT), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def run_context() -> dict[str, Any]:
    status = _git("status", "--porcelain")
    return {
        "mode": MODE,
        "versions": {
            "code_commit": _git("rev-parse", "HEAD"),
            "code_dirty": bool(status) if status is not None else None,
            "contract_schema": SCHEMA_VERSION,
            "fixture_dataset": MANIFEST["dataset_version"],
            "fixture_manifest_sha256": _sha256(pack.PACK / pack.MANIFEST),
            "acceptance_config": CONFIG["config_version"],
            "team_templates_sha256": _sha256(REPO_ROOT / "fixtures/teams/templates.json"),
            "model": "mock-model (deterministic; not a real model)",
            "prompt": None,
        },
        "environment": {
            "os": platform.system(),
            "os_release": platform.release(),
            "machine": platform.machine(),
            "cpu_count": os.cpu_count(),
            "python": platform.python_version(),
            "model_endpoint": None,
            "quantization": None,
            "context_window": None,
            "concurrency": "single asyncio loop, in-process ASGI",
        },
        "scenario_clock": MANIFEST["clock"],
        "clock_note": "fixture timestamps use this clock; schedule time travel uses the "
        "P0-023 ManualClock scheduler adapter, other product paths use the wall clock",
        "seed": MANIFEST["seed"],
    }


class CheckFailed(AssertionError):
    """A named functional/security check failed. The name is the only recorded detail."""


@dataclass
class Attempt:
    scenario: str
    variant: str
    repeat: int | None
    attempt_id: str
    started_at: str
    checks: list[dict[str, Any]] = field(default_factory=list)
    measurements: dict[str, dict[str, Any]] = field(default_factory=dict)
    quality: dict[str, dict[str, Any]] = field(default_factory=dict)
    observations: dict[str, Any] = field(default_factory=dict)
    not_run: list[dict[str, str]] = field(default_factory=list)
    gate_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    adapters: list[dict[str, Any]] = field(default_factory=list)
    policy_version: str | None = None
    real_gate: tuple[str, str] | None = None
    real_run: dict[str, Any] | None = None
    context_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)

    def check(self, check_id: str, condition: bool) -> None:
        self.checks.append({"id": check_id, "result": "passed" if condition else "failed"})
        if not condition:
            raise CheckFailed(check_id)

    def measure(
        self,
        name: str,
        samples_ms: list[float],
        *,
        target_seconds: float | None = None,
        scope: str = "controlled-local",
        note: str | None = None,
        statistic: str = "max",
    ) -> None:
        entry: dict[str, Any] = {"unit": "ms", "scope": scope, **summarize(samples_ms)}
        if target_seconds is not None:
            entry["target_seconds"] = target_seconds
            entry["rule"] = f"{statistic} <= target"
            entry["status"] = "passed" if entry[statistic] <= target_seconds * 1000 else "failed"
        if note:
            entry["note"] = note
        self.measurements[name] = entry

    def score(
        self,
        name: str,
        value: float | None,
        *,
        target: float | None = None,
        detail: dict[str, Any] | None = None,
        owner: str | None = None,
    ) -> None:
        entry: dict[str, Any] = {"value": value}
        if target is not None and value is not None:
            entry.update(target=target, status="passed" if value >= target else "failed")
        else:
            entry["status"] = "report_only"
        if detail:
            entry["detail"] = detail
        if owner:
            entry["owner"] = owner
        self.quality[name] = entry

    def observe(self, name: str, value: Any) -> None:
        self.observations[name] = value

    def pending(self, item: str, reason: str, next_task: str) -> None:
        """A sub-requirement this attempt could not exercise (feature missing)."""
        self.not_run.append({"item": item, "reason": reason, "next_task": next_task})

    def real_integration(self, next_task: str, reason: str) -> None:
        """Override the scenario default; the real-integration gate stays not_run."""
        self.real_gate = (next_task, reason)

    def real_executed(
        self, *, passed: bool, reason: str, next_task: str, evidence: dict[str, Any]
    ) -> None:
        """The opt-in real gate actually ran; its own verdict, never inferred from controlled."""
        self.real_run = {
            "status": "passed" if passed else "failed",
            "reason": reason,
            "next_task": None if passed else next_task,
            "evidence": evidence,
        }

    def set_context(self, **sections: Any) -> None:
        """Per-attempt mode/versions/environment (e.g. the real model id) over the session."""
        for name, values in sections.items():
            if isinstance(values, dict):
                self.context_overrides.setdefault(name, {}).update(values)
            else:
                self.context_overrides[name] = values

    def bind(self, stack: Stack) -> None:
        self.adapters = [item.model_dump(mode="json") for item in stack.container.adapters]
        self.policy_version = stack.container.policy.policy_version


class Recorder:
    """Writes one JSON report per attempt with separately judged gates.

    status mirrors the functional/security gate only. quality, performance and
    real_integration keep their own status; complete_e2e stays false unless every gate
    passed, which never happens in controlled mode because real_integration is not_run.
    """

    GATES = ("functional_security", "quality", "performance", "real_integration")

    def __init__(self, directory: Path) -> None:
        # One subfolder per pytest session so reruns into the same base never mix.
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.run_id = f"{stamp}-{uuid.uuid4().hex[:6]}"
        self.directory = directory / self.run_id
        self.directory.mkdir(parents=True, exist_ok=True)
        self.context = run_context()
        self.reports: list[dict[str, Any]] = []

    @contextlib.contextmanager
    def attempt(self, scenario: str, variant: str, *, repeat: int | None = None):
        record = Attempt(
            scenario, variant, repeat, uuid.uuid4().hex[:12], datetime.now(UTC).isoformat()
        )
        error: BaseException | None = None
        try:
            yield record
        except BaseException as exc:
            error = exc
            raise
        finally:
            self._write(self._finish(record, error))

    def not_run(
        self,
        scenario: str,
        variant: str,
        *,
        reason: str,
        next_task: str,
        related: tuple[str, ...] = (),
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        gate = {"status": "not_run", "reason": reason, "next_task": next_task}
        report = self._base(
            scenario, variant, None, uuid.uuid4().hex[:12], datetime.now(UTC).isoformat()
        )
        report.update(
            status="not_run",
            reason=reason,
            next_task=next_task,
            related_tasks=list(related),
            gates={name: dict(gate) for name in self.GATES},
            not_run_items=[],
            external_evidence=evidence,
            complete_e2e=False,
        )
        self._write(report)
        return report

    def _base(self, scenario, variant, repeat, attempt_id, started_at) -> dict[str, Any]:
        return {
            "schema": "rfa-e2e-attempt/1",
            "session_run_id": self.run_id,
            "scenario_id": scenario,
            "variant_id": variant,
            "repeat": repeat,
            "attempt_id": attempt_id,
            "started_at": started_at,
            "finished_at": datetime.now(UTC).isoformat(),
            **self.context,
        }

    def _finish(self, record: Attempt, error: BaseException | None) -> dict[str, Any]:
        owner = FUNCTIONAL_OWNERS.get(record.scenario)
        failed = [c["id"] for c in record.checks if c["result"] == "failed"]
        if error is not None:
            reason = (
                f"check failed: {error}"
                if isinstance(error, CheckFailed)
                else f"unexpected {type(error).__name__} before completion"
            )
            functional = {"status": "failed", "reason": reason, "next_task": owner}
        elif failed:
            functional = {
                "status": "failed",
                "reason": f"checks failed: {failed}",
                "next_task": owner,
            }
        elif record.checks:
            functional = {"status": "passed", "reason": None, "next_task": None}
        else:
            functional = {
                "status": "not_run",
                "reason": "no functional check executed",
                "next_task": owner,
            }
        functional["checks"] = record.checks

        scored = [q for q in record.quality.values() if q["status"] in {"passed", "failed"}]
        if scored:
            quality_status = "failed" if any(q["status"] == "failed" for q in scored) else "passed"
            failed_owners = [q.get("owner", owner) for q in scored if q["status"] == "failed"]
            below = sorted(n for n, q in record.quality.items() if q["status"] == "failed")
            quality = {
                "status": quality_status,
                "reason": f"below target: {below}" if below else None,
                "next_task": failed_owners[0] if failed_owners else None,
            }
        else:
            quality = {
                "status": "not_run",
                "reason": "no scored quality metric in this attempt",
                "next_task": None,
            }
        quality["metrics"] = record.quality

        targeted = [m for m in record.measurements.values() if "status" in m]
        if targeted:
            perf_status = "failed" if any(m["status"] == "failed" for m in targeted) else "passed"
            performance = {
                "status": perf_status,
                "reason": "controlled-local measurement against pinned target",
                "next_task": owner if perf_status == "failed" else None,
            }
        else:
            performance = {
                "status": "not_run",
                "reason": "no pinned latency target applies to these measurements",
                "next_task": None,
            }
        performance["measurements"] = record.measurements

        if record.real_run is not None:
            real = dict(record.real_run)
            if functional["status"] != "passed" and real["status"] == "passed":
                real.update(status="failed", reason="functional/security gate did not pass")
        else:
            task, why = record.real_gate or REAL_INTEGRATION_TASKS.get(
                record.scenario, ("P1-009", "controlled mode")
            )
            real = {"status": "not_run", "reason": f"controlled mode: {why}", "next_task": task}
        gates = {
            "functional_security": functional,
            "quality": quality,
            "performance": performance,
            "real_integration": real,
        }
        report = self._base(
            record.scenario, record.variant, record.repeat, record.attempt_id, record.started_at
        )
        for name, values in record.context_overrides.items():
            report[name] = (
                {**report.get(name, {}), **values} if isinstance(values, dict) else values
            )
        report.update(
            status=functional["status"],
            reason=functional["reason"],
            next_task=functional["next_task"],
            gates=gates,
            not_run_items=record.not_run,
            observations=record.observations,
            adapters=record.adapters,
            policy_version=record.policy_version,
            complete_e2e=all(g["status"] == "passed" for g in gates.values())
            and not record.not_run,
        )
        return report

    def _write(self, report: dict[str, Any]) -> None:
        folder = self.directory / report["scenario_id"] / report["variant_id"]
        folder.mkdir(parents=True, exist_ok=True)
        name = f"attempt-{report['repeat'] or 1}-{report['attempt_id']}.json"
        (folder / name).write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        self.reports.append(report)

    def summary(self) -> dict[str, Any]:
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for report in self.reports:
            grouped.setdefault((report["scenario_id"], report["variant_id"]), []).append(report)
        rows = []
        for (scenario, variant), reports in sorted(grouped.items()):
            statuses = {r["status"] for r in reports}
            samples: dict[str, list[float]] = {}
            for report in reports:
                for name, entry in report["gates"]["performance"].get("measurements", {}).items():
                    samples.setdefault(name, []).extend(entry["samples"])
            tasks = {r.get("next_task") for r in reports}
            tasks.update(item["next_task"] for r in reports for item in r["not_run_items"])
            tasks.update(r["gates"][g].get("next_task") for r in reports for g in self.GATES)
            rows.append(
                {
                    "scenario_id": scenario,
                    "variant_id": variant,
                    "attempts": len(reports),
                    "status": (
                        "failed"
                        if "failed" in statuses
                        else "not_run"
                        if statuses == {"not_run"}
                        else "passed"
                    ),
                    "gates": {
                        g: sorted({r["gates"][g]["status"] for r in reports}) for g in self.GATES
                    },
                    "not_run_items": sorted(
                        {i["item"] for r in reports for i in r["not_run_items"]}
                    ),
                    "next_tasks": sorted(t for t in tasks if t),
                    "measurements_all_attempts": {k: summarize(v) for k, v in samples.items()},
                }
            )
        return {"schema": "rfa-e2e-summary/1", **self.context, "rows": rows}

    def write_summary(self) -> Path:
        path = self.directory / "summary.json"
        path.write_text(
            json.dumps(self.summary(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return path


# -- real app subprocess (E2E-10) ----------------------------------------------------------


def free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class LocalServer:
    """The real `rfa api` entrypoint (uvicorn) as a child process on a free loopback port.

    Settings come only from a synthetic env file written into the temp data dir; the child
    gets a minimal environment (PATH, HOME=data dir), so no user .env or shell variable is
    read. The parent reaches it over 127.0.0.1 only.
    """

    def __init__(self, root: Path, **overrides: str) -> None:
        self.root = root.resolve()
        self.port = free_loopback_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.process: subprocess.Popen | None = None
        values = {
            "APP_ENV": "development",
            "APP_HOST": "127.0.0.1",
            "APP_PORT": str(self.port),
            "DATABASE_URL": f"sqlite:///{self.root / 'rfa.db'}",
            "TRACE_DIR": str(self.root / "traces"),
            "MODEL_PROVIDER": "mock",
            "RETRIEVER_BACKEND": "local",
            "RESPONSE_BACKEND": "mock",
            "TOOL_BACKEND": "mock",
            "RUNTIME_BACKEND": "local",
            "POLICY_BACKEND": "local",
            "TRACE_BACKEND": "local",
            "SCHEDULER_ENABLED": "false",
            "ALLOW_EXTERNAL_WRITES": "false",
            "ALLOW_EXTERNAL_EGRESS": "false",
            "LOG_LEVEL": "WARNING",
            **overrides,
        }
        self.env_file = self.root / "e2e-server.env"
        self.env_file.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
        self.log_path = self.root / "server.log"

    def start(self) -> None:
        self.port = free_loopback_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        text = self.env_file.read_text(encoding="utf-8")
        text = re.sub(r"APP_PORT=\d+", f"APP_PORT={self.port}", text)
        self.env_file.write_text(text, encoding="utf-8")
        log = self.log_path.open("ab")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "rfa_mas", "--env-file", str(self.env_file), "api"],
            cwd=self.root,
            env={"PATH": os.defpath, "HOME": str(self.root), "PYTHONDONTWRITEBYTECODE": "1"},
            stdout=subprocess.DEVNULL,
            stderr=log,
        )
        log.close()

    async def wait_ready(self, limit_seconds: float = 30.0) -> float:
        """Milliseconds from now until /readyz answers 200."""
        start = perf_counter()
        async with httpx.AsyncClient(base_url=self.base_url, timeout=2.0) as client:
            while perf_counter() - start < limit_seconds:
                if self.process is None or self.process.poll() is not None:
                    raise RuntimeError("server process exited before readiness")
                try:
                    if (await client.get("/readyz")).status_code == 200:
                        return (perf_counter() - start) * 1000
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.05)
        raise TimeoutError("server did not become ready")

    def kill(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=10)

    def terminate(self) -> int | None:
        if self.process is None:
            return None
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        return self.process.returncode

    def resources(self) -> dict[str, float] | None:
        """RSS (MB) and CPU (%) of the child as reported by ps; None if unavailable."""
        if self.process is None or self.process.poll() is not None:
            return None
        try:
            out = subprocess.run(
                ["ps", "-o", "rss=,%cpu=", "-p", str(self.process.pid)],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            ).stdout
            rss_kb, cpu = out.split()
            return {"rss_mb": round(int(rss_kb) / 1024, 1), "cpu_pct": float(cpu)}
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

    def log_tail(self, chars: int = 2000) -> str:
        if not self.log_path.exists():
            return ""
        return self.log_path.read_text(encoding="utf-8", errors="replace")[-chars:]


# -- a second SQLite process beside the API (E2E-10, folded-in P0-005A diagnostic) -------------

_READER_SCRIPT = """
import json, os, sqlite3, sys, time
database, stop = sys.argv[1], sys.argv[2]
reads, errors = 0, {}
while not os.path.exists(stop):
    try:
        connection = sqlite3.connect(database, timeout=0.5)
        try:
            connection.execute("SELECT count(*) FROM runs").fetchone()
            reads += 1
        finally:
            connection.close()
    except sqlite3.Error as exc:
        key = type(exc).__name__ + ": " + str(exc)
        errors[key] = errors.get(key, 0) + 1
    time.sleep(0.005)
print(json.dumps({"reads": reads, "errors": errors}))
"""


class SecondSqliteReader:
    """A separate process that keeps opening short connections to the live database.

    This is what the separate scheduler process (or any local reader) does. Before P0-005A
    it made the API process hit `disk I/O error`/SIGBUS: the API released SQLite's -shm
    lock by opening and closing the DB file for its private-file checks. The reader runs
    until stop() and reports its own read and error counts; it never writes.
    """

    def __init__(self, database: Path) -> None:
        self.database = database.resolve()
        self.stop_file = self.database.parent / f"reader-stop-{uuid.uuid4().hex[:8]}"
        self.process: subprocess.Popen | None = None
        self.result: dict[str, Any] | None = None

    def start(self) -> None:
        if not self.database.is_file():
            raise RuntimeError("the reader never creates the database")
        self.process = subprocess.Popen(
            [sys.executable, "-c", _READER_SCRIPT, str(self.database), str(self.stop_file)],
            env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def stop(self) -> dict[str, Any]:
        """Idempotent: the first call stops the process and keeps its report."""
        if self.result is not None:
            return self.result
        if self.process is None:
            return {"reads": 0, "errors": {}, "exit_code": None}
        self.stop_file.touch()
        try:
            out, _ = self.process.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            self.process.kill()
            out, _ = self.process.communicate(timeout=10)
        self.stop_file.unlink(missing_ok=True)
        try:
            result = json.loads(out.strip().splitlines()[-1])
        except (IndexError, ValueError):
            result = {"reads": 0, "errors": {"unparsed_reader_output": 1}}
        result["exit_code"] = self.process.returncode
        self.result = result
        return result


def sqlite_fault_lines(log: str) -> list[str]:
    """Server log lines that show the multi-process SQLite fault (never expected)."""
    markers = ("disk I/O error", "database disk image is malformed", "Bus error", "SIGBUS")
    return [line.strip()[:200] for line in log.splitlines() if any(m in line for m in markers)]
