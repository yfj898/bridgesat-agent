from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class SessionState(StrEnum):
    NEW = "NEW"
    PROFILE_READY = "PROFILE_READY"
    DIAGNOSTIC_ACTIVE = "DIAGNOSTIC_ACTIVE"
    DIAGNOSTIC_COMPLETE = "DIAGNOSTIC_COMPLETE"
    PLAN_READY = "PLAN_READY"
    QUESTION_ACTIVE = "QUESTION_ACTIVE"
    HINT_ACTIVE = "HINT_ACTIVE"
    ANSWER_EVALUATED = "ANSWER_EVALUATED"
    WORKED_EXAMPLE_ACTIVE = "WORKED_EXAMPLE_ACTIVE"
    MICRO_LESSON_ACTIVE = "MICRO_LESSON_ACTIVE"
    PRACTICE_ADAPTED = "PRACTICE_ADAPTED"
    PAUSED = "PAUSED"
    SESSION_SUMMARY = "SESSION_SUMMARY"
    SESSION_COMPLETED = "SESSION_COMPLETED"


ACTIVE_STATES = {
    SessionState.QUESTION_ACTIVE,
    SessionState.HINT_ACTIVE,
    SessionState.ANSWER_EVALUATED,
    SessionState.WORKED_EXAMPLE_ACTIVE,
    SessionState.MICRO_LESSON_ACTIVE,
    SessionState.PRACTICE_ADAPTED,
}

TRANSITIONS: dict[SessionState, set[SessionState]] = {
    SessionState.NEW: {SessionState.PROFILE_READY},
    SessionState.PROFILE_READY: {SessionState.DIAGNOSTIC_ACTIVE},
    SessionState.DIAGNOSTIC_ACTIVE: {SessionState.DIAGNOSTIC_COMPLETE},
    SessionState.DIAGNOSTIC_COMPLETE: {SessionState.PLAN_READY},
    SessionState.PLAN_READY: {SessionState.QUESTION_ACTIVE},
    SessionState.QUESTION_ACTIVE: {
        SessionState.HINT_ACTIVE,
        SessionState.ANSWER_EVALUATED,
        SessionState.PAUSED,
        SessionState.SESSION_SUMMARY,
    },
    SessionState.HINT_ACTIVE: {
        SessionState.QUESTION_ACTIVE,
        SessionState.ANSWER_EVALUATED,
        SessionState.PAUSED,
        SessionState.SESSION_SUMMARY,
    },
    SessionState.ANSWER_EVALUATED: {
        SessionState.WORKED_EXAMPLE_ACTIVE,
        SessionState.MICRO_LESSON_ACTIVE,
        SessionState.PRACTICE_ADAPTED,
        SessionState.QUESTION_ACTIVE,
        SessionState.PAUSED,
        SessionState.SESSION_SUMMARY,
    },
    SessionState.WORKED_EXAMPLE_ACTIVE: {
        SessionState.QUESTION_ACTIVE,
        SessionState.PAUSED,
        SessionState.SESSION_SUMMARY,
    },
    SessionState.MICRO_LESSON_ACTIVE: {
        SessionState.QUESTION_ACTIVE,
        SessionState.PAUSED,
        SessionState.SESSION_SUMMARY,
    },
    SessionState.PRACTICE_ADAPTED: {
        SessionState.QUESTION_ACTIVE,
        SessionState.PAUSED,
        SessionState.SESSION_SUMMARY,
    },
    SessionState.PAUSED: {
        SessionState.QUESTION_ACTIVE,
        SessionState.PAUSED,
        SessionState.SESSION_SUMMARY,
    },
    SessionState.SESSION_SUMMARY: {SessionState.SESSION_COMPLETED},
    SessionState.SESSION_COMPLETED: set(),
}

ILLEGAL_TRANSITION_ERROR = "ILLEGAL_STATE_TRANSITION"


class IllegalTransitionError(RuntimeError):
    code = ILLEGAL_TRANSITION_ERROR

    def __init__(self, source: SessionState, target: SessionState) -> None:
        self.source = source
        self.target = target
        super().__init__(
            f"{ILLEGAL_TRANSITION_ERROR}: cannot transition {source} -> {target}"
        )


def can_transition(source: SessionState, target: SessionState) -> bool:
    return target in TRANSITIONS.get(source, set())


def transition(source: SessionState, target: SessionState) -> SessionState:
    if not can_transition(source, target):
        raise IllegalTransitionError(source, target)
    return target


class SessionSnapshot(BaseModel):
    session_id: str
    student_id: str
    state: SessionState
    paused_from_state: SessionState | None = None
    started_at: str
    updated_at: str
    completed_at: str | None = None
    sequence: int = Field(default=0, ge=0)
    current_content_id: str | None = None