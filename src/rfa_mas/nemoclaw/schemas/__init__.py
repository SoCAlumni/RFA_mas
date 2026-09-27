"""Pydantic request/response/SSE schemas for the front-end contract — the single source of
``docs/openapi.yaml`` (``make openapi``). Paths, field names and event names may still change with
the front-end; only ``routes/`` and this package change when they do."""

from rfa_mas.nemoclaw.schemas.common import (
    SSE_ENVELOPE_VERSION,
    AgentRef,
    Me,
    Role,
    SseEnvelope,
    TaskView,
)

__all__ = ["SSE_ENVELOPE_VERSION", "AgentRef", "Me", "Role", "SseEnvelope", "TaskView"]
