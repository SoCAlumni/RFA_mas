from __future__ import annotations

import pytest

from rfa_mas.application.state_machine import (
    ALLOWED_TRANSITIONS,
    PUBLICATION_TRANSITIONS,
    ensure_publication_transition,
    ensure_transition,
)
from rfa_mas.contracts import PublicationStatus, WorkStatus
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


# P1-005A: publication lifecycle is separate from the Run lifecycle.
P = PublicationStatus
EXPECTED_PUBLICATION = {
    P.NOT_REQUESTED: frozenset({P.PENDING}),
    P.PENDING: frozenset({P.SUCCEEDED, P.FAILED, P.OUTCOME_UNKNOWN}),
    P.OUTCOME_UNKNOWN: frozenset({P.SUCCEEDED, P.FAILED}),
    P.SUCCEEDED: frozenset(),
    P.FAILED: frozenset(),
}


def test_publication_table_is_separate_and_never_republishes() -> None:
    assert PUBLICATION_TRANSITIONS == EXPECTED_PUBLICATION
    # Unknown outcomes are reconciled by query, never by returning to pending/sending.
    assert P.PENDING not in PUBLICATION_TRANSITIONS[P.OUTCOME_UNKNOWN]
    # No state can reach SUCCEEDED without first recording a durable PENDING intent.
    assert P.SUCCEEDED not in PUBLICATION_TRANSITIONS[P.NOT_REQUESTED]
    assert set(PUBLICATION_TRANSITIONS) == set(PublicationStatus)


@pytest.mark.parametrize(
    ("current", "requested"),
    [
        (c, r)
        for c in PublicationStatus
        for r in PublicationStatus
        if r not in EXPECTED_PUBLICATION[c]
    ],
)
def test_invalid_publication_transition_is_rejected(current, requested) -> None:
    with pytest.raises(InvalidStateTransitionError):
        ensure_publication_transition(current, requested)


@pytest.mark.parametrize(
    ("current", "requested"),
    [(c, r) for c, targets in EXPECTED_PUBLICATION.items() for r in targets],
)
def test_valid_publication_transition_is_accepted(current, requested) -> None:
    ensure_publication_transition(current, requested)
