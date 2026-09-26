from __future__ import annotations

from rfa_mas.contracts import WorkStatus
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
