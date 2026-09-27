from __future__ import annotations

import argparse
import asyncio
import json
import re
import signal
import sqlite3
import tempfile
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import uvicorn
from pydantic import SecretStr, ValidationError
from pydantic_settings import SettingsError

from rfa_mas.adapters.scheduler import ManualClock
from rfa_mas.api.app import create_app
from rfa_mas.bootstrap import (
    PROJECT_ROOT,
    build_container,
    build_scheduler_runner,
    inspect_configuration,
    run_langfuse_retention,
)
from rfa_mas.contracts import (
    Audience,
    DomainId,
    DraftTarget,
    KnowledgeExport,
    KnowledgeWrite,
    SimulationScenario,
    TrustedPrincipal,
    WorkRequest,
)
from rfa_mas.dev_env import initialize_dev_env
from rfa_mas.errors import BackendNotImplementedError, ConfigurationError, RfaError
from rfa_mas.knowledge_facade.app import create_knowledge_facade_app
from rfa_mas.settings import PLANNED_SETTINGS, Settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rfa", description="RFA MAS P0 developer commands")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Explicit settings file for runtime commands (default: Settings loads .env)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("api", help="Start the local FastAPI service")
    init_env = subparsers.add_parser(
        "init-env",
        help="Create or complete a private env profile with distinct local credentials",
    )
    init_env.add_argument(
        "--output",
        default=".env",
        help="Ignored env profile to create (default: .env)",
    )

    demo = subparsers.add_parser("demo", help="Run the deterministic local demo")
    demo.add_argument(
        "--domain",
        choices=[item.value for item in DomainId],
        default=DomainId.TRIV3.value,
    )
    demo.add_argument(
        "--audience",
        choices=[item.value for item in Audience],
        default=Audience.PUBLIC.value,
    )
    demo.add_argument(
        "--scenario",
        choices=[item.value for item in SimulationScenario],
        default=SimulationScenario.SUCCESS.value,
    )
    demo.add_argument("--query", default=None)
    demo.add_argument(
        "--full",
        action="store_true",
        help="Run the synthetic end-to-end story on a temp data dir (mock/local, offline)",
    )
    demo.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="--full only: empty or new directory for the demo DB (default: a new temp dir)",
    )
    demo.add_argument(
        "--materials",
        type=Path,
        default=None,
        help="--full only: synthetic materials JSONL (default: fixtures/demo/materials.jsonl)",
    )

    subparsers.add_parser("doctor", help="Show configured/missing variable names without values")

    retention = subparsers.add_parser(
        "langfuse-retention",
        help="Delete this app's Langfuse traces older than TRACE_RETENTION_DAYS (run daily)",
    )
    retention.add_argument(
        "--dry-run",
        action="store_true",
        help="Count expired traces without deleting anything",
    )

    scheduler = subparsers.add_parser(
        "scheduler",
        help="Run the single-owner APScheduler runner (separate from the API; one process)",
    )
    scheduler.add_argument(
        "--run-seconds",
        type=float,
        default=None,
        help="Stop after this many seconds (smoke checks); default runs until SIGINT/SIGTERM",
    )

    evaluation = subparsers.add_parser("evaluate", help="Run isolated synthetic rule evaluation")
    evaluation.add_argument(
        "--dataset", default="persona-core-v1", help="persona-core-v1 or persona-regression-v2"
    )
    evaluation.add_argument("--judge", default="disabled", help="disabled or mock only")
    evaluation.add_argument("--label", default="run", help="v2 run label, e.g. baseline")
    evaluation.add_argument("--output", default=None, help="v2: write the run manifest (new file)")
    evaluation.add_argument("--mode", default="simulated", choices=["simulated", "actual"])
    evaluation.add_argument(
        "--allow-actual",
        action="store_true",
        help="Explicit opt-in for actual-model mode; without a provider every case is not_run",
    )

    compare = subparsers.add_parser(
        "evaluate-compare", help="Compare two recorded persona-regression-v2 run manifests"
    )
    compare.add_argument("--baseline", required=True)
    compare.add_argument("--candidate", required=True)
    compare.add_argument("--output", default=None, help="Write the comparison (new file)")

    openapi = subparsers.add_parser("openapi", help="Export the generated OpenAPI document")
    openapi.add_argument("--output", default="openapi.json")
    openapi.add_argument(
        "--app",
        choices=("core", "knowledge-facade"),
        default="core",
        help="core: RFA MAS API; knowledge-facade: RFA_module knowledge contract provider",
    )

    facade = subparsers.add_parser(
        "knowledge-facade",
        help="Serve RFA_module's knowledge contract (GET /tasks, POST /tasks/{id}/ask)",
    )
    facade.add_argument(
        "--host", default="127.0.0.1", help="RequestForApproval modules.yaml runs it on 0.0.0.0"
    )
    facade.add_argument(
        "--port", type=int, default=8795, help="8791 is RFA_module head_stub's port; do not reuse it"
    )
    facade.add_argument(
        "--audience",
        choices=("public", "company", "business_unit"),
        default="public",
        help="Evidence audience the writer channel may receive (default public)",
    )
    facade.add_argument(
        "--api-key-env",
        default="KNOWLEDGE_FACADE_API_KEY",
        help="Env var holding the bearer key required for a non-public audience",
    )
    return parser


