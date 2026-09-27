"""Export generated OpenAPI documents (JSON + YAML) for teammate-facing contracts.

Usage: .venv/bin/python scripts/export_openapi.py [inbox|knowledge-facade|ask|all] [--out docs/api]

Pydantic/FastAPI are the source of truth; the files under docs/api are derived and must
not be edited by hand. YAML is written for RFA_module-style Redoc pages
(scripts/build_api_docs.sh there consumes contracts/*.openapi.yaml).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _inbox():
    from rfa_mas.inbox.reference import create_inbox_reference_app

    return create_inbox_reference_app().openapi()


def _knowledge_facade():
    import tempfile

    from rfa_mas.knowledge_facade.app import create_knowledge_facade_app

    from rfa_mas.bootstrap import build_container
    from rfa_mas.settings import Settings

    tmp = Path(tempfile.mkdtemp(prefix="rfa-openapi-"))
    settings = Settings(
        _env_file=None, database_url=f"sqlite:///{tmp / 'rfa.db'}", trace_dir=tmp / "traces"
    )
    return create_knowledge_facade_app(build_container(settings)).openapi()


def _entry_app():
    import tempfile

    from rfa_mas.nemoclaw.ask import build_fake_deps
    from rfa_mas.nemoclaw.ask_api import AskService, create_ask_app
    from rfa_mas.nemoclaw.config import load_ask, load_assignments, load_censors, load_roles, load_routing
    from rfa_mas.nemoclaw.services import build_frontend_services
    from rfa_mas.nemoclaw.store import Store
    from rfa_mas.nemoclaw.teams import KeywordPatterner, TeamService

    tmp = Path(tempfile.mkdtemp(prefix="rfa-openapi-"))
    ask_cfg = load_ask()
    deps = build_fake_deps(ask_cfg, load_censors(), tmp / "learned.yaml")
    teams = TeamService(roles=load_roles(), routing=load_routing(), ask_cfg=ask_cfg, patterner=KeywordPatterner(),
                        teams_path=tmp / "teams.yaml", fake=True)
    assignments = load_assignments(teams_path=tmp / "no-teams.yaml")  # static declaration only
    frontend = build_frontend_services(assignments=lambda: assignments, ask_deps=deps, token=None,
                                       store=Store(tmp / "frontend.db"))
    return create_ask_app(AskService(deps, None), teams, frontend)


def _ask():
    """desk ↔ knowledge server contract: /ask and /teams only (docs/api/ask.openapi.*)."""
    doc = _entry_app().openapi()
    doc["paths"] = {p: v for p, v in doc["paths"].items() if p == "/ask" or p.startswith("/teams")}
    return doc


def _frontend():
    """The whole entry app for the front-end (docs/openapi.yaml)."""
    return _entry_app().openapi()


EXPORTS: dict[str, Callable[[], dict]] = {
    "inbox": _inbox,
    "knowledge-facade": _knowledge_facade,
    "ask": _ask,
    "frontend": _frontend,
}


def write(name: str, document: dict, out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / f"{name}.openapi.json"
    yaml_path = out / f"{name}.openapi.yaml"
    json_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", "utf-8")
    yaml_path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False, width=100), "utf-8"
    )
    return [json_path, yaml_path]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("target", nargs="?", default="all", choices=[*EXPORTS, "all"])
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "api")
    args = parser.parse_args()
    names = list(EXPORTS) if args.target == "all" else [args.target]
    for name in names:
        try:
            document = EXPORTS[name]()
        except ImportError as exc:
            print(f"{name}: skipped ({exc.name} not available in this checkout)")
            continue
        if name == "frontend":  # front-end contract lives at docs/openapi.{yaml,json}
            out = args.out.parent if args.out.name == "api" else args.out
            out.mkdir(parents=True, exist_ok=True)
            (out / "openapi.json").write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", "utf-8")
            (out / "openapi.yaml").write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False, width=100), "utf-8")
            print(f"{(out / 'openapi.json').relative_to(ROOT)}\n{(out / 'openapi.yaml').relative_to(ROOT)}")
            continue
        for path in write(name, document, args.out):
            print(path.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
