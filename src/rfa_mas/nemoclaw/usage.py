"""Context usage of one LLM call, measured at the egress-proxy (D-19).

Every sandbox LLM call passes the proxy, so the request itself tells how the context is spent:
system prompt, tool definitions, memory & notes (workspace files OpenClaw injects into the system
prompt — the part after its "Project Context" heading) and the conversation (every non-system
message). Token counts are estimated from characters; when the upstream reports
``usage.prompt_tokens`` the parts are scaled so they sum to that measured total."""

from __future__ import annotations

import json
import math
import re

# OpenClaw appends the workspace files (IDENTITY.md, AGENTS.md, notes …) under this heading
_NOTES_HEADING = re.compile(r"^#{1,3}\s*(Project Context|Workspace Files|Memory)\b", re.IGNORECASE | re.MULTILINE)


def estimate_tokens(text: str) -> int:
    """≈ 4 ASCII characters or ≈ 1.4 non-ASCII (Hangul) characters per token."""
    if not text:
        return 0
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    return math.ceil(ascii_chars / 4 + (len(text) - ascii_chars) / 1.4)


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # multi-part content
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def context_breakdown(body: dict) -> dict:
    """Estimated tokens per part of one chat.completions request."""
    system = notes = conversation = 0
    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        text = _text(message.get("content"))
        if message.get("tool_calls"):
            text += json.dumps(message["tool_calls"], ensure_ascii=False)
        if message.get("role") in ("system", "developer"):
            m = _NOTES_HEADING.search(text)
            system += estimate_tokens(text[: m.start()] if m else text)
            notes += estimate_tokens(text[m.start():]) if m else 0
        else:
            conversation += estimate_tokens(text)
    tools = estimate_tokens(json.dumps(body.get("tools") or [], ensure_ascii=False)) if body.get("tools") else 0
    parts = {"systemPrompt": system, "toolDefinitions": tools, "memoryNotes": notes, "conversation": conversation}
    return {**parts, "total": sum(parts.values()), "measured": False}


def scale_to_usage(breakdown: dict, usage: dict | None) -> dict:
    """Scale the estimated parts so they sum to the upstream's ``prompt_tokens`` (when reported)."""
    prompt = (usage or {}).get("prompt_tokens")
    if not isinstance(prompt, int) or prompt <= 0 or not breakdown.get("total"):
        return breakdown
    keys = ("systemPrompt", "toolDefinitions", "memoryNotes", "conversation")
    factor = prompt / breakdown["total"]
    scaled = {k: int(breakdown[k] * factor) for k in keys}
    scaled["conversation"] += prompt - sum(scaled.values())  # rounding remainder
    return {**scaled, "total": prompt, "measured": True}


def usage_summary(usage: dict | None) -> dict | None:
    if not isinstance(usage, dict):
        return None
    keys = ("prompt_tokens", "completion_tokens", "total_tokens")
    out = {k: usage[k] for k in keys if isinstance(usage.get(k), int)}
    return out or None
