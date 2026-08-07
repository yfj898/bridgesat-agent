from pathlib import Path

import pytest

from app.domain.events import (
    AgentEvent,
    LearningEvent,
    LearningEventType,
    compute_integrity_hash,
)
from app.domain.sessions import SessionState
from app.infrastructure import migration_runner
from app.infrastructure.event_store import DuplicateEventError, EventStore
from app.infrastructure.learner_store import DuplicateEventIdError, LearnerStore

POLICY_VERSION = "policy-0.1.0"


@pytest.fixture()
def store(tmp_path: Path) -> tuple[EventStore, str]:
    db = tmp_path / "events.db"
    migration_runner.apply_migrations(db)
    learner = LearnerStore(db)
    student_id, _ = learner.create_student("Ari", 20, 1200)
    return EventStore(db), student_id


def make_event(
    session_id: str = "ses-1",
    student_id: str = "stu-1",
    event_type: str = "ANSWER_SUBMITTED",
) -> LearningEvent:
    return LearningEvent(
        event_id="evt-abc-123",
        student_id=student_id,
        session_id=session_id,
        event_type=event_type,
        payload={"content_id": "q1", "selected_choice_id": "A"},
        policy_version=POLICY_VERSION,
        content_version="q1.v1",
        occurred_at="2026-08-06T10:00:00+00:00",
        received_at="2026-08-06T10:00:00+00:00",
        device_id="device-1",
        device_sequence=1,
        origin="online",
        integrity_hash=compute_integrity_hash(
            event_type, {"content_id": "q1", "selected_choice_id": "A"}
        ),
    )


def test_append_and_read_learning_event(store: tuple[EventStore, str]) -> None:
    event_store, student_id = store
    event = make_event(student_id=student_id)
    assert event_store.append_learning_event(event) is True
    events = event_store.get_learning_events(session_id="ses-1")
    assert len(events) == 1
    assert events[0].event_id == "evt-abc-123"
    assert events[0].integrity_hash == event.integrity_hash


def test_duplicate_event_is_idempotent(store: tuple[EventStore, str]) -> None:
    event_store, student_id = store
    event = make_event(student_id=student_id)
    assert event_store.append_learning_event(event) is True
    assert event_store.append_learning_event(event) is False
    events = event_store.get_learning_events(session_id="ses-1")
    assert len(events) == 1
    assert event_store.learning_event_exists("evt-abc-123")

    with pytest.raises(DuplicateEventError):
        event_store.append_learning_event(event, on_duplicate="raise")


def test_duplicate_event_does_not_double_project(store: tuple[EventStore, str], tmp_path: Path) -> None:
    _, student_id = store
    learner = LearnerStore(tmp_path / "events.db")
    session_id = "ses-dupe"
    learner.create_session(student_id, session_id)
    for state in [
        SessionState.PROFILE_READY,
        SessionState.DIAGNOSTIC_ACTIVE,
        SessionState.DIAGNOSTIC_COMPLETE,
        SessionState.PLAN_READY,
        SessionState.QUESTION_ACTIVE,
    ]:
        learner.transition_session(session_id, state)

    eval_event = LearningEvent(
        event_id="evt-eval-1",
        student_id=student_id,
        session_id=session_id,
        event_type=LearningEventType.ANSWER_EVALUATED,
        payload={"correct": False, "selected_choice_id": "B"},
        occurred_at="2026-08-06T10:01:00+00:00",
        received_at="2026-08-06T10:01:00+00:00",
    ).with_integrity()
    learner.record_answer_evaluation(
        student_id=student_id,
        session_id=session_id,
        event=eval_event,
        content_id="q1",
        content_version=1,
        skill="linear_equations",
        subskill="sign_handling",
        difficulty=2,
        sequence=1,
        selected_choice_id="B",
        correct=False,
        hint_level=0,
        weight=1.0,
        validity="valid",
        misconception="sign_error",
        misconception_source_label="distractor_mapping",
        misconception_confidence_label="high",
        session_state="ANSWER_EVALUATED",
    )
    with pytest.raises(DuplicateEventIdError):
        learner.record_answer_evaluation(
            student_id=student_id,
            session_id=session_id,
            event=eval_event,
            content_id="q1",
            content_version=1,
            skill="linear_equations",
            subskill="sign_handling",
            difficulty=2,
            sequence=1,
            selected_choice_id="B",
            correct=False,
            hint_level=0,
            weight=1.0,
            validity="valid",
            misconception="sign_error",
            misconception_source_label="distractor_mapping",
            misconception_confidence_label="high",
            session_state="ANSWER_EVALUATED",
        )
    state = learner.get_skill_state(student_id, "linear_equations")
    assert state.evidence_count == 1
    total, distinct = learner.count_misconception_evidence(
        student_id, "linear_equations", "sign_error"
    )
    assert total == 1 and distinct == 1


def test_agent_event_roundtrip(store: tuple[EventStore, str]) -> None:
    event_store, student_id = store
    agent = AgentEvent(
        event_id="agt-1",
        student_id=student_id,
        session_id="ses-1",
        source_event_id="evt-abc-123",
        state_before="QUESTION_ACTIVE",
        state_after="WORKED_EXAMPLE_ACTIVE",
        action="SHOW_WORKED_EXAMPLE",
        action_payload={"misconception": "sign_error"},
        reason_code="RECALLED_SUCCESSFUL_EPISODE",
        reason_text="Episode recall changed the action.",
        episode_ids=["ep-1"],
        referenced_content=["q1"],
    )
    assert event_store.append_agent_event(agent) is True
    assert event_store.append_agent_event(agent) is False
    events = event_store.get_agent_events(session_id="ses-1")
    assert len(events) == 1
    assert events[0].episode_ids == ["ep-1"]
    assert events[0].reason_code == "RECALLED_SUCCESSFUL_EPISODE"
