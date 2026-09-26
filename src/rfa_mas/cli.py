from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

import uvicorn

from rfa_mas.api.app import create_app
from rfa_mas.bootstrap import build_container
from rfa_mas.contracts import (
    Audience,
    DomainId,
    DraftTarget,
    SimulationScenario,
    TrustedPrincipal,
    WorkRequest,
)
from rfa_mas.dev_env import initialize_dev_env
from rfa_mas.errors import BackendNotImplementedError, ConfigurationError
from rfa_mas.settings import Settings


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


def _doctor(settings: Settings) -> int:
    missing = settings.missing_for_selected_modes()
    reserved: list[str] = []
    if settings.model_provider != "mock":
        reserved.append(f"model:{settings.model_provider}")
    if settings.retriever_backend != "mock":
        reserved.append(f"retriever:{settings.retriever_backend}")
    if settings.trace_backend != "local":
        reserved.append(f"trace:{settings.trace_backend}")
    if settings.enable_judge and settings.judge_provider != "mock":
        reserved.append(f"judge:{settings.judge_provider}")
    reserved.extend(settings.selected_reserved_features())
    payload = {
        "ready": not missing and not reserved,
        "selected_modes": {
            "model": settings.model_provider,
            "retriever": settings.retriever_backend,
            "response": settings.response_backend,
            "tool": settings.tool_backend,
            "runtime": settings.runtime_backend,
            "policy": settings.policy_backend,
            "trace": settings.trace_backend,
            "judge": settings.judge_provider if settings.enable_judge else "disabled",
        },
        "variables": [item.model_dump(mode="json") for item in settings.doctor_statuses()],
        "missing": missing,
        "reserved_not_implemented": reserved,
        "external_writes_requested": settings.allow_external_writes,
        "external_writes_effective": settings.external_writes_effective,
        "local_identity_is_production_auth": False,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload["ready"] else 1


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    if args.command == "init-env":
        result = initialize_dev_env(target_name=args.output)
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2, sort_keys=True))
        return
    if args.env_file is not None and (args.env_file.is_symlink() or not args.env_file.is_file()):
        parser.error("--env-file must point to an existing regular file")
    settings = Settings() if args.env_file is None else Settings(_env_file=args.env_file)
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
