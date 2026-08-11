"""PolicyConstraints derivation: allowed-action boundary and trajectory parity.

H1 contract (Hybrid Integration Plan Section 9):

- ``derive_policy_constraints`` is the single source for hard action, allowed
  actions, legal next states, and the current deterministic fallback;
- ``decide_next_action`` keeps its historical outputs exactly (parity);
- hard guards (time, prerequisite, recalled episode) produce a single action;
- every allowed action maps to a legal transition from the current state;
- every constraint carries a valid deterministic fallback.
"""

import pytest

from app.agent.policy import (
    PolicyEvidence,
    PolicyInput,
    decide_next_action,
    derive_policy_constraints,
)
from app.domain.memory import BoundedAction
from app.domain.sessions import SessionState, can_transition


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


def assert_parity(inputs: PolicyInput) -> None:
    """derive_policy_constraints fallback must reproduce decide_next_action."""
    constraints = derive_policy_constraints(inputs)
    legacy = decide_next_action(inputs)
    fallback = constraints.preferred_fallback_as_result()
    assert fallback.decision.action == legacy.decision.action
    assert fallback.decision.reason_code == legacy.decision.reason_code
    assert fallback.decision.reason_text == legacy.decision.reason_text
    assert fallback.decision.action_payload == legacy.decision.action_payload
    assert fallback.decision.episode_ids == legacy.decision.episode_ids
    assert fallback.decision.difficulty == legacy.decision.difficulty
    assert fallback.next_state == legacy.next_state


def assert_contract(inputs: PolicyInput, *, expect_hard: BoundedAction | None) -> None:
    constraints = derive_policy_constraints(inputs)
    assert constraints.policy_version == "policy-0.1.0"
    assert constraints.hard_action == expect_hard
    assert len(constraints.allowed_actions) >= 1
    assert constraints.allowed_actions == tuple(
        dict.fromkeys(constraints.allowed_actions)
    )
    fallback_action = BoundedAction(constraints.preferred_fallback.action)
    assert fallback_action in constraints.allowed_actions
    assert fallback_action in constraints.next_states
    assert constraints.reasons
    if expect_hard is not None:
        assert constraints.allowed_actions == (expect_hard,)
        assert constraints.next_states[expect_hard] is not None
    for action, target in constraints.next_states.items():
        assert action in constraints.allowed_actions
        assert can_transition(inputs.state, target), f"illegal {inputs.state}->{target}"
    assert_parity(inputs)


# ---------- hard-guard branches ----------

@pytest.mark.parametrize("minutes", [0, 1, 2])
def test_time_budget_is_single_hard_action(minutes: int) -> None:
    assert_contract(
        base_input(minutes_remaining=minutes),
        expect_hard=BoundedAction.END_WITH_REVIEW,
    )


def test_recalled_successful_episode_is_single_hard_action() -> None:
    assert_contract(
        base_input(
            recalled_successful_episode=True,
            recalled_episode_ids=["ep-1"],
            active_misconception="sign_error",
            misconception_observation_count=2,
        ),
        expect_hard=BoundedAction.SHOW_WORKED_EXAMPLE,
    )


def test_prerequisite_blocker_is_single_hard_action() -> None:
    assert_contract(
        base_input(requires_unmastered_prerequisite=True),
        expect_hard=BoundedAction.SWITCH_TO_PREREQUISITE,
    )


# ---------- non-hard branches ----------

def test_repeated_misconception_allows_teaching_subset() -> None:
    constraints = derive_policy_constraints(
        base_input(
            active_misconception="sign_error",
            misconception_observation_count=2,
            misconception_distinct_items=2,
        )
    )
    assert constraints.hard_action is None
    assert constraints.allowed_actions == (
        BoundedAction.RETRY_SAME_SKILL,
        BoundedAction.SHOW_WORKED_EXAMPLE,
        BoundedAction.SHOW_MICRO_LESSON,
    )
    assert constraints.preferred_fallback.action == BoundedAction.SHOW_WORKED_EXAMPLE.value
    assert constraints.preferred_fallback.reason_code == "REPEATED_MISCONCEPTION"
    assert constraints.next_states[BoundedAction.SHOW_WORKED_EXAMPLE] == SessionState.WORKED_EXAMPLE_ACTIVE
    assert_parity(base_input(
        active_misconception="sign_error",
        misconception_observation_count=2,
        misconception_distinct_items=2,
    ))


def test_repeated_errors_without_misconception_teaches_micro_lesson() -> None:
    constraints = derive_policy_constraints(base_input(consecutive_errors=2))
    assert constraints.hard_action is None
    assert constraints.preferred_fallback.action == BoundedAction.SHOW_MICRO_LESSON.value
    assert constraints.preferred_fallback.reason_code == "REPEATED_SKILL_ERRORS"
    assert constraints.next_states[BoundedAction.SHOW_MICRO_LESSON] == SessionState.MICRO_LESSON_ACTIVE
    assert_parity(base_input(consecutive_errors=2))


def test_first_misconception_allows_only_retry() -> None:
    constraints = derive_policy_constraints(
        base_input(active_misconception="sign_error", misconception_observation_count=1)
    )
    assert constraints.hard_action is None
    assert constraints.allowed_actions == (BoundedAction.RETRY_SAME_SKILL,)
    assert constraints.preferred_fallback.reason_code == "MISCONCEPTION_OBSERVED"
    assert constraints.next_states[BoundedAction.RETRY_SAME_SKILL] == SessionState.QUESTION_ACTIVE