async def _demo(args: argparse.Namespace, settings: Settings) -> int:
    container = build_container(settings)
    await container.startup()
    try:
        domain = DomainId(args.domain)
        query = args.query or (
            "TRIV3 공개 트랙을 근거와 함께 요약해 줘."
            if domain == DomainId.TRIV3
            else "Quantization Research 공개 비교 기준을 근거와 함께 요약해 줘."
        )
        request = WorkRequest(
            query=query,
            domain_id=domain,
            target=DraftTarget(audience=Audience(args.audience)),
            simulation_scenario=SimulationScenario(args.scenario),
        )
        principal = TrustedPrincipal(
            user_id="fixture-owner-001",
            authenticated=True,
            company_id="local-company",
            business_units=frozenset({"triv3-team", "quantization-research-team"}),
            roles=frozenset({"company", "local_development_identity"}),
        )
        result = await container.service.run(request, principal)
        print(result.model_dump_json(indent=2))
        return 0 if result.status.value in {"completed", "waiting_approval"} else 1
    finally:
        await container.shutdown()


# -- P0-026: `rfa demo --full` synthetic end-to-end story ---------------------------------
# Offline composition only: temp SQLite data dir, mock model/review/publication, local
# retrieval/policy/runtime/trace, manual-clock scheduler. No key, GPU, Docker or network; the
# in-process app is called over an ASGI transport as the installation's own local owner.
DEMO_MATERIALS = PROJECT_ROOT / "fixtures" / "demo" / "materials.jsonl"
DEMO_DOMAIN = DomainId.TRIV3.value
DEMO_BENCHMARK_GOAL = "TRIV-DEMO benchmark 로그를 검증하고 A/B 결과를 비교해줘."
DEMO_FOLLOW_UP = "같은 작업의 benchmark 결과를 다시 분석해줘."
DEMO_OWNER_QUERY = "TRIV-DEMO SDK 출시 준비 상태와 내부 검증 일정을 알려줘."
DEMO_EXTERNAL_REQUEST = "TRIV-DEMO SDK 공식 출시일과 설치 명령을 고객 문의에 답변해 주세요."
DEMO_FULL_SETTINGS = {
    "_env_file": None,
    "app_api_key": None,
    "log_level": "WARNING",
    "model_provider": "mock",
    "retriever_backend": "local",
    "response_backend": "mock",
    "tool_backend": "mock",
    "runtime_backend": "local",
    "policy_backend": "local",
    "trace_backend": "local",
    "enable_judge": False,
    "enable_nat": False,
    "enable_debate": False,
    "enable_auto_domain_creation": False,
    "scheduler_enabled": True,
    "allow_external_writes": False,
    "allow_external_egress": False,
}
DEMO_NOT_RUN = {
    "real_model": "P1-002A (opt-in NVIDIA gate; this demo uses the mock model)",
    "real_publication_or_teammate_service": "P1-008A (this demo publishes to the mock sink)",
    "openshell_sandbox_runtime": "P1-007B/P1-007C (this demo uses the local role runtime)",
    "installed_nat": "P0-028",
    "ui": "P0-025A (API/report only)",
}


def _demo_data_dir(value: Path | None) -> Path:
    if value is None:
        return Path(tempfile.mkdtemp(prefix="rfa-demo-")).resolve()
    root = value.expanduser().resolve()
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise RfaError("demo_data_dir_not_empty", "빈 데모 데이터 경로가 필요합니다.")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _demo_materials(path: Path) -> list[dict[str, Any]]:
    materials = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    for item in materials:
        model = KnowledgeWrite if item["channel"] == "note" else KnowledgeExport
        model.model_validate(item["request"])
    return materials


def _demo_next_fire() -> datetime:
    """The next 09:00 Asia/Seoul at least a day ahead (the schedule's own cron time)."""
    local = datetime.now(ZoneInfo("Asia/Seoul")) + timedelta(days=1)
    return local.replace(hour=9, minute=0, second=0, microsecond=0)


class _DemoApi:
    """In-process calls to the product API as the installation owner (loopback, keyless).

    Every response body is kept for the privacy scan except ingest acknowledgements, which
    only echo the owner's own input.
    """

    def __init__(self, container) -> None:
        self.outputs: list[str] = []
        self.public: list[str] = []
        transport = httpx.ASGITransport(
            app=create_app(container=container), client=("127.0.0.1", 1)
        )
        self.client = httpx.AsyncClient(transport=transport, base_url="http://rfa.demo")

    async def __call__(self, method, path, *, body=None, params=None, echo=False, public=False):
        response = await self.client.request(method, path, json=body, params=params)
        if not echo:
            self.outputs.append(response.text)
        if public:
            self.public.append(response.text)
        return response.status_code, response.json()

    async def aclose(self) -> None:
        await self.client.aclose()


