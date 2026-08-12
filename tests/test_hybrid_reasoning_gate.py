"""Hybrid Reasoning Gate branches, reproducibility, and PII-free contexts.

H2 is dark mode: eligibility is computed and auditable, but no model is
called anywhere on the main path. These tests pin the Section 8 decision
table: every deterministic fast path wins before semantic ambiguity is
considered, and the single-exact-episode competition demo never requires a
model.
"""

from __future__ import annotations

import pytest

from app.agent.hybrid import (
    MODE_DETERMINISTIC,
    MODE_HYBRID,
    REASON_AMBIGUOUS_ALLOWED_ACTIONS,
    REASON_BUDGET_EXHAUSTED,
    REASON_CIRCUIT_OPEN,
    REASON_CONFLICTING_EPISODES,
    REASON_HARD_ACTION,
    REASON_NOT_CONFIGURED,
    REASON_OFFLINE,
    REASON_SINGLE_ALLOWED_ACTION,
    REASON_STAT_DISAGREES_WITH_RECENT,
    REASON_TASK_DISABLED,
    GateDecision,
    HybridAvailability,
    HybridTask,
    choose_mode,
    exactly_one_action_gate,
    semantic_reasoning_needed,
    task_enabled,
    task_settings,
    validate_hybrid_runtime_configuration,
)
from app.agent.policy import (
    PolicyEvidence,
    PolicyInput,
    derive_policy_constraints,
)
from app.domain.memory import BoundedAction


def constraints_for(**overrides):
    inputs = PolicyInput(
        student_id="stu-1",
        session_id="ses-1",
        skill="linear_equations",
        **overrides,
    )
    return derive_policy_constraints(inputs)


AVAILABLE = HybridAvailability(configured=True)
ENABLED = HybridAvailability(configured=True)


@pytest.fixture(autouse=True)
def enable_all_tasks(monkeypatch):
    monkeypatch.setenv("BRIDGESAT_HYBRID_ENABLED", "1")
    monkeypatch.setenv("BRIDGESAT_HYBRID_SHADOW_ENABLED", "1")
    yield


def test_hard_action_wins_even_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("BRIDGESAT_HYBRID_SHADOW_ENABLED", "1")
    constraints = constraints_for(minutes_remaining=1)
    decision = choose_mode(constraints, PolicyEvidence.empty(), AVAILABLE)
    assert decision.mode == MODE_DETERMINISTIC
    assert decision.reasons == (REASON_HARD_ACTION,)
    assert not decision.is_hybrid


def test_recalled_single_episode_never_needs_model(monkeypatch) -> None:
    monkeypatch.setenv("BRIDGESAT_HYBRID_SHADOW_ENABLED", "1")
    constraints = constraints_for(
        recalled_successful_episode=True,
        recalled_episode_ids=["ep-1"],
    )
    decision = choose_mode(constraints, PolicyEvidence.empty(), AVAILABLE)
    assert decision.mode == MODE_DETERMINISTIC
    assert REASON_HARD_ACTION in decision.reasons
    policy = constraints_for(
        recalled_successful_episode=True,
        recalled_episode_ids=["ep-1"],
    )
    assert policy.hard_action == BoundedAction.SHOW_WORKED_EXAMPLE


def test_offline_is_deterministic() -> None:
    constraints = constraints_for(consecutive_errors=2)
    decision = choose_mode(
        constraints,
        PolicyEvidence.empty(),
        AVAILABLE,
        offline=True,
    )
    assert decision.mode == MODE_DETERMINISTIC
    assert decision.reasons == (REASON_OFFLINE,)


def test_missing_key_is_deterministic() -> None:
    constraints = constraints_for(consecutive_errors=2)
    decision = choose_mode(
        constraints,
        PolicyEvidence.empty(),
        HybridAvailability(configured=False),
    )
    assert decision.mode == MODE_DETERMINISTIC
    assert decision.reasons == (REASON_NOT_CONFIGURED,)


def test_circuit_open_is_deterministic() -> None:
    constraints = constraints_for(consecutive_errors=2)
    decision = choose_mode(
        constraints,
        PolicyEvidence.empty(),
        HybridAvailability(configured=True, circuit_open=True),
    )
    assert decision.mode == MODE_DETERMINISTIC
    assert decision.reasons == (REASON_CIRCUIT_OPEN,)


def test_budget_exhausted_is_deterministic() -> None:
    constraints = constraints_for(consecutive_errors=2)
    decision = choose_mode(
        constraints,
        PolicyEvidence.empty(),
        HybridAvailability(configured=True, budget_exhausted=True),
    )
    assert decision.mode == MODE_DETERMINISTIC
    assert decision.reasons == (REASON_BUDGET_EXHAUSTED,)


def test_task_flag_off_is_deterministic(monkeypatch) -> None:
    monkeypatch.setenv("BRIDGESAT_HYBRID_SHADOW_ENABLED", "0")
    constraints = constraints_for(consecutive_errors=2)
    decision = choose_mode(constraints, PolicyEvidence.empty(), AVAILABLE)
    assert decision.mode == MODE_DETERMINISTIC
    assert decision.reasons == (REASON_TASK_DISABLED,)


