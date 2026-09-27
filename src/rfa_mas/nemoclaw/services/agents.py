from __future__ import annotations

import hashlib
from collections.abc import Callable

from rfa_mas.nemoclaw.config import Assignments

PALETTE = ("#2563eb", "#16a34a", "#d97706", "#dc2626", "#7c3aed", "#0891b2", "#be185d", "#4d7c0f",
           "#b45309", "#1d4ed8", "#0f766e", "#9333ea")


def color_for(agent_id: str) -> str:
    digest = hashlib.sha1(agent_id.encode()).digest()[0]
    return PALETTE[digest % len(PALETTE)]


def display_name(agent_id: str, description: str) -> str:
    """Korean role name from the description's first clause, else the id."""
    head = (description or "").split(".")[0].split("(")[0].strip()
    return head[:40] if head else agent_id


class AgentService:
    def __init__(self, assignments: Callable[[], Assignments]):
        self._assignments = assignments

    def list(self, *, include_assistant: bool = True) -> list[dict]:
        a = self._assignments()
        placement = a.placement()
        out = []
        for agent_id, spec in a.agents.items():
            if spec.kind == "task" and not spec.delegatable:
                continue  # the censor is never a conversation partner
            if spec.kind == "fixed" and not include_assistant:
                continue
            kind = "assistant" if spec.kind == "fixed" else ("supervisor" if getattr(spec, "team", None) and
                                                              agent_id.endswith("-sup") else "task")
            out.append({"id": agent_id, "name": display_name(agent_id, spec.description), "color": color_for(agent_id),
                        "sandbox": placement.get(agent_id), "description": spec.description or "", "kind": kind,
                        "groups": list(spec.groups or []), "alias": spec.alias})
        return out

    def get(self, agent_id: str) -> dict | None:
        return next((x for x in self.list() if x["id"] == agent_id), None)
