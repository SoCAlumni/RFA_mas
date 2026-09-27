"""Display metadata for the chat roster and the task list (``deploy/nemoclaw/frontend.yaml``): names,
icons, colours, desks and suggested questions. Nothing here routes or places anything."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from rfa_mas.nemoclaw.config import DEPLOY_DIR

Icon = Literal["star", "github", "research", "slack", "mail", "shield", "generic"]


class Color(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bg: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    fg: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")


class TaskLook(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_name: str | None = None
    desk: str | None = None
    icon: Icon = "generic"
    color: Color | None = None
    description: str = ""
    tags: list[str] = []
    suggestions: list[str] = []


class AssistantLook(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "비서 에이전트"
    icon: Icon = "star"
    color: Color = Color(bg="#ffffff", fg="#1a1d23")
    description: str = "질문에 맞는 담당자를 찾아 확인한 뒤 답합니다."
    suggestions: list[str] = []


class Owner(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "소유자"


class Presentation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    owner: Owner = Owner()
    assistant: AssistantLook = AssistantLook()
    tasks: dict[str, TaskLook] = {}
    palette: list[Color] = Field(default_factory=lambda: [Color(bg="#dfe3ea", fg="#2b3140")], min_length=1)


def presentation_path() -> Path:
    return Path(os.environ.get("RFA_FE_PRESENTATION") or DEPLOY_DIR / "frontend.yaml")


def load_presentation(path: Path | None = None) -> Presentation:
    path = path or presentation_path()
    if not path.exists():
        return Presentation()
    return Presentation.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


_LATIN = re.compile(r"[A-Za-z0-9]+")


def default_agent_name(task_name: str) -> str:
    return f"{task_name.strip()} 대응 에이전트"


def default_description(task_name: str) -> str:
    return f"{task_name.strip()} 태스크를 맡은 에이전트입니다."


def desk_for(agent_name: str, fallback: str) -> str:
    """UI rule: first latin/digit token of the agent name, lower-cased, + ``-desk``."""
    m = _LATIN.search(agent_name)
    return f"{m.group(0).lower()}-desk" if m else f"{fallback}-desk"


def initials_for(agent_name: str) -> str:
    """UI rule: drop a trailing 에이전트; two latin words → their initials, one → its first two letters,
    no latin → the first two characters."""
    base = re.sub(r"\s*에이전트\s*$", "", agent_name.strip()) or agent_name.strip()
    words = _LATIN.findall(base)
    if words:
        letters = "".join(w[0] for w in words[:2])
        if len(letters) < 2:
            letters += words[0][1:2]
        return letters.upper()[:2]
    return base.replace(" ", "")[:2]


def default_suggestions(task_name: str, tags: list[str]) -> list[str]:
    second = f"{tags[0]} 관련 문의가 있었어?" if tags else f"{task_name}에서 결재가 필요한 안건 있어?"
    return [f"{task_name} 관련해서 지금 들어온 요청 정리해줘", second, "이 에이전트가 맡은 안건 중 결재가 필요한 게 있어?"]


def clean_tags(tags: list[str], limit: int = 8) -> list[str]:
    """Split on commas, trim, strip a leading ``#``, drop empties and duplicates, keep the first ``limit``."""
    out: list[str] = []
    for raw in tags:
        for part in str(raw).split(","):
            tag = part.strip().lstrip("#").strip()[:30]
            if tag and tag not in out:
                out.append(tag)
    return out[:limit]


__all__ = ["Color", "Presentation", "TaskLook", "load_presentation", "default_agent_name", "default_description",
           "desk_for", "initials_for", "default_suggestions", "clean_tags"]
