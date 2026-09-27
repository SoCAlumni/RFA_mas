"""Signed in-band routing markers.

The OpenShell gateway rewrites the request ``model`` to the live route model and adds no
sandbox-identifying header (observed 2026-09-27), so the egress-proxy cannot tell channels or
agents apart from transport metadata. Instead the host plants HMAC-signed markers:

- ``⟦rfa-channel v1 ch=external sid=<session> sig=…⟧`` — prepended by the channel API entry point
  to the first user message of a session (it then rides along in the conversation history);
- ``⟦rfa-agent v1 agent=research alias=rfa-external sandbox=… sig=…⟧`` — written into the agent's
  workspace ``IDENTITY.md`` so it appears in the system prompt.

Unsigned or tampered markers are ignored (and reported), so an injected instruction cannot
re-route a session; missing markers fall back to the least-exposed alias.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

OPEN, CLOSE = "⟦", "⟧"  # ⟦ ⟧
_VALUE = r"[A-Za-z0-9._:-]+"
_MARKER = re.compile(
    rf"{OPEN}rfa-(?P<kind>channel|agent) v1 (?P<fields>(?:[a-z_]+={_VALUE} )*)sig=(?P<sig>[0-9a-f]{{40}}){CLOSE}"
)
_FIELD = re.compile(rf"([a-z_]+)=({_VALUE})")


@dataclass(frozen=True)
class Marker:
    kind: str
    fields: dict[str, str]
    verified: bool
    raw: str


def load_or_create_secret(path: Path) -> bytes:
    """Host-only HMAC key (0600). Created once; never printed."""
    path = path.expanduser()
    if path.exists():
        value = path.read_bytes().strip()
        if len(value) < 32:
            raise ValueError("marker_key_invalid")
        return value
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_hex(32).encode()
    path.touch(mode=0o600)
    path.chmod(0o600)
    path.write_bytes(value + b"\n")
    return value


def _payload(kind: str, fields: dict[str, str]) -> str:
    for key, value in fields.items():
        if not re.fullmatch(r"[a-z_]+", key) or not re.fullmatch(_VALUE, value):
            raise ValueError(f"marker field {key}={value!r} contains unsupported characters")
    body = " ".join(f"{k}={fields[k]}" for k in sorted(fields))
    return f"{kind} v1 {body} " if body else f"{kind} v1 "


def _sign(payload: str, secret: bytes) -> str:
    return hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()[:40]


def make_marker(kind: str, fields: dict[str, str], secret: bytes) -> str:
    if kind not in ("channel", "agent"):
        raise ValueError("marker kind must be channel or agent")
    payload = _payload(kind, fields)
    return f"{OPEN}rfa-{payload}sig={_sign(payload, secret)}{CLOSE}"


def find_markers(text: str, secret: bytes) -> list[Marker]:
    """All markers in ``text`` in order; ``verified`` is False for unsigned/tampered ones."""
    out: list[Marker] = []
    for match in _MARKER.finditer(text or ""):
        kind = match.group("kind")
        fields = dict(_FIELD.findall(match.group("fields")))
        payload = _payload(kind, fields)
        verified = hmac.compare_digest(_sign(payload, secret), match.group("sig"))
        out.append(Marker(kind, fields, verified, match.group(0)))
    return out


def strip_markers(text: str) -> str:
    return _MARKER.sub("", text or "").strip()
