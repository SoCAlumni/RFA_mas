from __future__ import annotations

from rfa_mas.contracts import PublicationStatus, WorkStatus
from rfa_mas.errors import InvalidStateTransitionError

ALLOWED_TRANSITIONS: dict[WorkStatus, frozenset[WorkStatus]] = {
    WorkStatus.CREATED: frozenset({WorkStatus.RUNNING, WorkStatus.CANCELLED}),
    WorkStatus.RUNNING: frozenset(
        {
            WorkStatus.WAITING_APPROVAL,
            WorkStatus.COMPLETED,
            WorkStatus.FAILED,
            WorkStatus.CANCELLED,
            WorkStatus.OUTCOME_UNKNOWN,
        }
    ),
    WorkStatus.WAITING_APPROVAL: frozenset(
        {WorkStatus.COMPLETED, WorkStatus.FAILED, WorkStatus.CANCELLED}
    ),
    WorkStatus.COMPLETED: frozenset(),
    WorkStatus.FAILED: frozenset(),
    WorkStatus.CANCELLED: frozenset(),
    WorkStatus.OUTCOME_UNKNOWN: frozenset(),
}


def ensure_transition(current: WorkStatus, requested: WorkStatus) -> None:
    if requested not in ALLOWED_TRANSITIONS[current]:
        raise InvalidStateTransitionError(current.value, requested.value)


# P1-005A: publication state is separate from the Run lifecycle. A completed Run can
# carry any publication state. OUTCOME_UNKNOWN is resolved only by a result query
# (never by republishing); SUCCEEDED/FAILED are terminal for that publication.
PUBLICATION_TRANSITIONS: dict[PublicationStatus, frozenset[PublicationStatus]] = {
    PublicationStatus.NOT_REQUESTED: frozenset({PublicationStatus.PENDING}),
    PublicationStatus.PENDING: frozenset(
        {
            PublicationStatus.SUCCEEDED,
            PublicationStatus.FAILED,
            PublicationStatus.OUTCOME_UNKNOWN,
        }
    ),
    PublicationStatus.OUTCOME_UNKNOWN: frozenset(
        {PublicationStatus.SUCCEEDED, PublicationStatus.FAILED}
    ),
    PublicationStatus.SUCCEEDED: frozenset(),
    PublicationStatus.FAILED: frozenset(),
}


def ensure_publication_transition(
    current: PublicationStatus, requested: PublicationStatus
) -> None:
    if requested not in PUBLICATION_TRANSITIONS[current]:
        raise InvalidStateTransitionError(current.value, requested.value)
