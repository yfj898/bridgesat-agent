from pathlib import Path

import pytest

from app.agent.policy import PolicyInput, decide_next_action
from app.domain.memory import BoundedAction
from app.domain.sessions import SessionState


def base_input(**overrides) -> PolicyInput:
    values = dict(
        student_id="stu-1",
        session_id="ses-1",
        skill="linear_equations",
        subskill="sign_handling",
        difficulty=2,
        mastery=0.6,
        confidence=0.3,
    )
    values.update(overrides)
    return PolicyInput(**values)


def test_time_budget_ends_with_review() -> None:
    result = decide_next_action(base_input(minutes_remaining=1))
    assert result.decision.action == BoundedAction.END_WITH_REVIEW.value
    assert result.decision.reason_code == "TIME_BUDGET_EXHAUSTED"
    assert result.next_state == SessionState.SESSION_SUMMARY


def test_two_consecutive_errors_insert_lesson() -> None:
    result = decide_next_action(base_input(consecutive_errors=2))
    assert result.decision.action == BoundedAction.SHOW_MICRO_LESSON.value
    assert result.next_state == SessionState.MICRO_LESSON_ACTIVE


def test_repeated_misconception_shows_worked_example() -> None:
    result = decide_next_action(
        base_input(
            active_misconception="sign_error",
            misconception_observation_count=2,
            misconception_distinct_items=2,
        )
    )
    assert result.decision.action == BoundedAction.SHOW_WORKED_EXAMPLE.value
    assert result.decision.reason_code == "REPEATED_MISCONCEPTION"
    assert result.next_state == SessionState.WORKED_EXAMPLE_ACTIVE


def test_first_misconception_observation_retries_same_skill() -> None:
    result = decide_next_action(
        base_input(active_misconception="sign_error", misconception_observation_count=1)
    )
    assert result.decision.action == BoundedAction.RETRY_SAME_SKILL.value
    assert result.decision.reason_code == "MISCONCEPTION_OBSERVED"


def test_recalled_episode_reuses_worked_example_early() -> None:
    result = decide_next_action(
        base_input(
            active_misconception="sign_error",
            misconception_observation_count=1,
            recalled_successful_episode=True,
            recalled_episode_ids=["ep-1"],
        )
    )
    assert result.decision.action == BoundedAction.SHOW_WORKED_EXAMPLE.value
    assert result.decision.reason_code == "RECALLED_SUCCESSFUL_EPISODE"
    assert result.decision.episode_ids == ["ep-1"]
    assert result.next_state == SessionState.WORKED_EXAMPLE_ACTIVE


def test_no_memory_baseline_does_not_reuse() -> None:
    result = decide_next_action(
        base_input(
            active_misconception="sign_error",
            misconception_observation_count=1,
            recalled_successful_episode=False,
        )
    )
    assert result.decision.action == BoundedAction.RETRY_SAME_SKILL.value
    assert result.decision.reason_code != "RECALLED_SUCCESSFUL_EPISODE"


def test_prerequisite_switch() -> None:
    result = decide_next_action(base_input(requires_unmastered_prerequisite=True))
    assert result.decision.action == BoundedAction.SWITCH_TO_PREREQUISITE.value
    assert result.decision.reason_code == "PREREQUISITE_BLOCKER"


def test_promotion_raises_difficulty() -> None:
    result = decide_next_action(
        base_input(
            mastery=0.8,
            confidence=0.6,
            correct_streak=3,
            recent_correct_without_high_hint=3,
            recent_total=3,
        )
    )
    assert result.decision.action == BoundedAction.RAISE_DIFFICULTY.value
    assert result.decision.action_payload["difficulty"] == 3


def test_default_continue_practice() -> None:
    result = decide_next_action(base_input())
    assert result.decision.action == BoundedAction.RETRY_SAME_SKILL.value
    assert result.decision.reason_code == "CONTINUE_PRACTICE"


def test_decision_saves_policy_version() -> None:
    result = decide_next_action(base_input())
    assert result.decision.policy_version == "policy-0.1.0"
