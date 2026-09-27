from __future__ import annotations

from fastapi import Request

from rfa_mas.nemoclaw.services import FrontendServices


def services(request: Request) -> FrontendServices:
    return request.app.state.frontend
