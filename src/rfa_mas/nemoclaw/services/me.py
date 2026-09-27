from __future__ import annotations

import hmac
from collections.abc import Callable

from rfa_mas.nemoclaw.services.presentation import Presentation


class MeService:
    """Who is calling: a valid ``RFA_ASK_TOKEN`` bearer is the owner/admin, anyone else is a guest.
    (``/chat`` still takes ``role`` in its body; a guest bearer cannot claim owner there — see chat.)"""

    def __init__(self, token: str | None, presentation: Callable[[], Presentation] | None = None):
        self.token = token
        self._presentation = presentation or Presentation

    def authenticated(self, authorization: str | None) -> bool:
        if not self.token or not authorization or not authorization.lower().startswith("bearer "):
            return False
        return hmac.compare_digest(authorization[7:].strip(), self.token)

    def resolve(self, authorization: str | None) -> dict:
        ok = self.authenticated(authorization)
        return {"authenticated": ok, "role": "owner" if ok else "guest", "isAdmin": ok,
                "name": self._presentation().owner.name if ok else "게스트"}

    def clamp_role(self, requested: str, authorization: str | None) -> str:
        """The body may ask for owner; only an authenticated caller gets it."""
        if requested == "owner" and not self.authenticated(authorization):
            return "guest"
        return requested
