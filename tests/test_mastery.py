import pytest

from app.domain.learner import (
    HINT_MULTIPLIER,
    DIFFICULTY_WEIGHT,
    EvidenceWeight,
    SkillState,
    next_misconception_state,
    should_promote_difficulty,
    should_support,
)


def test_initial_state() -> None:
    state = SkillState(skill="linear_equations")
    assert state.alpha == 2.0
    assert state.beta == 2.0
    assert state.mastery == pytest.approx(0.5)
    assert state.confidence == pytest.approx(0.0)


def test_weighted_beta_update() -> None:
    state = SkillState(skill="linear_equations")
    state.record_attempt(correct=True, weight=1.0, now="2026-08-06T10:00:00+00:00")
    assert state.alpha == pytest.approx(3.0)
    assert state.mastery == pytest.approx(3.0 / 5.0)
    assert state.correct_streak == 1
    state.record_attempt(correct=False, weight=1.25, now="2026-08-06T10:01:00+00:00")
    assert state.beta == pytest.approx(3.25)
    assert state.incorrect_streak == 1
    assert state.correct_streak == 0


def test_evidence_weight_multipliers() -> None:
    base = EvidenceWeight(difficulty=2, hint_level=0).weight()
    assert base == DIFFICULTY_WEIGHT[2] * HINT_MULTIPLIER[0]

    hinted = EvidenceWeight(difficulty=2, hint_level=2).weight()
    assert hinted == DIFFICULTY_WEIGHT[2] * HINT_MULTIPLIER[2]

    repeated = EvidenceWeight(difficulty=2, hint_level=0, repeated_same_item=True).weight()
    assert repeated == pytest.approx(base * 0.35)

    transfer = EvidenceWeight(difficulty=2, hint_level=0, immediate_transfer=True).weight()
    assert transfer == pytest.approx(base * 1.10)

    invalid = EvidenceWeight(difficulty=2, hint_level=0, validity_multiplier=0.0).weight()
    assert invalid == 0.0


def test_confidence_formula() -> None:
    state = SkillState(skill="ratios_percentages", alpha=6.0, beta=2.0)
    assert state.confidence == pytest.approx(min(1.0, (8.0 - 4.0) / 8.0))
    big = SkillState(skill="functions_models", alpha=20.0, beta=2.0)
    assert big.confidence == pytest.approx(1.0)


def test_promotion_requires_all_conditions() -> None:
    assert not should_promote_difficulty(
        mastery=0.5, confidence=0.6,
        recent_correct_without_high_hint=2, recent_total=3,
        has_active_high_confidence_misconception=False,
    )
    assert not should_promote_difficulty(
        mastery=0.8, confidence=0.6,
        recent_correct_without_high_hint=1, recent_total=3,
        has_active_high_confidence_misconception=False,
    )
    assert not should_promote_difficulty(
        mastery=0.8, confidence=0.6,
        recent_correct_without_high_hint=2, recent_total=3,
        has_active_high_confidence_misconception=True,
    )
    assert should_promote_difficulty(
        mastery=0.8, confidence=0.6,
        recent_correct_without_high_hint=2, recent_total=3,
        has_active_high_confidence_misconception=False,
    )


def test_support_conditions() -> None:
    assert should_support(
        mastery=0.8, confidence=0.8,
        consecutive_errors=2, repeated_misconception=False,
        requires_unmastered_prerequisite=False,
    )
    assert should_support(
        mastery=0.8, confidence=0.8,
        consecutive_errors=0, repeated_misconception=True,
        requires_unmastered_prerequisite=False,
    )
    assert should_support(
        mastery=0.4, confidence=0.5,
        consecutive_errors=0, repeated_misconception=False,
        requires_unmastered_prerequisite=False,
    )
    assert not should_support(
        mastery=0.8, confidence=0.8,
        consecutive_errors=0, repeated_misconception=False,
        requires_unmastered_prerequisite=False,
    )


def test_misconception_state_progression() -> None:
    assert next_misconception_state(1, 1) == "observed"
    assert next_misconception_state(2, 2) == "suspected"
    assert next_misconception_state(3, 2) == "confirmed"
    assert next_misconception_state(3, 1) == "suspected"
