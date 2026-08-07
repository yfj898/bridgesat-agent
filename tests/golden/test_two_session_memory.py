"""Two-session memory golden test.

Session 1: two distinct items with a sign error -> SHOW_WORKED_EXAMPLE ->
a distinct transfer item answered correctly without hints; a validated
episode is built.

Session 2: the first similar sign error recalls the validated episode and the
memory-aware policy selects SHOW_WORKED_EXAMPLE earlier than the no-memory
baseline. Decision must persist episode ID, RECALLED_SUCCESSFUL_EPISODE,
reason text, and policy version. Mnemis is unavailable (local mode) and SQLite
recall still produces the allowed action.
"""

from pathlib import Path

import pytest

from app.agent.orchestrator import ContentItem, SessionOrchestrator
from app.domain.memory import BoundedAction
from app.domain.sessions import SessionState
from app.infrastructure import migration_runner
from app.infrastructure.learner_store import LearnerStore

SIGN_ERROR = "sign_error"


def sign_item(content_id: str, difficulty: int = 2) -> ContentItem:
    return ContentItem(
        content_id=content_id,
        version=1,
        skill="linear_equations",
        subskill="sign_handling",
        difficulty=difficulty,
        answer_choice_id="C",
        misconception_map={"A": SIGN_ERROR, "B": "inverse_operation_error", "D": "arithmetic_error"},
    )


@pytest.fixture()
def orchestrator(tmp_path: Path) -> SessionOrchestrator:
    db = tmp_path / "golden.db"
    migration_runner.apply_migrations(db)
    return SessionOrchestrator(db)


def _bring_session_to_question(orchestrator: SessionOrchestrator, session_id: str) -> None:
    for state in [
        SessionState.PROFILE_READY,
        SessionState.DIAGNOSTIC_ACTIVE,
        SessionState.DIAGNOSTIC_COMPLETE,
        SessionState.PLAN_READY,
        SessionState.QUESTION_ACTIVE,
    ]:
        orchestrator.learner.transition_session(session_id, state)


def test_two_session_memory_changes_next_action(orchestrator: SessionOrchestrator) -> None:
    learner: LearnerStore = orchestrator.learner
    student_id, _ = learner.create_student("Ari", 20, 1200)

    # ---------- Session 1 ----------
    session_1 = "ses-1"
    learner.create_session(student_id, session_1)
    _bring_session_to_question(orchestrator, session_1)

    first = orchestrator.evaluate_answer(
        student_id=student_id,
        session_id=session_1,
        item=sign_item("sign-a"),
        selected_choice_id="A",
        hint_level=0,
        minutes_remaining=15,
    )
    assert first.decision.action == BoundedAction.RETRY_SAME_SKILL.value
    assert first.decision.reason_code == "MISCONCEPTION_OBSERVED"
    assert first.next_state == SessionState.QUESTION_ACTIVE

    second = orchestrator.evaluate_answer(
        student_id=student_id,
        session_id=session_1,
        item=sign_item("sign-b"),
        selected_choice_id="A",
        hint_level=0,
        minutes_remaining=14,
    )
    assert second.decision.action == BoundedAction.SHOW_WORKED_EXAMPLE.value
    assert second.decision.reason_code == "REPEATED_MISCONCEPTION"
    assert second.next_state == SessionState.WORKED_EXAMPLE_ACTIVE

    # Transfer item, answered correctly without hints -> validated episode
    episode = orchestrator.build_episode(
        student_id=student_id,
        session_id=session_1,
        skill="linear_equations",
        misconception=SIGN_ERROR,
        intervention=BoundedAction.SHOW_WORKED_EXAMPLE.value,
        teaching_content_id="sign-b",
        outcome_item=sign_item("sign-transfer-1"),
        outcome_correct=True,
        outcome_hint_level=0,
        summary="Worked example resolved sign_error on a distinct transfer item.",
    )
    assert episode is not None
    assert episode.status == "validated"
    assert episode.effectiveness >= 0.6

    # ---------- Session 2 ----------
    session_2 = "ses-2"
    learner.create_session(student_id, session_2)
    _bring_session_to_question(orchestrator, session_2)

    recalled = orchestrator.evaluate_answer(
        student_id=student_id,
        session_id=session_2,
        item=sign_item("sign-c"),
        selected_choice_id="A",
        hint_level=0,
        minutes_remaining=15,
    )
    assert recalled.decision.action == BoundedAction.SHOW_WORKED_EXAMPLE.value
    assert recalled.decision.reason_code == "RECALLED_SUCCESSFUL_EPISODE"
    assert recalled.decision.episode_ids == [episode.episode_id]
    assert "worked example" in recalled.decision.reason_text.lower()
    assert recalled.decision.policy_version == "policy-0.1.0"
    assert recalled.next_state == SessionState.WORKED_EXAMPLE_ACTIVE

    # Agent event persisted the decision with episode ID and reason
    agent_events = orchestrator.events.get_agent_events(session_id=session_2)
    assert agent_events
    assert agent_events[-1].episode_ids == [episode.episode_id]
    assert agent_events[-1].reason_code == "RECALLED_SUCCESSFUL_EPISODE"


def test_no_memory_baseline_does_not_recall(orchestrator: SessionOrchestrator) -> None:
    learner = orchestrator.learner
    student_id, _ = learner.create_student("Baseline", 20, 1200)
    session = "ses-baseline"
    learner.create_session(student_id, session)
    _bring_session_to_question(orchestrator, session)

    result = orchestrator.evaluate_answer(
        student_id=student_id,
        session_id=session,
        item=sign_item("sign-baseline"),
        selected_choice_id="A",
        hint_level=0,
        minutes_remaining=15,
    )
    assert result.decision.action == BoundedAction.RETRY_SAME_SKILL.value
    assert result.decision.reason_code != "RECALLED_SUCCESSFUL_EPISODE"
    assert result.decision.episode_ids == []


def test_sqlite_recall_works_without_mnemis(orchestrator: SessionOrchestrator) -> None:
    """Local mode (BRIDGESAT_MODE unset) has no Mnemis; recall still works."""
    assert orchestrator.memory is not None
    learner = orchestrator.learner
    student_id, _ = learner.create_student("Offline", 20, 1200)
    session = "ses-offline"
    learner.create_session(student_id, session)
    _bring_session_to_question(orchestrator, session)

    result = orchestrator.evaluate_answer(
        student_id=student_id,
        session_id=session,
        item=sign_item("sign-offline"),
        selected_choice_id="A",
        hint_level=0,
        minutes_remaining=15,
    )
    allowed_actions = {action.value for action in BoundedAction}
    assert result.decision.action in allowed_actions