def _demo_team(team: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    findings = team.get("findings") or {}
    runner = findings.get("experiment_runner") or {}
    analyst = findings.get("result_analyst") or {}
    return {
        "task_id": team.get("task_id"),
        "team_id": team.get("team_id"),
        "pattern": team.get("pattern"),
        "status": team.get("status"),
        "roles": [[r.get("role"), r.get("status")] for r in team.get("roles", [])],
        "experiment": {
            "mode": runner.get("mode"),
            "simulated_experiment": runner.get("simulated_experiment"),
            "runs": [
                {
                    "label": r.get("label"),
                    "material": names.get(r.get("source_id"), "other"),
                    "source_revision": r.get("source_revision"),
                    "latency_ms": r.get("latency_ms"),
                    "accuracy_pct": r.get("accuracy_pct"),
                    "tentative": r.get("tentative"),
                }
                for r in runner.get("runs", [])
            ],
        },
        "comparisons": [
            {
                "latency_change_pct": c.get("latency_change_pct"),
                "accuracy_delta_pp": c.get("accuracy_delta_pp"),
                "same_environment": c.get("same_environment"),
            }
            for c in analyst.get("comparisons", [])
        ],
        "analysis_simulated": analyst.get("simulated_experiment"),
        "tokens": (team.get("usage") or {}).get("tokens"),
    }


def _demo_evidence(run: dict[str, Any], names: dict[str, str]) -> list[dict[str, Any]]:
    draft = run.get("draft") or {}
    return [
        {
            "material": names.get(e.get("source_id"), "builtin:" + str(e.get("source_id"))),
            "source_id": e.get("source_id"),
            "source_revision": e.get("source_revision"),
            "audience": e.get("audience"),
        }
        for e in draft.get("allowed_evidence", [])
    ]


def _demo_run(run: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    draft = run.get("draft") or {}
    return {
        "run_id": run.get("run_id"),
        "status": run.get("status"),
        "stop_reason": run.get("stop_reason"),
        "simulated": run.get("simulated"),
        "target": (draft.get("target") or {}).get("audience"),
        "draft_adapter": draft.get("adapter"),
        "policy_version": draft.get("policy_version"),
        "evidence": _demo_evidence(run, names),
    }


def _demo_review(review: dict[str, Any] | None) -> dict[str, Any] | None:
    if not review:
        return None
    return {
        key: review.get(key) for key in ("draft_id", "draft_version", "decision", "content_hash")
    }


def _demo_counts(database: Path) -> dict[str, int]:
    """Read-only counts from the demo's own database, after every container is closed."""
    queries = {
        "publications": "SELECT count(*) FROM publications",
        "publication_ledger_entries": (
            "SELECT count(*) FROM effect_ledger WHERE operation_key LIKE 'publication:%'"
        ),
        "product_tasks": "SELECT count(*) FROM product_tasks",
        "run_team_bindings": "SELECT count(*) FROM run_team_bindings",
        "runs": "SELECT count(*) FROM runs",
    }
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        return {name: connection.execute(sql).fetchone()[0] for name, sql in queries.items()}
    finally:
        connection.close()


async def _demo_full(args: argparse.Namespace) -> int:
    root = _demo_data_dir(args.data_dir)
    materials = _demo_materials(args.materials)
    settings = Settings(
        database_url=f"sqlite:///{root / 'rfa.db'}", trace_dir=root / "traces", **DEMO_FULL_SETTINGS
    )
    private = sorted({m for i in materials for m in i.get("markers", {}).get("private", [])})
    internal = sorted({m for i in materials for m in i.get("markers", {}).get("internal", [])})
    steps: list[dict[str, Any]] = []
    checks: dict[str, bool] = {}
    names: dict[str, str] = {}
    outputs: list[str] = []
    public: list[str] = []

    def step(number: int, name: str, mode: str, **fields: Any) -> None:
        steps.append({"step": number, "name": name, "mode": mode, **fields})

    container = build_container(settings)
    await container.startup()
    adapters = [item.model_dump(mode="json") for item in container.adapters]
    model_mode = (
        "mock" if any(a["port"] == "model" and a["simulated"] for a in adapters) else "real"
    )
    api = _DemoApi(container)
    try:
        # 1. ingest the six synthetic materials through the knowledge API.
        ingested: dict[str, dict[str, Any]] = {}
        for item in materials:
            if item["channel"] == "note":
                code, body = await api(
                    "POST", "/v1/knowledge/sources", body=item["request"], echo=True
                )
                document = body.get("document") or {}
                receipt = {
                    "status": "accepted" if code == 201 else f"http_{code}",
                    "source_id": document.get("source_id"),
                    "source_revision": document.get("source_revision"),
                }
                audience = item["request"]["acl"]["audience"]
            else:
                code, body = await api(
                    "POST", "/v1/knowledge/imports", body=item["request"], echo=True
                )
                row = (body.get("rows") or [{}])[0]
                receipt = {key: row.get(key) for key in ("status", "source_id", "source_revision")}
                audience = item["request"]["rows"][0]["acl"]["audience"]
            ingested[item["material"]] = {
                "channel": item["channel"],
                "audience": audience,
                **receipt,
            }
            if receipt["source_id"]:
                names[receipt["source_id"]] = item["material"]
        checks["materials_ingested"] = len(ingested) == len(materials) and all(
            r["status"] == "accepted" and r["source_id"] for r in ingested.values()
        )
        step(1, "ingest", "local", materials=ingested)

        # 2. derived decisions/claims and Todo candidates (deterministic rules, no model).
        code, derived = await api("POST", "/v1/knowledge/derive", params={"domain_id": DEMO_DOMAIN})
        code2, found = await api(
            "POST", "/v1/candidates/discover", params={"domain_id": DEMO_DOMAIN}
        )
        code3, listed = await api("GET", "/v1/knowledge/derived", params={"domain_id": DEMO_DOMAIN})
        for item in listed if isinstance(listed, list) else []:
            parents = sorted({names.get(p["source_id"], "builtin") for p in item["parents"]})
            names[item["reference"]["source_id"]] = "derived:" + "+".join(parents)
        accepted = [i for i in derived.get("items", []) if i.get("review_state") == "accepted"]
        candidates = (
            [
                {
                    "candidate_id": c["candidate_id"],
                    "kind": c["kind"],
                    "state": c["state"],
                    "due_date": c.get("due_date"),
                    "blocker": c.get("blocker"),
                    "parents": sorted({names.get(p["source_id"], "builtin") for p in c["parents"]}),
                }
                for c in found
            ]
            if isinstance(found, list)
            else []
        )
        checks["derive_and_candidates_ran"] = code == code2 == 200 and bool(candidates)
        step(
            2,
            "derive_and_candidates",
            "local",
            rules="deterministic cue rules, no model",
            derived={
                "accepted": len(accepted),
                "kinds": sorted({i["kind"] for i in accepted}),
                "parents": sorted(
                    {
                        names.get(p["source_id"], "builtin")
                        for i in accepted
                        for p in i.get("parents", [])
                    }
                ),
            },
            candidates=candidates,
        )

        # 3. a Benchmark team task (roles on the local runtime; numbers from synthetic logs).
        code, session = await api("POST", "/v1/sessions")
        session_id = session["session_id"]
        team_body = {
            "schema_version": "1.1",
            "query": DEMO_BENCHMARK_GOAL,
            "domain_id": DEMO_DOMAIN,
            "target": {"audience": "owner"},
            "team": {"goal": DEMO_BENCHMARK_GOAL, "outputs": ["benchmark_report"]},
        }
        code, bench = await api("POST", f"/v1/sessions/{session_id}/work", body=team_body)
        code, team = await api("GET", f"/v1/runs/{bench['run_id']}/team")
        first = _demo_team(team, names)
        checks["benchmark_team_completed_and_labelled_simulated"] = (
            bench.get("status") == "completed"
            and first["pattern"] == "benchmark"
            and first["experiment"]["simulated_experiment"] is True
            and first["experiment"]["mode"] == "fixture_log_parse"
            and bench.get("simulated") is True
        )
        step(
            3,
            "benchmark_team_task",
            "simulated",
            session_id=session_id,
            run=_demo_run(bench, names),
            team=first,
            note="local role runtime and synthetic log parsing; not a GPU/model measurement",
        )

        # 4. a follow-up in the same session and Task reuses the Task's team.
        follow_body = {**team_body, "query": DEMO_FOLLOW_UP, "task_id": first["task_id"]}
        code, follow = await api("POST", f"/v1/sessions/{session_id}/work", body=follow_body)
        code, follow_team = await api("GET", f"/v1/runs/{follow['run_id']}/team")
        second = _demo_team(follow_team, names)
        checks["follow_up_reuses_the_team"] = (
            follow.get("status") == "completed"
            and follow["run_id"] != bench["run_id"]
            and (second["task_id"], second["team_id"]) == (first["task_id"], first["team_id"])
        )
        step(4, "follow_up_reuses_team", "simulated", run=_demo_run(follow, names), team=second)

        # 5. the owner's internal status answer (owner target; private notes never used).
        code, owner_run = await api(
            "POST",
            "/v1/work",
            body={
                "query": DEMO_OWNER_QUERY,
                "domain_id": DEMO_DOMAIN,
                "target": {"audience": "owner"},
            },
        )
        owner_view = _demo_run(owner_run, names)
        checks["owner_answer_uses_internal_not_private_evidence"] = (
            owner_run.get("status") == "completed"
            and any(e["audience"] == "owner" for e in owner_view["evidence"])
            and all(e["audience"] != "private" for e in owner_view["evidence"])
        )
        step(5, "owner_internal_status_answer", model_mode, run=owner_view)

        # 6. an external request becomes a public DRAFT from the public FAQ only.
        code, assisted = await api(
            "POST",
            "/v1/assistant",
            public=True,
            body={"text": DEMO_EXTERNAL_REQUEST, "domain_id": DEMO_DOMAIN, "ingress": "public"},
        )
        external = assisted.get("run") or {}
        run_id = external.get("run_id")
        external_view = _demo_run(external, names)
        checks["external_public_draft_from_public_faq_only"] = (
            (assisted.get("decision") or {}).get("intent") == "external_draft"
            and external.get("status") == "completed"
            and external_view["target"] == "public"
            and bool(external_view["evidence"])
            and all(e["audience"] == "public" for e in external_view["evidence"])
            # The FAQ itself, or items derived only from it (staged context uses summaries).
            and {e["material"] for e in external_view["evidence"]}
            <= {"public_faq", "derived:public_faq"}
        )
        step(
            6,
            "external_public_draft",
            model_mode,
            intent=(assisted.get("decision") or {}).get("intent"),
            run=external_view,
        )

        # 7. an edit invalidates the approval; a publish attempt is refused.
        code, before = await api("GET", f"/v1/runs/{run_id}/draft", public=True)
        code, edited = await api(
            "POST",
            f"/v1/runs/{run_id}/draft/edits",
            public=True,
            body={
                "expected_version": before["current_version"],
                "content": before["draft"]["content"] + "\n(공개 FAQ 기준 문구 수정)",
            },
        )
        refused_code, refused = await api(
            "POST",
            f"/v1/runs/{run_id}/publication",
            public=True,
            body={"idempotency_key": "demo-before-review"},
        )
        checks["edit_invalidates_the_approval"] = (
            before["approval_valid"] is True
            and edited["approval_valid"] is False
            and edited["invalid_reason"] == "draft_changed"
            and refused_code == 409
            and refused.get("code") == "approval_required"
        )
        step(
            7,
            "edit_invalidates_approval",
            "mock",
            before={
                "version": before["current_version"],
                "approval_valid": before["approval_valid"],
                "approval": _demo_review(before.get("review")),
            },
            after={
                "version": edited["current_version"],
                "approval_valid": edited["approval_valid"],
                "invalid_reason": edited["invalid_reason"],
            },
            publish_attempt={"http_status": refused_code, "code": refused.get("code")},
        )

        # 8. re-review binds the edited version.
        code, reviewed = await api("POST", f"/v1/runs/{run_id}/draft/review", public=True)
        review = reviewed.get("review") or {}
        checks["re_review_binds_the_latest_version"] = (
            reviewed["approval_valid"] is True
            and review.get("draft_version")
            == reviewed["current_version"]
            == edited["current_version"]
        )
        step(
            8,
            "re_review",
            "mock",
            version=reviewed["current_version"],
            approval_valid=reviewed["approval_valid"],
            approval=_demo_review(review),
        )

        # 9. exactly one mock publication (replay returns the same receipt).
        code, receipt = await api(
            "POST",
            f"/v1/runs/{run_id}/publication",
            public=True,
            body={"idempotency_key": "demo-publish"},
        )
        code, replay = await api(
            "POST",
            f"/v1/runs/{run_id}/publication",
            public=True,
            body={"idempotency_key": "demo-publish"},
        )
        other_code, other = await api(
            "POST",
            f"/v1/runs/{run_id}/publication",
            public=True,
            body={"idempotency_key": "demo-publish-again"},
        )
        checks["single_mock_publication"] = (
            receipt.get("status") == "succeeded"
            and receipt.get("mode") == "mock"
            and replay.get("publication_id") == receipt.get("publication_id")
            and other_code == 409
            and other.get("code") == "publication_exists"
        )
        step(
            9,
            "publication",
            receipt.get("mode") or "mock",
            receipt={
                "publication_id": receipt.get("publication_id"),
                "status": receipt.get("status"),
                "mode": receipt.get("mode"),
                "approval_ref": receipt.get("approval_id"),
                "version": (receipt.get("binding") or {}).get("version"),
                "external_result_ref": receipt.get("external_result_ref"),
            },
            replay_same_receipt=replay.get("publication_id") == receipt.get("publication_id"),
            second_key={"http_status": other_code, "code": other.get("code")},
        )

        # 10. a scheduled briefing fired by the manual-clock scheduler runner (duplicate tick).
        code, schedule = await api(
            "POST",
            "/v1/schedules",
            body={
                "job_type": "briefing",
                "domain_id": DEMO_DOMAIN,
                "cron": "0 9 * * *",
                "timezone": "Asia/Seoul",
            },
        )
        fire = _demo_next_fire()
        clock = ManualClock(fire - timedelta(minutes=30))
        runner = build_scheduler_runner(
            settings, container, clock=clock, sync_interval_seconds=None
        )
        await runner.start()
        try:
            clock.set(fire)
            await runner.tick()
            await runner.tick()  # the same fire delivered again
        finally:
            await runner.stop()
        code, fired = await api("GET", f"/v1/schedules/{schedule['schedule_id']}/runs")
        code, notices = await api("GET", "/v1/notifications")
        briefings = [n for n in notices if n.get("kind") == "briefing"]
        checks["briefing_fired_once"] = (
            [r.get("status") for r in fired] == ["succeeded"]
            and len(briefings) == 1
            and all(n.get("audience") == "owner" for n in notices)
        )
        step(
            10,
            "scheduled_briefing",
            "local",
            clock="manual (P0-023 ManualClock adapter)",
            schedule_id=schedule["schedule_id"],
            fire_time=fire.isoformat(),
            runs=[
                {"status": r.get("status"), "scheduled_fire_time": r.get("scheduled_fire_time")}
                for r in fired
            ],
            briefing_items=[
                {
                    "candidate_id": i.get("candidate_id"),
                    "rank": i.get("rank"),
                    "due_date": i.get("due_date"),
                    "blocker": i.get("blocker"),
                }
                for n in briefings
                for i in n.get("items", [])
            ],
        )
    finally:
        outputs += api.outputs
        public += api.public
        await api.aclose()
        await container.shutdown()

    # 11. restart: a fresh container on the same database keeps every durable record.
    container = build_container(settings)
    await container.startup()
    api = _DemoApi(container)
    try:
        code, sessions = await api("GET", "/v1/sessions")
        code, record = await api("GET", f"/v1/runs/{bench['run_id']}")
        code, team_after = await api("GET", f"/v1/runs/{bench['run_id']}/team")
        code, state_after = await api("GET", f"/v1/runs/{run_id}/draft", public=True)
        code, receipt_after = await api("GET", f"/v1/runs/{run_id}/publication", public=True)
        code, replay_after = await api(
            "POST",
            f"/v1/runs/{run_id}/publication",
            public=True,
            body={"idempotency_key": "demo-publish"},
        )
        code, fired_after = await api("GET", f"/v1/schedules/{schedule['schedule_id']}/runs")
        checks["restart_preserves_state"] = (
            session_id in {s.get("session_id") for s in sessions}
            and record.get("status") == "completed"
            and (team_after.get("task_id"), team_after.get("team_id"))
            == (first["task_id"], first["team_id"])
            and state_after.get("publication_status") == "succeeded"
            and state_after.get("current_version") == reviewed["current_version"]
            and receipt_after.get("publication_id") == receipt.get("publication_id")
            and replay_after.get("publication_id") == receipt.get("publication_id")
            and len(fired_after) == len(fired)
        )
        restart = {
            "session_listed": session_id in {s.get("session_id") for s in sessions},
            "benchmark_run_status": record.get("status"),
            "team_ref": [team_after.get("task_id"), team_after.get("team_id")],
            "draft_version": state_after.get("current_version"),
            "publication_status": state_after.get("publication_status"),
            "publication_id": receipt_after.get("publication_id"),
            "replay_same_receipt": replay_after.get("publication_id")
            == receipt.get("publication_id"),
            "schedule_runs": len(fired_after),
        }
    finally:
        outputs += api.outputs
        public += api.public
        await api.aclose()
        await container.shutdown()
    counts = _demo_counts(root / "rfa.db")
    checks["exactly_one_publication_record"] = (
        counts["publications"] == 1 and counts["publication_ledger_entries"] == 1
    )
    step(11, "restart_same_database", "local", after_restart=restart, database_counts=counts)

    traces = [
        p.read_text(encoding="utf-8", errors="replace")
        for p in sorted((root / "traces").rglob("*"))
        if p.is_file()
    ]
    report: dict[str, Any] = {
        "demo": "rfa-demo-full/1",
        "data_dir": str(root),
        "identity": "installation owner (keyless loopback, local development)",
        "adapters": adapters,
        "steps": steps,
        "not_run": DEMO_NOT_RUN,
    }
    rendered = json.dumps(report, ensure_ascii=False)
    scanned = [*outputs, *traces, rendered]
    private_hits = {m: sum(text.count(m) for text in scanned) for m in private}
    public_hits = {m: sum(text.count(m) for text in public) for m in [*private, *internal]}
    checks["no_private_canary_in_any_output"] = not any(private_hits.values())
    checks["no_internal_or_private_fact_in_public_outputs"] = not any(public_hits.values())
    report["privacy"] = {
        "scope": "every API response except ingest acknowledgements (echo of the owner's own "
        "input), every trace file and this report",
        "responses_scanned": len(outputs),
        "trace_files_scanned": len(traces),
        "public_responses_scanned": len(public),
        "private_marker_hits": sum(private_hits.values()),
        "public_output_internal_or_private_hits": sum(public_hits.values()),
    }
    report["checks"] = checks
    report["ok"] = all(checks.values())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


async def _scheduler(args: argparse.Namespace, settings: Settings) -> int:
    """Dedicated runner process. The FastAPI app/workers never start a scheduler."""
    if not settings.scheduler_enabled:
        print(json.dumps({"code": "scheduler_disabled", "setting": "SCHEDULER_ENABLED"}))
        return 2
    container = build_container(settings)
    await container.startup()
    runner = build_scheduler_runner(settings, container)
    try:
        try:
            await runner.start()
        except RfaError as exc:
            if exc.code != "scheduler_owner_locked":
                raise
            print(json.dumps({"code": exc.code}))
            return 3
        print(
            json.dumps(
                {
                    "status": "scheduler_running",
                    "jobs": len(runner.jobs()),
                    "sync": runner.last_sync,
                }
            )
        )
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        signals = (signal.SIGINT, signal.SIGTERM)
        for signum in signals:
            loop.add_signal_handler(signum, stop.set)
        try:
            await asyncio.wait_for(stop.wait(), timeout=args.run_seconds)
        except TimeoutError:
            pass
        finally:
            for signum in signals:
                loop.remove_signal_handler(signum)
        return 0
    finally:
        await runner.stop()
        await container.shutdown()


def sqlite_runtime_status() -> dict[str, object]:
    """Read linked library version only; never opens a DB or upgrades the environment."""
    measured = sqlite3.sqlite_version
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", measured)
    version = tuple(map(int, match.groups())) if match else ()
    assessment = "unknown"
    if version and version[0] == 3:
        if (
            version >= (3, 51, 3)
            or (version[:2] == (3, 50) and version[2] >= 7)
            or (version[:2] == (3, 44) and version[2] >= 6)
        ):
            assessment = "fixed"
        elif version >= (3, 7, 0):
            assessment = "affected"
    return {
        "version": measured if match else "unknown",
        "wal_reset_patch": assessment,
        "assessment_basis": "official_release_version_not_binary_attestation",
        "warning": None if assessment == "fixed" else f"sqlite_wal_reset_{assessment}",
        "advisory": "https://sqlite.org/wal.html#walresetbug",
        "integrity_check": "not_run",
        "concurrency_check": "not_run",
        "rfa_e2e": "not_run",
    }


def _doctor(settings: Settings) -> int:
    inspection = inspect_configuration(settings)
    payload = {
        "ready": inspection.ready,
        "ready_scope": "preflight_only",
        "configuration_ready": inspection.configuration_ready,
        "implementation_ready": inspection.implementation_ready,
        "local_lifecycle": "not_run",
        "provider_probe": "not_run",
        "selected_modes": {
            "model": settings.model_provider,
            "retriever": settings.retriever_backend,
            "response": settings.response_backend,
            "tool": settings.tool_backend,
            "runtime": settings.runtime_backend,
            "policy": settings.policy_backend,
            "trace": settings.trace_backend,
            "judge": settings.judge_provider if settings.enable_judge else "disabled",
            "nat": "selected_unavailable" if settings.enable_nat else "disabled",
            # Enabled means only the dedicated `rfa scheduler` process may run jobs.
            "scheduler": "rfa_scheduler_cli" if settings.scheduler_enabled else "disabled",
        },
        "variables": [item.model_dump(mode="json") for item in settings.doctor_statuses()],
        "missing": list(inspection.missing),
        "invalid": list(inspection.invalid),
        "reserved_not_implemented": list(inspection.reserved),
        "nat_dependency": inspection.nat_dependency,
        "planned_settings": [asdict(item) for item in PLANNED_SETTINGS],
        "external_writes_requested": settings.allow_external_writes,
        "external_writes_effective": settings.external_writes_effective,
        "external_egress_requested": settings.allow_external_egress,
        "external_egress_effective": settings.external_egress_effective,
        "local_identity_is_production_auth": False,
        "runtime_checks": {"sqlite": sqlite_runtime_status()},
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload["ready"] else 1


_RELEASE_EXIT = {"pass": 0, "fail": 1, "incomplete": 2}


def _reject(code: str) -> None:
    print(json.dumps({"code": code}))
    raise SystemExit(2)


def _write_new(path_text: str | None, payload: str) -> None:
    """Evaluation artifacts are new files only: never overwrite or follow a symlink."""
    if path_text is None:
        return
    path = Path(path_text)
    if path.is_symlink() or path.exists():
        _reject("evaluation_output_exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(payload + "\n")


def _read_manifest(path_text: str):
    from rfa_mas.application.evaluation import RegressionRun

    path = Path(path_text)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 5_000_000:
        _reject("evaluation_manifest_invalid")
    try:
        return RegressionRun.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValidationError, ValueError, UnicodeDecodeError):
        # Never echo manifest content or parser messages.
        _reject("evaluation_manifest_invalid")


def _evaluate_regression(args: argparse.Namespace) -> None:
    from rfa_mas.application.evaluation import run_persona_regression

    if args.judge != "disabled":
        _reject("evaluation_configuration_rejected")
    try:
        run = asyncio.run(
            run_persona_regression(label=args.label, mode=args.mode, allow_actual=args.allow_actual)
        )
    except ValueError as exc:
        code = str(exc) if str(exc).startswith("evaluation_") else "evaluation_error"
        print(json.dumps({"code": code, "product_final_gate": "not_run"}))
        raise SystemExit(2) from None
    except Exception:
        print(json.dumps({"code": "evaluation_error", "product_final_gate": "not_run"}))
        raise SystemExit(2) from None
    payload = run.model_dump_json(indent=2)
    _write_new(args.output, payload)
    print(payload)
    raise SystemExit(_RELEASE_EXIT[run.release_gate])


def _evaluate_compare(args: argparse.Namespace) -> None:
    from rfa_mas.application.evaluation import compare_regression_runs

    comparison = compare_regression_runs(
        _read_manifest(args.baseline), _read_manifest(args.candidate)
    )
    payload = comparison.model_dump_json(indent=2)
    _write_new(args.output, payload)
    print(payload)
    raise SystemExit(_RELEASE_EXIT[comparison.release_gate])


def _knowledge_facade(args: argparse.Namespace, settings: Settings) -> int:
    """Serve 승희's knowledge contract from the local core (P1-010).

    A public facade may bind a non-loopback host without a key because it serves public,
    shareable evidence only. Any other audience needs a bearer key from the environment
    (never a CLI argument, so it does not land in process listings or shell history).
    """
    import os

    audience = Audience(args.audience)
    api_key = None
    if audience != Audience.PUBLIC:
        raw = os.environ.get(args.api_key_env)
        if not raw:
            print(
                json.dumps(
                    {
                        "code": "configuration_error",
                        "message": f"{args.api_key_env} is required for a {audience.value} facade",
                    }
                )
            )
            return 2
        api_key = SecretStr(raw)
    try:
        settings.ensure_ready()
        app = create_knowledge_facade_app(
            build_container(settings), audience=audience, api_key=api_key
        )
    except (ConfigurationError, BackendNotImplementedError) as exc:
        print(json.dumps({"code": exc.code, "message": exc.safe_message}, ensure_ascii=False))
        return 2
    print(
        json.dumps(
            {
                "service": "knowledge-facade",
                "host": args.host,
                "port": args.port,
                "audience": audience.value,
                "authenticated": api_key is not None,
                "contract": "RFA_module contracts/knowledge.openapi.yaml",
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    uvicorn.run(app, host=args.host, port=args.port, reload=False, proxy_headers=False)
    return 0


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    if args.command == "evaluate-compare":
        if args.env_file is not None:
            _reject("evaluation_configuration_rejected")
        _evaluate_compare(args)
    if args.command == "evaluate" and args.dataset == "persona-regression-v2":
        # Dispatch BEFORE Settings(), env-file inspection or provider construction.
        if args.env_file is not None:
            _reject("evaluation_configuration_rejected")
        _evaluate_regression(args)
    if args.command == "evaluate":
        # Dispatch BEFORE Settings(), env-file inspection or provider construction.
        if (
            args.env_file is not None
            or args.dataset != "persona-core-v1"
            or args.judge not in {"disabled", "mock"}
            or args.output is not None
            or args.label != "run"
            or args.mode != "simulated"
            or args.allow_actual
        ):
            print(json.dumps({"code": "evaluation_configuration_rejected"}))
            raise SystemExit(2)
        from rfa_mas.application.evaluation import run_persona_evaluation

        try:
            report = asyncio.run(run_persona_evaluation(judge_mode=args.judge))
        except Exception:
            print(json.dumps({"code": "evaluation_error", "product_final_gate": "not_run"}))
            raise SystemExit(2) from None
        print(report.model_dump_json(indent=2))
        raise SystemExit(report.exit_code)
    if args.command == "init-env":
        result = initialize_dev_env(target_name=args.output)
        print(
            json.dumps(
                asdict(result)
                | {
                    "advisory": "Local credentials generated; service registration is not verified."
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return
    if args.command == "demo" and args.full:
        # Fixed offline composition on its own data dir: dispatched BEFORE Settings() so no
        # .env or --env-file is read; errors print codes only, never inputs or content.
        if args.env_file is not None or args.query is not None:
            print(json.dumps({"code": "demo_configuration_rejected"}))
            raise SystemExit(2)
        args.materials = args.materials or DEMO_MATERIALS
        try:
            raise SystemExit(asyncio.run(_demo_full(args)))
        except RfaError as exc:
            print(json.dumps({"code": exc.code, "message": exc.safe_message}, ensure_ascii=False))
            raise SystemExit(2) from None
        except (OSError, ValueError, KeyError, TypeError, ValidationError) as exc:
            print(json.dumps({"code": "demo_failed", "error": type(exc).__name__}))
            raise SystemExit(2) from None
    if args.env_file is not None and (args.env_file.is_symlink() or not args.env_file.is_file()):
        parser.error("--env-file must point to an existing regular file")
    try:
        settings = Settings() if args.env_file is None else Settings(_env_file=args.env_file)
    except (ValidationError, SettingsError):
        # Pydantic errors can contain raw inputs; never print them or their chained cause.
        print(json.dumps({"code": "configuration_error", "message": "Invalid settings input"}))
        raise SystemExit(2) from None
    if args.command == "api":
        settings.ensure_ready()
        uvicorn.run(
            create_app(settings),
            host=settings.app_host,
            port=settings.app_port,
            reload=False,
        )
        return
    if args.command == "knowledge-facade":
        raise SystemExit(_knowledge_facade(args, settings))
    if args.command == "doctor":
        raise SystemExit(_doctor(settings))
    if args.command == "langfuse-retention":
        report = asyncio.run(run_langfuse_retention(settings, dry_run=args.dry_run))
        print(json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True))
        exit_codes = {"completed": 0, "partial": 1, "failed": 1, "not_attempted": 2}
        raise SystemExit(exit_codes[report.status])
    if args.command == "scheduler":
        try:
            raise SystemExit(asyncio.run(_scheduler(args, settings)))
        except (ConfigurationError, BackendNotImplementedError) as exc:
            print(json.dumps({"code": exc.code, "message": exc.safe_message}, ensure_ascii=False))
            raise SystemExit(2) from None
    if args.command == "demo":
        try:
            raise SystemExit(asyncio.run(_demo(args, settings)))
        except (ConfigurationError, BackendNotImplementedError) as exc:
            print(json.dumps({"code": exc.code, "message": exc.safe_message}, ensure_ascii=False))
            raise SystemExit(2) from None
    if args.command == "openapi":
        settings.ensure_ready()
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        if args.app == "knowledge-facade":
            document = create_knowledge_facade_app(build_container(settings)).openapi()
        else:
            document = create_app(settings).openapi()
        path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(path)


if __name__ == "__main__":
    main()
