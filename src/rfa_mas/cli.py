from __future__ import annotations

import argparse
import asyncio
import json
import re
import signal
import sqlite3
from dataclasses import asdict
from pathlib import Path

import uvicorn
from pydantic import ValidationError
from pydantic_settings import SettingsError

from rfa_mas.api.app import create_app
from rfa_mas.bootstrap import build_container, build_scheduler_runner, inspect_configuration
from rfa_mas.contracts import (
    Audience,
    DomainId,
    DraftTarget,
    SimulationScenario,
    TrustedPrincipal,
    WorkRequest,
)
from rfa_mas.dev_env import initialize_dev_env
from rfa_mas.errors import BackendNotImplementedError, ConfigurationError, RfaError
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

    subparsers.add_parser("doctor", help="Show configured/missing variable names without values")

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
    evaluation.add_argument("--dataset", default="persona-core-v1")
    evaluation.add_argument("--judge", default="disabled", help="disabled or mock only")

    openapi = subparsers.add_parser("openapi", help="Export the generated OpenAPI document")
    openapi.add_argument("--output", default="openapi.json")
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
        print(json.dumps({"status": "scheduler_running", "jobs": len(runner.jobs()),
                          "sync": runner.last_sync}))
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


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    if args.command == "evaluate":
        # Dispatch BEFORE Settings(), env-file inspection or provider construction.
        if (
            args.env_file is not None
            or args.dataset != "persona-core-v1"
            or args.judge not in {"disabled", "mock"}
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
    if args.command == "doctor":
        raise SystemExit(_doctor(settings))
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
        path.write_text(
            json.dumps(create_app(settings).openapi(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(path)


if __name__ == "__main__":
    main()
