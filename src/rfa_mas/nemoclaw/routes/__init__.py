"""HTTP layer only: paths, (de)serialisation, status codes. Renaming a path or a field for the
front-end touches this package and ``schemas/`` — never ``services/``."""

from __future__ import annotations

from fastapi import FastAPI

from rfa_mas.nemoclaw.routes import agents, chat, me, tasks
from rfa_mas.nemoclaw.services import FrontendServices


def mount_frontend(app: FastAPI, services: FrontendServices) -> None:
    app.state.frontend = services
    for module in (agents, me, tasks, *((chat,) if services.chat is not None else ())):
        app.include_router(module.router)


__all__ = ["mount_frontend"]
