from pathlib import Path

import pytest

from app.domain.sessions import (
    ILLEGAL_TRANSITION_ERROR,
    IllegalTransitionError,
    SessionState,
    can_transition,
    transition,
)


def test_happy_path_state_machine() -> None:
    states = [
        SessionState.NEW,
        SessionState.PROFILE_READY,
        SessionState.DIAGNOSTIC_ACTIVE,
        SessionState.DIAGNOSTIC_COMPLETE,
        SessionState.PLAN_READY,
        SessionState.QUESTION_ACTIVE,
    ]
    current = states[0]
    for target in states[1:]:
        current = transition(current, target)
    assert current == SessionState.QUESTION_ACTIVE


def test_question_active_cycles() -> None:
    q = SessionState.QUESTION_ACTIVE
    assert transition(q, SessionState.HINT_ACTIVE) == SessionState.HINT_ACTIVE
    assert transition(SessionState.HINT_ACTIVE, SessionState.QUESTION_ACTIVE) == q
    assert transition(q, SessionState.ANSWER_EVALUATED) == SessionState.ANSWER_EVALUATED


def test_answer_evaluated_branches() -> None:
    a = SessionState.ANSWER_EVALUATED
    for target in [
        SessionState.WORKED_EXAMPLE_ACTIVE,
        SessionState.MICRO_LESSON_ACTIVE,
        SessionState.PRACTICE_ADAPTED,
        SessionState.QUESTION_ACTIVE,
    ]:
        assert can_transition(a, target)


def test_pause_resume() -> None:
    for active in [
        SessionState.QUESTION_ACTIVE,
        SessionState.HINT_ACTIVE,
        SessionState.ANSWER_EVALUATED,
        SessionState.WORKED_EXAMPLE_ACTIVE,
        SessionState.MICRO_LESSON_ACTIVE,
        SessionState.PRACTICE_ADAPTED,
    ]:
        assert can_transition(active, SessionState.PAUSED)
    assert can_transition(SessionState.PAUSED, SessionState.QUESTION_ACTIVE)
    assert can_transition(SessionState.PAUSED, SessionState.SESSION_SUMMARY)


def test_completion_path() -> None:
    assert can_transition(SessionState.QUESTION_ACTIVE, SessionState.SESSION_SUMMARY)
    assert transition(SessionState.SESSION_SUMMARY, SessionState.SESSION_COMPLETED)
    assert not can_transition(SessionState.SESSION_COMPLETED, SessionState.QUESTION_ACTIVE)


def test_illegal_transition_raises_stable_error() -> None:
    with pytest.raises(IllegalTransitionError) as exc:
        transition(SessionState.NEW, SessionState.SESSION_COMPLETED)
    assert exc.value.code == ILLEGAL_TRANSITION_ERROR
    assert "NEW -> SESSION_COMPLETED" in str(exc.value)

    with pytest.raises(IllegalTransitionError):
        transition(SessionState.SESSION_COMPLETED, SessionState.QUESTION_ACTIVE)
    with pytest.raises(IllegalTransitionError):
        transition(SessionState.PLAN_READY, SessionState.PAUSED)
