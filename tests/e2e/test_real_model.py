"""Opt-in real-model gate: the product ModelPort path to hosted NVIDIA (P1-002), public only.

Selected only with RFA_ENV_FILE=<absolute path of an explicit env file>. As in P1-002's own
product live smoke, only NVIDIA_BASE_URL, NVIDIA_MODEL and NVIDIA_API_KEY are read from that
file and the gate selects model_provider=nvidia; everything else is the controlled offline
composition (temp DB, local retrieval/policy/runtime/trace, mock review and publication).
No configuration value is printed or written: reports keep setting names, the model id that
was sent, the endpoint host, latencies, status codes and check results only.

Subset, public synthetic data only, public target, sequential calls:
  E2E-02 public question  G03 asked by the owner (R01 is public)
  E2E-06 public persona   the external persona's first E2E-06 question
  E2E-08 public draft     the external request routed by /v1/assistant (ingress public); the
                          draft is not published
The store also holds the synthetic private notes (R07/R08 and the seeded owner canaries) so
"zero private canaries in outbound requests" is a real check. The model transport refuses,
before anything is sent, any body carrying a private canary or an internal-only fixture fact
and any host other than the configured endpoint; the socket guard allows DNS and connections
for that one host only.

Without RFA_ENV_FILE (or when the file does not configure NVIDIA_MODEL/NVIDIA_API_KEY) every
item is recorded not_run with owner P1-002A and the test skips with the reason.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.parse import urlsplit

import anyio
import fixture_pack as pack
import harness as h
import httpx
import pytest
import test_rfa_scenarios as sc
from pydantic import ValidationError

from rfa_mas.bootstrap import inspect_configuration
from rfa_mas.settings import Settings

pytestmark = pytest.mark.real_network
ENV_FILE = os.environ.get("RFA_ENV_FILE")
OWNER_TASK = "P1-002A"
ITEMS = (
    ("E2E-02", "real-model-public-question"),
    ("E2E-06", "real-model-public-persona"),
    ("E2E-08", "real-model-public-draft"),
)
# P1-002's measured hosted latency ranged from ~20 s to >150 s: bounded, never unbounded.
REAL_LIMITS = {
    "http_timeout_seconds": 170,
    "tool_timeout_seconds": 400,
    "nvidia_max_output_tokens": 1024,
}
FORBIDDEN = (*h.DOC_CANARIES, *sc.INTERNAL_FACTS)
DATE_FORMS = {"2026-10-20": ("2026-10-20", "10월 20일", "2026.10.20", "2026/10/20")}
SCOPE = (
    "hosted NVIDIA model through the product ModelPort (P1-002); retrieval, policy and "
    "runtime local, review and publication mock"
)


def configured() -> tuple[dict[str, Any] | None, str | None]:
    """Stack overrides from the explicit env file, or the reason the gate cannot run."""
    if not ENV_FILE:
        return None, "RFA_ENV_FILE not set; the real-model gate is opt-in (coordinator run)"
    path = Path(ENV_FILE)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        return None, "RFA_ENV_FILE must be the absolute path of a regular file"
    try:
        source = Settings(_env_file=path)
    except ValidationError as exc:  # field names only, never input values
        fields = sorted({".".join(map(str, e["loc"])) for e in exc.errors()})
        return None, f"RFA_ENV_FILE does not parse as Settings: {fields}"
    if not source.nvidia_model or source.nvidia_api_key is None:
        return None, "RFA_ENV_FILE does not configure NVIDIA_MODEL/NVIDIA_API_KEY"
    overrides = {
        "model_provider": "nvidia",
        "nvidia_base_url": source.nvidia_base_url,
        "nvidia_model": source.nvidia_model,
        "nvidia_api_key": source.nvidia_api_key,
        **REAL_LIMITS,
    }
    return overrides, None


class GuardedModelTransport(httpx.AsyncBaseTransport):
    """The model port's only network path: one host; private content is never sent.

    Keeps no request or response body. Per HTTP attempt: host, path, the model id sent, the
    private-marker count, whether it passed the guard to the network, its outcome (response
    status, or the transport error such as a timeout that the product then retries) and time.
    """

    def __init__(self, host: str) -> None:
        self.host = host
        self.inner = httpx.AsyncHTTPTransport()
        self.calls: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8", "replace")
        markers = len(h.PRIVATE_CANARY.findall(body)) + sum(token in body for token in FORBIDDEN)
        try:
            model = json.loads(body).get("model") if body else None
        except ValueError:
            model = None
        call: dict[str, Any] = {
            "host": request.url.host,
            "path": request.url.path,
            "model": model,
            "private_markers": markers,
            "handed_to_network": False,
        }
        self.calls.append(call)
        if request.url.host != self.host or markers:
            call["refused"] = "host" if request.url.host != self.host else "private_content"
            raise httpx.ConnectError("refused by the E2E real-model guard", request=request)
        call["handed_to_network"] = True
        start = perf_counter()
        try:
            response = await self.inner.handle_async_request(request)
        except httpx.HTTPError as exc:
            call.update(outcome=type(exc).__name__, ms=round((perf_counter() - start) * 1000, 1))
            raise
        call.update(
            outcome=f"http_{response.status_code}",
            ms=round((perf_counter() - start) * 1000, 1),
        )
        return response

    async def aclose(self) -> None:
        await self.inner.aclose()


def fact_present(fact: str, content: str) -> bool:
    return any(form in content for form in DATE_FORMS.get(fact, (fact,)))


async def one_item(
    stack: h.Stack,
    recorder: h.Recorder,
    transport: GuardedModelTransport,
    item: dict[str, Any],
) -> None:
    scenario, variant, persona = item["scenario"], item["variant"], item["persona"]
    model_id = stack.settings.nvidia_model
    with recorder.attempt(scenario, variant) as a:
        a.bind(stack)
        a.set_context(
            mode="real-model",
            versions={"model": model_id, "prompt": "product default (P1-002 adapter)"},
            environment={
                "model_endpoint": transport.host,
                "concurrency": "sequential; one API request and model call at a time",
            },
        )
        principal = stack.people[persona]
        first_call = len(transport.calls)
        runs: list[dict[str, Any]] = []
        latencies: list[float] = []
        events: list[dict[str, Any]] = []
        try:
            async with stack.http(persona) as client:
                for index in range(h.CONFIG["sampling"]["real_model_repeats"]):
                    start = perf_counter()
                    if item["route"] == "assistant":
                        response = await client.post(
                            "/v1/assistant",
                            json={"text": item["text"], "domain_id": h.DOMAIN, "ingress": "public"},
                        )
                        routed = response.json()
                        run = routed.get("run") or {}
                        accepted = (
                            response.status_code == 201
                            and (routed.get("decision") or {}).get("intent") == "external_draft"
                        )
                    else:
                        response = await client.post(
                            "/v1/work", json=h.work_body(item["text"], audience="public")
                        )
                        run = response.json()
                        accepted = response.status_code == 201
                    latencies.append((perf_counter() - start) * 1000)
                    runs.append({"accepted": accepted, "run": run})
                    if run.get("run_id"):
                        ledger = await stack.container.service.observations.ledger(
                            run["run_id"], principal
                        )
                        events += [
                            {
                                "repeat": index + 1,
                                "mode": r.event.mode.value,
                                "status": r.event.status,
                                "duration_ms": r.event.duration_ms,
                            }
                            for r in ledger.observations
                            if r.event.event == "model" and r.event.status != "started"
                        ]
            calls = transport.calls[first_call:]
            drafts = [r["run"].get("draft") or {} for r in runs]
            a.observe("model_id_sent", sorted({str(c["model"]) for c in calls}))
            a.observe("model_calls", events)
            a.observe("outbound_requests", calls)
            a.observe("draft_adapters", sorted({str(d.get("adapter")) for d in drafts}))
            a.observe(
                "runs",
                [
                    {
                        "status": r["run"].get("status"),
                        "stop_reason": r["run"].get("stop_reason"),
                        "errors": [e.get("code") for e in r["run"].get("errors") or []],
                        "api_ms": round(ms, 1),
                    }
                    for r, ms in zip(runs, latencies, strict=True)
                ],
            )
            # Measurements and scores are facts of this run: record them before any check.
            present = [
                fact_present(fact, d.get("content", "")) for d in drafts for fact in item["facts"]
            ]
            a.score(
                item["quality"],
                sum(present) / len(present) if present else None,
                target=item.get("target"),
                owner=OWNER_TASK,
            )
            a.measure(
                "real_model_api_latency",
                latencies,
                scope="real-model",
                target_seconds=h.TARGETS["real_model_grounded_answer"],
                note="wall time of the API request, sequential, hosted endpoint",
            )
            a.measure(
                "real_model_call_duration",
                [e["duration_ms"] for e in events if e["duration_ms"] is not None],
                scope="real-model",
                note="model boundary observation (product ledger), includes product retries",
            )
            a.check(
                "runs_completed_with_a_draft",
                all(
                    r["accepted"]
                    and r["run"].get("status") == "completed"
                    and r["run"].get("draft")
                    for r in runs
                ),
            )
            a.check(
                "public_target_public_evidence_no_internal_fact_or_canary",
                all(
                    r["run"]["draft"]["target"]["audience"] == "public"
                    and sc.public_only(stack, r["run"])
                    and sc.cited_fixtures(stack, r["run"]) <= set(sc.MATRIX["read"][persona])
                    for r in runs
                ),
            )
            a.check(
                "every_model_call_real_mode_and_succeeded",
                {e["repeat"] for e in events} == set(range(1, len(runs) + 1))
                and all(e["mode"] == "real" and e["status"] == "succeeded" for e in events),
            )
            a.check(
                "draft_written_by_the_nvidia_adapter",
                all("nvidia-chat-completions" in r["run"]["draft"]["adapter"] for r in runs),
            )
            a.check(
                "outbound_only_to_the_model_host_with_zero_private_canaries",
                len(calls) >= len(runs)
                and all(
                    c["handed_to_network"]
                    and "refused" not in c
                    and c["host"] == transport.host
                    and c["private_markers"] == 0
                    for c in calls
                ),
            )
            a.check(
                "model_id_sent_is_the_configured_model",
                all(c["model"] == model_id for c in calls),
            )
        finally:
            a.real_executed(
                passed=bool(a.checks) and all(c["result"] == "passed" for c in a.checks),
                reason=SCOPE,
                next_task=OWNER_TASK,
                evidence={
                    "model_id": model_id,
                    "endpoint_host": transport.host,
                    "api_requests": len(runs),
                    "model_calls": len(events),
                    "modes": sorted({e["mode"] for e in events}),
                    "private_markers_outbound": sum(
                        c["private_markers"] for c in transport.calls[first_call:]
                    ),
                    "refused_outbound": sum("refused" in c for c in transport.calls[first_call:]),
                    "http_attempt_outcomes": sorted(
                        str(c.get("outcome")) for c in transport.calls[first_call:]
                    ),
                },
            )


async def test_real_model_public_subset(tmp_path, recorder, network_guard):
    overrides, reason = configured()
    if overrides is None:
        for scenario, variant in ITEMS:
            recorder.not_run(
                scenario, variant, reason=reason, next_task=OWNER_TASK, related=("P1-002",)
            )
        pytest.skip(reason)
    root = (tmp_path / "real").resolve()
    try:
        probe = inspect_configuration(h.controlled_settings(root, **overrides))
    except ValidationError as exc:  # field names only, never input values
        probe, fields = None, sorted({".".join(map(str, e["loc"])) for e in exc.errors()})
    if probe is None:
        reason = f"real model settings invalid: {fields}"
        for scenario, variant in ITEMS:
            recorder.not_run(scenario, variant, reason=reason, next_task=OWNER_TASK)
        pytest.skip(reason)
    if not probe.ready:
        reason = f"real model not buildable: {sorted([*probe.missing, *probe.invalid])}"
        for scenario, variant in ITEMS:
            recorder.not_run(scenario, variant, reason=reason, next_task=OWNER_TASK)
        pytest.skip(reason)
    host = urlsplit(overrides["nvidia_base_url"]).hostname or ""
    network_guard.allow_host(host)
    transport = GuardedModelTransport(host)
    golden = {q["question_id"]: q for q in pack.golden()}
    items = [
        {
            "scenario": "E2E-02",
            "variant": "real-model-public-question",
            "persona": "owner",
            "route": "work",
            "text": golden["G03"]["question"],
            "facts": golden["G03"]["expected_facts"],
            "quality": "key_fact_presence_g03",
            "target": h.CONFIG["quality_targets"]["key_fact_accuracy_min"],
        },
        {
            "scenario": "E2E-06",
            "variant": "real-model-public-persona",
            "persona": "external",
            "route": "work",
            "text": sc.SCENARIOS["E2E-06"]["questions"][0],
            "facts": ["2026-10-20"],
            "quality": "public_release_date_present",
        },
        {
            "scenario": "E2E-08",
            "variant": "real-model-public-draft",
            "persona": "owner",
            "route": "assistant",
            "text": sc.EXTERNAL,
            "facts": ["2026-10-20"],
            "quality": "public_release_date_present",
        },
    ]
    failures: list[str] = []
    try:
        async with h.open_stack(root, model_transport=transport, **overrides) as stack:
            await h.ingest(stack, sc.SMALL20)
            for item in items:
                try:
                    await one_item(stack, recorder, transport, item)
                except h.CheckFailed as exc:
                    failures.append(f"{item['scenario']}/{item['variant']}: {exc}")
                except Exception as exc:  # recorded as failed; the next item still runs
                    failures.append(f"{item['scenario']}/{item['variant']}: {type(exc).__name__}")
    finally:
        await transport.aclose()
    assert not failures, failures


# -- controlled self-tests of the gate's own guards (always run, no network) ----------------


async def test_model_transport_guard_refuses_private_content_and_other_hosts():
    transport = GuardedModelTransport("model.example")
    transport.inner = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    url = "https://model.example/v1/chat/completions"
    refused = (
        (url, {"model": "m", "messages": ["SYNTHETIC_PRIVATE_CANARY_OWNER_TEST"]}),
        (url, {"model": "m", "messages": ["내부 GPU Pool 예약"]}),
        ("https://other.example/v1/chat/completions", {"model": "m", "messages": ["공개"]}),
    )
    async with httpx.AsyncClient(transport=transport) as client:
        sent = await client.post(url, json={"model": "m", "messages": ["공개 FAQ 근거"]})
        for target, payload in refused:
            with pytest.raises(httpx.ConnectError):
                await client.post(target, json=payload)
    assert sent.status_code == 200
    assert [c.get("refused") for c in transport.calls] == [
        None,
        "private_content",
        "private_content",
        "host",
    ]
    assert [c["handed_to_network"] for c in transport.calls] == [True, False, False, False]
    assert transport.calls[0]["outcome"] == "http_200"
    assert all("messages" not in c for c in transport.calls)  # no body is kept


def test_real_gate_verdict_needs_the_functional_gate(tmp_path):
    local = h.Recorder(tmp_path / "reports")
    with local.attempt("E2E-99", "real-ok") as attempt:
        attempt.check("functional", True)
        attempt.real_executed(passed=True, reason="real", next_task="P1-002A", evidence={})
        attempt.set_context(mode="real-model", versions={"model": "m"})
    with pytest.raises(h.CheckFailed), local.attempt("E2E-99", "real-but-check-fails") as a:
        a.real_executed(passed=True, reason="real", next_task="P1-002A", evidence={})
        a.check("forbidden_disclosure_zero", False)
    ok, failed = local.reports
    assert ok["gates"]["real_integration"]["status"] == "passed"
    assert (ok["mode"], ok["versions"]["model"]) == ("real-model", "m")
    assert ok["versions"]["contract_schema"]  # the session context is kept
    assert failed["gates"]["real_integration"]["status"] == "failed"
    assert failed["complete_e2e"] is False


async def test_network_guard_allows_exactly_one_named_host_given_as_text_or_bytes(monkeypatch):
    resolved = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.7", 443))]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: resolved)  # fake resolver
    guard = h.NetworkGuard()
    guard.install(monkeypatch)
    guard.allow_host("Model.Example")
    assert socket.getaddrinfo(b"model.example", 443) == resolved  # anyio passes bytes
    assert socket.getaddrinfo("model.example", 443) == resolved
    # The path httpx/httpcore actually take (anyio -> event loop executor -> socket).
    assert [entry[4][0] for entry in await anyio.getaddrinfo("model.example", 443)] == [
        "203.0.113.7"
    ]
    assert guard._permitted("203.0.113.7") and not guard._permitted("198.51.100.1")
    with pytest.raises(h.NetworkBlocked):
        socket.getaddrinfo(b"other.example", 443)
    assert guard.attempts == ["getaddrinfo"]
