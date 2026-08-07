from pathlib import Path

import pytest

from app.domain.events import LearningEvent, LearningEventType
from app.domain.sessions import IllegalTransitionError, SessionState
from app.infrastructure import migration_runner
from app.infrastructure.learner_store import LearnerStore


@pytest.fixture()
def learner(tmp_path: Path) -> tuple[LearnerStore, str]:
    db = tmp_path / "misconceptions.db"
    migration_runner.apply_migrations(db)
    store = LearnerStore(db)
    student_id, _ = store.create_student("Ari", 20, 1200)
    session_id = "ses-mis"
    store.create_session(student_id, session_id)
    for state in [
        SessionState.PROFILE_READY,
        SessionState.DIAGNOSTIC_ACTIVE,
        SessionState.DIAGNOSTIC_COMPLETE,
        SessionState.PLAN_READY,
        SessionState.QUESTION_ACTIVE,
    ]:
        store.transition_session(session_id, state)
    return store, student_id


def make_eval_event(student_id: str, session_id: str, event_id: str) -> LearningEvent:
    return LearningEvent(
        event_id=event_id,
        student_id=student_id,
        session_id=session_id,
        event_type=LearningEventType.ANSWER_EVALUATED,
        payload={"correct": False},
        occurred_at="2026-08-06T10:00:00+00:00",
        received_at="2026-08-06T10:00:00+00:00",
    ).with_integrity()


def _wrong_answer(
    learner: LearnerStore,
    student_id: str,
    session_id: str,
    event_id: str,
    item_id: str,
) -> None:
    learner.record_answer_evaluation(
        student_id=student_id,
        session_id=session_id,
        event=make_eval_event(student_id, session_id, event_id),
        content_id=item_id,
        content_version=1,
        skill="linear_equations",
        subskill="sign_handling",
        difficulty=2,
        sequence=1,
        selected_choice_id="A",
        correct=False,
        hint_level=0,
        weight=1.0,
        validity="valid",
        misconception="sign_error",
        misconception_source_label="distractor_mapping",
        misconception_confidence_label="high",
        session_state=SessionState.ANSWER_EVALUATED,
    )
    learner.transition_session(session_id, SessionState.QUESTION_ACTIVE)


def test_misconception_evidence_progression(learner: tuple[LearnerStore, str]) -> None:
    store, student_id = learner
    session = "ses-mis"

    _wrong_answer(store, student_id, session, "evt-1", "item-a")
    total, distinct = store.count_misconception_evidence(
        student_id, "linear_equations", "sign_error"
    )
    assert (total, distinct) == (1, 1)

    _wrong_answer(store, student_id, session, "evt-2", "item-b")
    total, distinct = store.count_misconception_evidence(
        student_id, "linear_equations", "sign_error"
    )
    assert (total, distinct) == (2, 2)

    _wrong_answer(store, student_id, session, "evt-3", "item-c")
    total, distinct = store.count_misconception_evidence(
        student_id, "linear_equations", "sign_error"
    )
    assert (total, distinct) == (3, 3)


def test_illegal_transition_does_not_write_projection(learner: tuple[LearnerStore, str]) -> None:
    store, student_id = learner
    session = "ses-mis"
    state_before = store.get_session_state(session)
    assert state_before == SessionState.QUESTION_ACTIVE

    with pytest.raises(IllegalTransitionError):
        store.transition_session(session, SessionState.NEW)

    assert store.get_session_state(session) == SessionState.QUESTION_ACTIVE
    state = store.get_skill_state(student_id, "linear_equations")
    assert state is None or state.evidence_count == 0