def test_support_without_prioritized_misconception_lowers_difficulty() -> None:
    inputs = base_input(mastery=0.3, confidence=0.45, consecutive_errors=1)
    assert_contract(inputs, expect_hard=None)
    constraints = derive_policy_constraints(inputs)
    assert constraints.preferred_fallback.action == BoundedAction.LOWER_DIFFICULTY.value
    assert constraints.preferred_fallback.reason_code == "SUPPORT_NEEDED"
    assert constraints.preferred_fallback.difficulty == 1
    assert constraints.next_states[BoundedAction.LOWER_DIFFICULTY] == SessionState.PRACTICE_ADAPTED


def test_support_with_repeated_misconception_shows_worked_example() -> None:
    inputs = base_input(
        mastery=0.3,
        confidence=0.45,
        consecutive_errors=1,
        repeated_misconception=True,
    )
    constraints = derive_policy_constraints(inputs)
    assert constraints.preferred_fallback.action == BoundedAction.SHOW_WORKED_EXAMPLE.value
    assert constraints.preferred_fallback.reason_code == "REPEATED_MISCONCEPTION"
    assert constraints.next_states[BoundedAction.SHOW_WORKED_EXAMPLE] == SessionState.WORKED_EXAMPLE_ACTIVE
    assert_parity(inputs)


def test_promotion_raises_difficulty_with_retry_alternative() -> None:
    inputs = base_input(
        mastery=0.8,
        confidence=0.6,
        correct_streak=3,
        recent_correct_without_high_hint=3,
        recent_total=3,
    )
    constraints = derive_policy_constraints(inputs)
    assert constraints.hard_action is None
    assert constraints.allowed_actions == (
        BoundedAction.RETRY_SAME_SKILL,
        BoundedAction.RAISE_DIFFICULTY,
    )
    assert constraints.preferred_fallback.action == BoundedAction.RAISE_DIFFICULTY.value
    assert constraints.preferred_fallback.difficulty == 3
    assert constraints.next_states[BoundedAction.RAISE_DIFFICULTY] == SessionState.PRACTICE_ADAPTED
    assert_parity(inputs)


def test_default_continue_practice_is_single_action() -> None:
    constraints = derive_policy_constraints(base_input())
    assert constraints.hard_action is None
    assert constraints.allowed_actions == (BoundedAction.RETRY_SAME_SKILL,)
    assert constraints.preferred_fallback.reason_code == "CONTINUE_PRACTICE"
    assert constraints.next_states[BoundedAction.RETRY_SAME_SKILL] == SessionState.PRACTICE_ADAPTED
    assert_parity(base_input())


# ---------- evidence wiring (H6 placeholder) ----------

def test_evidence_parameter_is_accepted_and_does_not_change_fallback() -> None:
    inputs = base_input(consecutive_errors=2)
    evidence = PolicyEvidence(
        supported_interventions=(BoundedAction.SHOW_MICRO_LESSON,),
        conflicting_episodes=True,
    )
    without = derive_policy_constraints(inputs)
    with_evidence = derive_policy_constraints(inputs, evidence)
    assert with_evidence.preferred_fallback.action == without.preferred_fallback.action
    assert with_evidence.preferred_fallback.reason_code == without.preferred_fallback.reason_code


def test_empty_evidence_is_strict_by_default() -> None:
    assert PolicyEvidence.empty() == PolicyEvidence()


# ---------- trajectory table ----------

@pytest.mark.parametrize(
    ("overrides", "expected_action", "expected_state"),
    [
        ({"minutes_remaining": 2}, "END_WITH_REVIEW", SessionState.SESSION_SUMMARY),
        ({"recalled_successful_episode": True}, "SHOW_WORKED_EXAMPLE", SessionState.WORKED_EXAMPLE_ACTIVE),
        ({"requires_unmastered_prerequisite": True}, "SWITCH_TO_PREREQUISITE", SessionState.PRACTICE_ADAPTED),
        ({"consecutive_errors": 2}, "SHOW_MICRO_LESSON", SessionState.MICRO_LESSON_ACTIVE),
        (
            {"active_misconception": "sign_error", "misconception_observation_count": 2},
            "SHOW_WORKED_EXAMPLE",
            SessionState.WORKED_EXAMPLE_ACTIVE,
        ),
        (
            {"active_misconception": "sign_error", "misconception_observation_count": 1},
            "RETRY_SAME_SKILL",
            SessionState.QUESTION_ACTIVE,
        ),
        ({"mastery": 0.3, "confidence": 0.45}, "LOWER_DIFFICULTY", SessionState.PRACTICE_ADAPTED),
        (
            {
                "mastery": 0.8,
                "confidence": 0.6,
                "correct_streak": 3,
                "recent_correct_without_high_hint": 3,
                "recent_total": 3,
            },
            "RAISE_DIFFICULTY",
            SessionState.PRACTICE_ADAPTED,
        ),
        ({}, "RETRY_SAME_SKILL", SessionState.PRACTICE_ADAPTED),
    ],
)
def test_trajectory_table_matches_legacy_policy(
    overrides: dict, expected_action: str, expected_state: SessionState
) -> None:
    inputs = base_input(**overrides)
    constraints = derive_policy_constraints(inputs)
    assert constraints.preferred_fallback.action == expected_action
    assert constraints.preferred_fallback_as_result().next_state == expected_state
    assert_parity(inputs)