def test_single_allowed_action_is_deterministic() -> None:
    constraints = constraints_for(active_misconception="sign_error")
    decision = choose_mode(constraints, PolicyEvidence.empty(), AVAILABLE)
    assert decision.mode == MODE_DETERMINISTIC
    assert decision.reasons == (REASON_SINGLE_ALLOWED_ACTION,)


def test_ambiguous_allowed_actions_grants_hybrid_eligibility() -> None:
    constraints = constraints_for(active_misconception="sign_error", misconception_observation_count=2)
    needed, reasons = semantic_reasoning_needed(constraints, PolicyEvidence.empty())
    assert needed
    assert REASON_AMBIGUOUS_ALLOWED_ACTIONS in reasons
    decision = choose_mode(constraints, PolicyEvidence.empty(), AVAILABLE)
    assert decision.mode == MODE_HYBRID
    assert decision.reasons == (REASON_AMBIGUOUS_ALLOWED_ACTIONS,)


def test_conflicting_episodes_grants_hybrid_eligibility() -> None:
    constraints = constraints_for(consecutive_errors=2)
    evidence = PolicyEvidence(
        supported_interventions=(),
        conflicting_episodes=True,
    )
    decision = choose_mode(constraints, evidence, AVAILABLE)
    assert decision.mode == MODE_HYBRID
    assert REASON_CONFLICTING_EPISODES in decision.reasons


def test_supported_stat_differing_from_fallback_signals_ambiguity() -> None:
    constraints = constraints_for(mastery=0.3, confidence=0.45)
    assert constraints.preferred_fallback.action == BoundedAction.LOWER_DIFFICULTY.value
    evidence = PolicyEvidence(
        supported_interventions=(BoundedAction.SHOW_WORKED_EXAMPLE,),
        conflicting_episodes=False,
    )
    needed, reasons = semantic_reasoning_needed(constraints, evidence)
    assert needed
    assert REASON_STAT_DISAGREES_WITH_RECENT in reasons


def test_supported_stat_matching_fallback_is_not_ambiguity_signal() -> None:
    constraints = constraints_for(mastery=0.3, confidence=0.45)
    evidence = PolicyEvidence(
        supported_interventions=(BoundedAction.LOWER_DIFFICULTY,),
        conflicting_episodes=False,
    )
    needed, reasons = semantic_reasoning_needed(constraints, evidence)
    assert REASON_STAT_DISAGREES_WITH_RECENT not in reasons
    assert REASON_CONFLICTING_EPISODES not in reasons
    assert needed is (len(constraints.allowed_actions) >= 2)


def test_eligibility_is_reproducible() -> None:
    first = choose_mode(
        constraints_for(consecutive_errors=2),
        PolicyEvidence.empty(),
        AVAILABLE,
    )
    second = choose_mode(
        constraints_for(consecutive_errors=2),
        PolicyEvidence.empty(),
        AVAILABLE,
    )
    assert first == second
    assert first.mode == MODE_HYBRID
    assert first.reasons == second.reasons


def test_exactly_one_action_gate_keeps_wording_path_open() -> None:
    constraints = constraints_for(recalled_successful_episode=True)
    decision = exactly_one_action_gate(
        constraints,
        PolicyEvidence.empty(),
        HybridAvailability(configured=True),
    )
    assert decision.mode == MODE_HYBRID
    assert decision.reasons == ()


def test_flag_defaults_and_task_settings() -> None:
    assert task_enabled(HybridTask.DECISION_REASONING) is True
    settings = task_settings(HybridTask.DECISION_REASONING)
    assert settings.timeout_ms <= 2000
    assert settings.max_tokens > 0
    assert settings.prompt_version
    assert settings.enabled is True


def test_master_flag_off_blocks_all_tasks(monkeypatch) -> None:
    monkeypatch.setenv("BRIDGESAT_HYBRID_ENABLED", "0")
    assert task_enabled(HybridTask.EXPLANATION) is False
    settings = task_settings(HybridTask.EXPLANATION)
    assert settings.enabled is False


def test_competition_mode_freezes_hybrid_runtime(monkeypatch) -> None:
    monkeypatch.setenv("BRIDGESAT_HYBRID_COMPETITION_MODE", "1")
    monkeypatch.setenv("BRIDGESAT_HYBRID_ENABLED", "1")
    monkeypatch.setenv("BRIDGESAT_HYBRID_SUMMARY_ENABLED", "1")

    assert task_enabled(HybridTask.SUMMARY) is False
    with pytest.raises(RuntimeError, match="competition mode"):
        validate_hybrid_runtime_configuration()


def test_gate_decision_shape() -> None:
    decision = GateDecision(MODE_DETERMINISTIC, ("a", "b"))
    assert not decision.is_hybrid
    assert decision.reasons == ("a", "b")
    assert GateDecision(MODE_HYBRID, ()).is_hybrid
