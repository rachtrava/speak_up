import pytest

from commons import statemachine as sm
from commons.errors import InvalidTransition


def test_required_states_exist():
    required = {"created", "awaiting_authorization", "authorized", "queued", "delivered", "retrieved",
                "in_progress", "awaiting_human_input", "result_ready", "accepted", "failed", "cancelled", "expired"}
    assert required <= set(sm.ALL_STATES)


def test_terminal_states_have_no_exits():
    for s in sm.TERMINAL:
        assert sm.TRANSITIONS[s] == set()


def test_delivery_is_not_retrieval():
    # A task cannot skip from queued straight to retrieved: inbox delivery must be recorded first.
    with pytest.raises(InvalidTransition):
        sm.check(sm.QUEUED, sm.RETRIEVED)
    # And it cannot produce a result before the assignee retrieved it.
    with pytest.raises(InvalidTransition):
        sm.check(sm.DELIVERED, sm.RESULT_READY)


def test_unauthorized_task_cannot_be_delivered():
    with pytest.raises(InvalidTransition):
        sm.check(sm.AWAITING_AUTHORIZATION, sm.QUEUED)
    with pytest.raises(InvalidTransition):
        sm.check(sm.CREATED, sm.DELIVERED)


def test_every_live_state_can_be_stopped():
    for s, nxt in sm.TRANSITIONS.items():
        if s not in sm.TERMINAL:
            assert nxt & {sm.CANCELLED, sm.FAILED, sm.EXPIRED}, s


def test_every_state_has_plain_language():
    assert set(sm.PLAIN) == set(sm.ALL_STATES)
