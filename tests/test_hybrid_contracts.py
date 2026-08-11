"""Hybrid contract strictness: extra=forbid, bounds, serialization, PII.

Section 10 requires fail-closed models: no unknown fields, bounded
strings/lists, strict enums, no student identifiers, no raw history, no
provider secrets. These tests pin that boundary for every contract.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agent.hybrid_contracts import (
    CONTEXT_VERSION,
    ContentCandidate,
    EvidenceClaim,
    ExplanationProposal,
    HybridDecisionContext,
    HybridDecisionProposal,
    InterventionEvidence,
    RecalledEpisodeEvidence,
    VerifiedHybridDecision,
)
from app.agent.policy import (
    PolicyInput,
    derive_policy_constraints,
)
from app.domain.events import AgentDecision
from app.domain.memory import BoundedAction
from app.domain.sessions import SessionState


def episode(**overrides) -> dict:
    values = dict(
        episode_id="ep_001",
        skill="linear_equations",
        misconception="sign_error",
        intervention=BoundedAction.SHOW_WORKED_EXAMPLE,
        outcome_correct=True,
        different_item=True,
        effectiveness=1.0,
        confidence=0.8,
        status="validated",
        recency_bucket="recent",
        teaching_content_id="lesson-1",
        difficulty_band="d2",
    )
    values.update(overrides)
    return values


def stat(**overrides) -> dict:
    values = dict(
        intervention=BoundedAction.SHOW_WORKED_EXAMPLE,
        difficulty_band="d2",
        immediate_attempts=3,
        short_term_attempts=0,
        delayed_attempts=0,
        blended_effectiveness=0.75,
        support="supported",
    )
    values.update(overrides)
    return values


def content(**overrides) -> dict:
    values = dict(
        content_id="lesson-1",
        content_type="worked_example",
        skill="linear_equations",
        misconceptions=("sign_error",),
        pack_version="bridgesat-math-0.3.0",
        content_hash="sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        review_status="approved",
        human_approved=False,
    )
    values.update(overrides)
    return values


def fallback_decision() -> AgentDecision:
    return AgentDecision(
        action=BoundedAction.SHOW_WORKED_EXAMPLE.value,
        action_payload={"skill": "linear_equations"},
        reason_code="RECALLED_SUCCESSFUL_EPISODE",
        reason_text="reuse",
        policy_version="policy-0.1.0",
    )


def context(**overrides) -> dict:
    values = dict(
        task="intervention_ranking",
        context_version=CONTEXT_VERSION,
        skill="linear_equations",
        subskill=None,
        difficulty=2,
        mastery=0.5,
        mastery_confidence=0.4,
        consecutive_errors=0,
        correct_streak=0,
        active_misconception="sign_error",
        misconception_evidence_count=1,
        misconception_confidence="medium",
        hints_used=0,
        minutes_remaining=15,
        current_state=SessionState.ANSWER_EVALUATED,
        allowed_actions=(
            BoundedAction.RETRY_SAME_SKILL,
            BoundedAction.SHOW_WORKED_EXAMPLE,
            BoundedAction.SHOW_MICRO_LESSON,
        ),
        deterministic_fallback=fallback_decision(),
        recalled_episodes=(episode(),),
        intervention_stats=(stat(),),
        content_candidates=(content(),),
    )
    values.update(overrides)
    return values


def proposal(**overrides) -> dict:
    values = dict(
        proposed_action=BoundedAction.SHOW_WORKED_EXAMPLE,
        selected_episode_id="ep_001",
        selected_content_id="lesson-1",
        rationale_code="PRIOR_TRANSFER_SUCCESS",
        rationale="A worked example was followed by transfer success.",
        confidence=0.8,
        evidence_claims=(
            EvidenceClaim(claim_code="SUCCESSFUL_TRANSFER", evidence_refs=("ep_001",)),
        ),
    )
    values.update(overrides)
    return values


# ---------- extra="forbid" ----------

@pytest.mark.parametrize(
    "model, factory",
    [
        (RecalledEpisodeEvidence, episode),
        (InterventionEvidence, stat),
        (ContentCandidate, content),
        (HybridDecisionContext, context),
        (HybridDecisionProposal, proposal),
    ],
)
def test_contracts_reject_unknown_fields(model, factory) -> None:
    payload = dict(factory())
    payload["student_id"] = "stu-1"
    payload["prompt_secret"] = "sk-abc"
    with pytest.raises(ValidationError):
        model.model_validate(payload)


# ---------- enum / literal strictness ----------

def test_episode_status_is_strict_validated() -> None:
    with pytest.raises(ValidationError):
        RecalledEpisodeEvidence.model_validate(
            episode(status="candidate")
        )


def test_episode_recency_bucket_is_strict() -> None:
    with pytest.raises(ValidationError):
        RecalledEpisodeEvidence.model_validate(episode(recency_bucket="yesterday"))


def test_stat_support_label_is_strict() -> None:
    with pytest.raises(ValidationError):
        InterventionEvidence.model_validate(stat(support="strong"))


def test_content_type_is_strict() -> None:
    with pytest.raises(ValidationError):
        ContentCandidate.model_validate(content(content_type="video"))


def test_content_review_status_is_strict_approved() -> None:
    with pytest.raises(ValidationError):
        ContentCandidate.model_validate(content(review_status="simulated"))


def test_context_task_is_strict_intervention_ranking() -> None:
    with pytest.raises(ValidationError):
        HybridDecisionContext.model_validate(context(task="explanation"))


def test_context_misconception_confidence_is_strict() -> None:
    with pytest.raises(ValidationError):
        HybridDecisionContext.model_validate(
            context(misconception_confidence="certain")
        )


def test_proposal_claim_codes_are_strict() -> None:
    with pytest.raises(ValidationError):
        EvidenceClaim(claim_code="INVENTED_SUPERPOWER", evidence_refs=("ep_001",))


# ---------- bounds ----------

def test_mastery_bounds_reject_out_of_range() -> None:
    with pytest.raises(ValidationError):
        HybridDecisionContext.model_validate(context(mastery=1.5))
    with pytest.raises(ValidationError):
        HybridDecisionContext.model_validate(context(mastery=-0.1))


def test_effectiveness_bounds_reject_out_of_range() -> None:
    with pytest.raises(ValidationError):
        RecalledEpisodeEvidence.model_validate(episode(effectiveness=1.2))


def test_rationale_length_is_bounded() -> None:
    with pytest.raises(ValidationError):
        HybridDecisionProposal.model_validate(
            proposal(rationale="x" * 321)
        )


def test_allowed_actions_requires_at_least_one() -> None:
    with pytest.raises(ValidationError):
        HybridDecisionContext.model_validate(context(allowed_actions=()))


def test_allowed_actions_capped() -> None:
    many = tuple(BoundedAction)
    with pytest.raises(ValidationError):
        HybridDecisionContext.model_validate(context(allowed_actions=many))


def test_evidence_refs_are_bounded_and_scoped() -> None:
    with pytest.raises(ValidationError):
        EvidenceClaim(
            claim_code="SAME_MISCONCEPTION",
            evidence_refs=tuple(f"ref-{i}" for i in range(20)),
        )


def test_evidence_id_is_bounded() -> None:
    with pytest.raises(ValidationError):
        RecalledEpisodeEvidence.model_validate(episode(episode_id="e" * 65))


def test_confidence_range_rejected_out_of_bounds() -> None:
    with pytest.raises(ValidationError):
        HybridDecisionProposal.model_validate(proposal(confidence=1.5))
    with pytest.raises(ValidationError):
        HybridDecisionProposal.model_validate(proposal(confidence=-0.5))


# ---------- no PII / no raw providers ----------

def test_context_excludes_student_and_session_identifiers() -> None:
    payload = dict(context())
    assert "student_id" not in payload
    assert "session_id" not in payload
    assert "name" not in payload
    model = HybridDecisionContext.model_validate(payload)

    # model_dump() may differ only in nested model normalization, never in keys.
    dumped = model.model_dump()
    assert set(dumped.keys()) == set(payload.keys())
    assert "student_id" not in dumped
    assert "session_id" not in dumped


def test_context_serializes_without_extra_or_secrets() -> None:
    model = HybridDecisionContext.model_validate(context())
    serialized = model.model_dump()
    assert serialized["context_version"] == CONTEXT_VERSION
    assert "student" not in str(serialized)
    assert "token" not in str(serialized)
    assert "api_key" not in str(serialized)
    assert "tenant" not in str(serialized)
    assert "password" not in str(serialized)


def test_deterministic_fallback_keeps_only_decision_fields() -> None:
    model = HybridDecisionContext.model_validate(context())
    decision = model.deterministic_fallback
    assert decision.action == BoundedAction.SHOW_WORKED_EXAMPLE.value
    assert "student" not in decision.model_dump_json()


def test_verified_decision_contains_no_raw_output() -> None:
    verified = VerifiedHybridDecision(
        accepted=True,
        final_action=BoundedAction.SHOW_WORKED_EXAMPLE,
        model_used=True,
        fallback_used=False,
        fallback_reason=None,
        verification_checks=("action_allowed", "episode_grounded"),
        selected_episode_id="ep_001",
        selected_content_id="lesson-1",
        safe_student_explanation="A worked example helped you before.",
        model_task="intervention_ranking",
        model_name="deepseek-ai/deepseek-v4-flash-0731",
        prompt_version="decision-ranking-0.1.0",
        latency_ms=412,
    )
    raw = verified.model_dump()
    assert "prompt" not in raw
    assert "raw" not in raw
    with pytest.raises(ValidationError):
        VerifiedHybridDecision.model_validate(
            dict(
                raw, 
                model_config_field="should_not_exist",
            )
        )


def test_explanation_proposal_is_strict() -> None:
    with pytest.raises(ValidationError):
        ExplanationProposal.model_validate(
            {
                "student_explanation": "x" * 321,
                "emphasis": "magic",
                "evidence_refs": (),
            }
        )
    ok = ExplanationProposal.model_validate(
        {
            "student_explanation": "This worked for you before.",
            "emphasis": "process",
            "evidence_refs": ("ep_001",),
        }
    )
    assert ok.emphasis == "process"


# ---------- integration with H1 constraints ----------

def test_context_allowed_actions_match_derived_constraints() -> None:
    inputs = PolicyInput(
        student_id="stu-1",
        session_id="ses-1",
        skill="linear_equations",
        active_misconception="sign_error",
        misconception_observation_count=2,
    )
    constraints = derive_policy_constraints(inputs)
    ctx = HybridDecisionContext.model_validate(
        context(
            allowed_actions=constraints.allowed_actions,
            deterministic_fallback=constraints.preferred_fallback,
        )
    )
    assert ctx.allowed_actions == constraints.allowed_actions
    assert ctx.deterministic_fallback.action == constraints.preferred_fallback.action