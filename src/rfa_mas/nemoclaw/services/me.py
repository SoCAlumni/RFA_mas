from __future__ import annotations

import hmac


class MeService:
    """Who is calling: a valid ``RFA_ASK_TOKEN`` bearer is the owner/admin, anyone else is a guest.
    (``/chat`` still takes ``role`` in its body; a guest bearer cannot claim owner there — see chat.)"""

    def __init__(self, token: str | None):
        self.token = token

    def authenticated(self, authorization: str | None) -> bool:
        if not self.token or not authorization or not authorization.lower().startswith("bearer "):
            return False
        return hmac.compare_digest(authorization[7:].strip(), self.token)

    def resolve(self, authorization: str | None) -> dict:
        ok = self.authenticated(authorization)
        return {"authenticated": ok, "role": "owner" if ok else "guest", "isAdmin": ok}

    def clamp_role(self, requested: str, authorization: str | None) -> str:
        """The body may ask for owner; only an authenticated caller gets it."""
        if requested == "owner" and not self.authenticated(authorization):
            return "guest"
        return requested
