from __future__ import annotations

import pytest

from rfa_mas.application.state_machine import ALLOWED_TRANSITIONS, ensure_transition
from rfa_mas.contracts import WorkStatus
from rfa_mas.errors import InvalidStateTransitionError

EXPECTED_TRANSITIONS: dict[WorkStatus, frozenset[WorkStatus]] = {
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

VALID_TRANSITIONS = tuple(
    (current, requested)
    for current, requested_states in EXPECTED_TRANSITIONS.items()
    for requested in requested_states
)
INVALID_TRANSITIONS = tuple(
    (current, requested)
    for current in WorkStatus
    for requested in WorkStatus
    if requested not in EXPECTED_TRANSITIONS[current]
)


def test_transition_table_matches_the_public_lifecycle() -> None:
    assert ALLOWED_TRANSITIONS == EXPECTED_TRANSITIONS


@pytest.mark.parametrize(("current", "requested"), VALID_TRANSITIONS)
def test_valid_transition_is_accepted(current: WorkStatus, requested: WorkStatus) -> None:
    ensure_transition(current, requested)


@pytest.mark.parametrize(("current", "requested"), INVALID_TRANSITIONS)
def test_invalid_transition_is_rejected(current: WorkStatus, requested: WorkStatus) -> None:
    with pytest.raises(InvalidStateTransitionError) as captured:
        ensure_transition(current, requested)

    assert captured.value.code == "invalid_state_transition"
    assert captured.value.retryable is False
    assert f"{current.value} -> {requested.value}" in captured.value.safe_message
