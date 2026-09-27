"""Learned censor rules: rejection reasons fed back from human review (``feedback[].reason``).

``deploy/nemoclaw/censor-rules/learned.yaml`` is a host file. Each entry is
``{audience, task, reason, at, request_id}``; ``reason`` is trusted operator input, never the draft
text. The reasons for an audience/task are handed to the censor LLM stage as hints and to the head
prompt as "previous rejections" — the sandbox never mounts the file, it travels in the request.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class LearnedRule:
    audience: str
    task: str | None
    reason: str
    at: str
    request_id: str | None = None

    def to_dict(self) -> dict:
        return {"audience": self.audience, "task": self.task, "reason": self.reason, "at": self.at,
                "request_id": self.request_id}


class LearnedRules:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    # ---- storage ----------------------------------------------------------------------------

    def load(self) -> list[LearnedRule]:
        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            return []
        except yaml.YAMLError:
            return []
        out = []
        for item in data.get("learned") or []:
            if not isinstance(item, dict) or not str(item.get("reason", "")).strip():
                continue
            out.append(LearnedRule(str(item.get("audience", "public")), item.get("task") or None,
                                   str(item["reason"]).strip(), str(item.get("at", "")), item.get("request_id")))
        return out

    def _save(self, rules: list[LearnedRule]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        header = ("# 되먹임된 거절 사유. `/ask` 가 feedback[].reason 을 {audience, task, reason, at} 로 누적한다(사람 입력이므로 신뢰).\n"
                  "# censor LLM 단계 프롬프트와 head 프롬프트에 같은 audience·task 의 사유가 주입된다. 손으로 편집해도 된다.\n")
        body = yaml.safe_dump({"version": 1, "learned": [r.to_dict() for r in rules]}, allow_unicode=True, sort_keys=False)
        self.path.write_text(header + body, encoding="utf-8")

    # ---- API --------------------------------------------------------------------------------

    def add(self, audience: str, task: str | None, reasons: list[str], request_id: str | None = None) -> int:
        """Append new (audience, task, reason) triples; returns how many were new."""
        added = 0
        with self._lock:
            rules = self.load()
            seen = {(r.audience, r.task, r.reason) for r in rules}
            now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            for reason in reasons:
                reason = " ".join(str(reason).split())[:500]
                if not reason or (audience, task, reason) in seen:
                    continue
                rules.append(LearnedRule(audience, task, reason, now, request_id))
                seen.add((audience, task, reason))
                added += 1
            if added:
                self._save(rules)
        return added

    def reasons(self, audience: str, task: str | None = None, *, any_task: bool = False) -> list[str]:
        """Reasons for this audience: task-specific ones first, then audience-wide ones (task null).
        ``any_task`` (for the head, which does not know the task yet) returns every reason of the audience."""
        rules = self.load()
        specific = [r.reason for r in rules if r.audience == audience and r.task is not None and (any_task or r.task == task)]
        generic = [r.reason for r in rules if r.audience == audience and r.task is None]
        out: list[str] = []
        for reason in specific + generic:
            if reason not in out:
                out.append(reason)
        return out


__all__ = ["LearnedRule", "LearnedRules"]
