"""결재 인박스 UI contract (`/v1/inbox`), reference server and RFA_module review mapper."""

from rfa_mas.inbox.reference import InboxReferenceState, create_inbox_reference_app

__all__ = ["InboxReferenceState", "create_inbox_reference_app"]
