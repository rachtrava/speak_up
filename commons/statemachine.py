"""Task lifecycle. The only place task states and legal transitions are defined."""
from __future__ import annotations

from .errors import InvalidTransition

CREATED = "created"
AWAITING_AUTHORIZATION = "awaiting_authorization"
AUTHORIZED = "authorized"
QUEUED = "queued"
DELIVERED = "delivered"          # placed in the assignee's Commons inbox (not Telegram, not read)
RETRIEVED = "retrieved"          # the assignee agent actually fetched it through its own connection
IN_PROGRESS = "in_progress"
AWAITING_HUMAN_INPUT = "awaiting_human_input"
RESULT_READY = "result_ready"
ACCEPTED = "accepted"
FAILED = "failed"
CANCELLED = "cancelled"
EXPIRED = "expired"

TERMINAL = {ACCEPTED, FAILED, CANCELLED, EXPIRED}

TRANSITIONS: dict[str, set[str]] = {
    CREATED: {AWAITING_AUTHORIZATION, AUTHORIZED, CANCELLED, FAILED},
    AWAITING_AUTHORIZATION: {AUTHORIZED, CANCELLED, EXPIRED},
    AUTHORIZED: {QUEUED, CANCELLED, FAILED},
    QUEUED: {DELIVERED, CANCELLED, EXPIRED, FAILED},
    DELIVERED: {RETRIEVED, CANCELLED, EXPIRED, FAILED},
    RETRIEVED: {IN_PROGRESS, RESULT_READY, AWAITING_HUMAN_INPUT, CANCELLED, EXPIRED, FAILED},
    IN_PROGRESS: {RESULT_READY, AWAITING_HUMAN_INPUT, CANCELLED, EXPIRED, FAILED},
    AWAITING_HUMAN_INPUT: {IN_PROGRESS, CANCELLED, EXPIRED},
    # Revision by the human sends a result back to in_progress.
    RESULT_READY: {ACCEPTED, IN_PROGRESS, CANCELLED, EXPIRED},
    ACCEPTED: set(),
    FAILED: set(),
    CANCELLED: set(),
    EXPIRED: set(),
}

ALL_STATES = list(TRANSITIONS)

# Plain-language descriptions for people, shown in the UI and Telegram.
PLAIN = {
    CREATED: "Just created",
    AWAITING_AUTHORIZATION: "Waiting for you to approve it",
    AUTHORIZED: "Approved",
    QUEUED: "Approved and lined up",
    DELIVERED: "In the assistant's inbox, not opened yet",
    RETRIEVED: "The assistant has opened it",
    IN_PROGRESS: "The assistant is working on it",
    AWAITING_HUMAN_INPUT: "Paused: an assistant needs your input",
    RESULT_READY: "A result is ready for your review",
    ACCEPTED: "Done: you accepted the result",
    FAILED: "Stopped because something went wrong",
    CANCELLED: "Cancelled",
    EXPIRED: "Expired before it was finished",
}


def check(from_state: str, to_state: str) -> None:
    if to_state not in TRANSITIONS.get(from_state, set()):
        raise InvalidTransition(
            f"A task cannot move from '{from_state}' to '{to_state}'.",
            from_state=from_state,
            to_state=to_state,
        )


def is_terminal(state: str) -> bool:
    return state in TERMINAL
