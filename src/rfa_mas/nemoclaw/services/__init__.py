"""HTTP-agnostic logic behind ``routes/``. Services take plain values and return plain dicts /
dataclasses; ``routes/`` maps them to the schemas."""

from rfa_mas.nemoclaw.services.context import FrontendServices, build_frontend_services

__all__ = ["FrontendServices", "build_frontend_services"]
