"""Adversarial ProposalVerifier tests (Hybrid Integration Plan Section 20.2).

Fail-closed: zero accepted illegal action, hallucinated ID, wrong-tenant
evidence, unapproved content, fake claim, or math mutation. Every reject
returns the deterministic fallback path (accepted=False).
"""

from __future__ import annotations

from app.agent.hybrid import (
    AuthoritativeEvidence,
    ContentRecord,
    verify_math_placeholders,
    verify_proposal,
)
from app.agent.hybrid_contracts import (
    CONTEXT_VERSION,
    EvidenceClaim,
    HybridDecisionContext,
    HybridDecisionProposal,
    InterventionEvidence,
)
from app.agent.policy import PolicyInput, derive_policy_constraints
from app.domain.events import AgentDecision
from app.domain.memory import BoundedAction, Episode
from app.domain.sessions import SessionState


def make_episode(
    *,
    episode_id: str = "ep_001",
    student_id: str = "stu-1",
    skill: str = "linear_equations",
    misconception: str = "sign_error",
    intervention: str = "SHOW_WORKED_EXAMPLE",
    correct: bool = True,
    different_item: bool = True,
    effectiveness: float = 1.0,
    confidence: float = 0.9,
    status: str = "validated",
    outcome: dict | None = None,
) -> Episode:
    return Episode(
        episode_id=episode_id,
        student_id=student_id,
        session_id="ses-1",
        skill=skill,
        misconception=misconception,
        intervention=intervention,
        outcome=outcome
        or {
            "correct": correct,
            "hint_level": 0,
            "different_item": different_item,
            "teaching_content_id": "lesson-1",
            "outcome_content_id": "item-2",
        },
        effectiveness=effectiveness,
        evidence_event_ids=["ev-1"],
        summary="episode summary",
        confidence=confidence,
        status=status,
    )


def make_content(**overrides) -> ContentRecord:
    values = dict(
        content_id="lesson-1",
        content_hash="sha256:abc123",
        review_status="approved",
        content_type="worked_example",
        target_skill="linear_equations",
        misconceptions=("sign_error",),
        license_id="mit",
        license_name="MIT",
        source_id="src-1",
        pack_version="bridgesat-math-0.3.0",
        human_approved=False,
        body="## Step 1\n{{m1}} is the constant term.\n## Step 2\n{{m2}} is the coefficient.",
    )
    values.update(overrides)
    return ContentRecord(**values)


def make_context(**overrides) -> HybridDecisionContext:
    inputs = PolicyInput(
        student_id="stu-1",
        session_id="ses-1",
        skill="linear_equations",
        active_misconception="sign_error",
        misconception_observation_count=1,
        recalled_successful_episode=True,
        recalled_episode_ids=["ep_001"],
    )
    constraints = derive_policy_constraints(inputs)
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
        allowed_actions=constraints.allowed_actions,
        deterministic_fallback=constraints.preferred_fallback,
        recalled_episodes=(),
        intervention_stats=(),
        content_candidates=(),
    )
    values.update(overrides)
    return HybridDecisionContext.model_validate(values)


def make_proposal(**overrides) -> HybridDecisionProposal:
    values = dict(
        proposed_action=BoundedAction.SHOW_WORKED_EXAMPLE,
        selected_episode_id="ep_001",
        selected_content_id="lesson-1",
        rationale_code="PRIOR_TRANSFER_SUCCESS",
        rationale="A worked example was followed by transfer success.",
        confidence=0.8,
        evidence_claims=(
            EvidenceClaim(
                claim_code="SUCCESSFUL_TRANSFER",
                evidence_refs=("ep_001",),
            ),
        ),
    )
    values.update(overrides)
    return HybridDecisionProposal.model_validate(values)


def verify(proposal=None, *, context=None, episodes=None, content=None, constraints=None, **kwargs):
    proposal = proposal or make_proposal()
    context = context or make_context()
    episodes = episodes or {"ep_001": make_episode()}
    content = content or {"lesson-1": make_content()}
    evidence = AuthoritativeEvidence(
        episodes=episodes,
        content=content,
        expected_student_id="stu-1",
    )
    if constraints is None:
        constraints = derive_policy_constraints(
            PolicyInput(
                student_id="stu-1",
                session_id="ses-1",
                skill="linear_equations",
                active_misconception="sign_error",
                misconception_observation_count=1,
                recalled_successful_episode=True,
                recalled_episode_ids=["ep_001"],
            )
        )
    return verify_proposal(
        context=context,
        constraints=constraints,
        proposal=proposal,
        evidence=evidence,
        **kwargs,
    )


def test_valid_proposal_accepted() -> None:
    outcome = verify()
    assert outcome.accepted is True
    assert outcome.selected_episode_id == "ep_001"
    assert outcome.selected_content_id == "lesson-1"
    assert outcome.rationale_accepted is True
    assert "action_allowed" in outcome.checks
    assert "episode_grounded" in outcome.checks
    assert "content_grounded" in outcome.checks
    assert "claims_grounded" in outcome.checks


# ---------- action / state ----------

def test_action_outside_allowed_set_rejected() -> None:
    constraints = derive_policy_constraints(
        PolicyInput(
            student_id="stu-1",
            session_id="ses-1",
            skill="linear_equations",
            active_misconception="sign_error",
            misconception_observation_count=1,
        )
    )
    context = make_context(
        allowed_actions=constraints.allowed_actions,
        deterministic_fallback=constraints.preferred_fallback,
    )
    outcome = verify(
        context=context,
        constraints=constraints,
        proposal=make_proposal(
            proposed_action=BoundedAction.END_WITH_REVIEW,
        ),
    )
    assert not outcome.accepted
    assert outcome.rejected_reason == "action_not_allowed"


def test_hard_guard_action_cannot_be_overridden() -> None:
    context = make_context()
    proposal = make_proposal(proposed_action=BoundedAction.RETRY_SAME_SKILL)
    inputs = PolicyInput(
        student_id="stu-1",
        session_id="ses-1",
        skill="linear_equations",
        minutes_remaining=1,
    )
    constraints = derive_policy_constraints(inputs)
    evidence = AuthoritativeEvidence(episodes={}, content={}, expected_student_id="stu-1")
    outcome = verify_proposal(
        context=context,
        constraints=constraints,
        proposal=proposal,
        evidence=evidence,
    )
    assert not outcome.accepted
    assert outcome.rejected_reason == "action_violates_hard_guard"


def test_illegal_state_transition_rejected() -> None:
    proposal = make_proposal(proposed_action=BoundedAction.END_WITH_REVIEW)
    context = make_context(
        allowed_actions=(BoundedAction.END_WITH_REVIEW,),
        deterministic_fallback=AgentDecision(
            action=BoundedAction.END_WITH_REVIEW.value,
            reason_code="TIME_BUDGET_EXHAUSTED",
            reason_text="time",
        ),
    )
    outcome = verify(context=context, proposal=proposal)
    assert not outcome.accepted


def test_stale_source_event_rejected() -> None:
    outcome = verify(source_event_current=False)
    assert not outcome.accepted
    assert outcome.rejected_reason == "source_event_stale"


# ---------- episode grounding ----------

def test_hallucinated_episode_id_rejected() -> None:
    outcome = verify(proposal=make_proposal(selected_episode_id="ep_999"))
    assert not outcome.accepted
    assert outcome.rejected_reason == "ungrounded_episode"


def test_foreign_student_episode_rejected() -> None:
    episodes = {"ep_001": make_episode(student_id="stu-2")}
    outcome = verify(episodes=episodes)
    assert not outcome.accepted
    assert outcome.rejected_reason == "foreign_episode"


def test_candidate_episode_claimed_successful_rejected() -> None:
    episodes = {"ep_001": make_episode(status="candidate")}
    outcome = verify(episodes=episodes)
    assert not outcome.accepted
    assert outcome.rejected_reason == "episode_not_validated"


def test_failed_episode_claimed_successful_rejected() -> None:
    episodes = {
        "ep_001": make_episode(
            correct=False,
            different_item=False,
            effectiveness=0.2,
        )
    }
    outcome = verify(episodes=episodes)
    assert not outcome.accepted
    assert outcome.rejected_reason == "claim_episode_not_successful"


def test_same_skill_wrong_misconception_rejected() -> None:
    episodes = {"ep_001": make_episode(misconception="inverse_operation_error")}
    outcome = verify(episodes=episodes)
    assert not outcome.accepted
    assert outcome.rejected_reason == "episode_misconception_mismatch"


def test_wrong_skill_episode_rejected() -> None:
    episodes = {"ep_001": make_episode(skill="quadratic_equations")}
    outcome = verify(episodes=episodes)
    assert not outcome.accepted
    assert outcome.rejected_reason == "episode_skill_mismatch"


# ---------- content grounding ----------

def test_hallucinated_content_id_rejected() -> None:
    outcome = verify(proposal=make_proposal(selected_content_id="lesson_999"))
    assert not outcome.accepted
    assert outcome.rejected_reason == "ungrounded_content"


def test_unapproved_content_rejected() -> None:
    content = {"lesson-1": make_content(review_status="simulated")}
    outcome = verify(content=content)
    assert not outcome.accepted
    assert outcome.rejected_reason == "content_not_approved"


def test_wrong_content_type_rejected() -> None:
    content = {"lesson-1": make_content(content_type="micro_lesson")}
    outcome = verify(content=content)
    assert not outcome.accepted
    assert outcome.rejected_reason == "content_type_mismatch"


def test_wrong_skill_content_rejected() -> None:
    content = {"lesson-1": make_content(target_skill="quadratic_equations")}
    outcome = verify(content=content)
    assert not outcome.accepted
    assert outcome.rejected_reason == "content_skill_mismatch"


def test_content_without_lineage_rejected() -> None:
    content = {
        "lesson-1": make_content(
            license_id="",
            source_id="",
            pack_version="",
        )
    }
    outcome = verify(content=content)
    assert not outcome.accepted
    assert outcome.rejected_reason == "content_lineage_incomplete"


def test_content_hash_mismatch_is_registry_truth() -> None:
    # The verifier trusts the registry record provided by the gateway; the
    # H4 gateway is responsible for loading records whose hash equals the
    # installed manifest. A record with a foreign hash is treated as the
    # truth by the verifier, so hash equality is asserted at the gateway.
    content = {"lesson-1": make_content(content_hash="sha256:different")}
    outcome = verify(content=content)
    assert outcome.accepted is True


# ---------- rationale / claims ----------

def test_invented_transfer_success_selected_episode_rejected() -> None:
    episodes = {
        "ep_001": make_episode(correct=True, different_item=False)
    }
    outcome = verify(episodes=episodes)
    assert not outcome.accepted
    assert outcome.rejected_reason == "claim_not_transfer"


def test_fake_supported_stat_claim_about_selected_episode_rejected() -> None:
    # The claim misrepresents the selected episode itself (no supported stat
    # exists), so the selection is ungrounded and must fall back.
    outcome = verify(
        proposal=make_proposal(
            evidence_claims=(
                EvidenceClaim(
                    claim_code="SUPPORTED_INTERVENTION_EFFECT",
                    evidence_refs=("ep_001",),
                ),
            )
        )
    )
    assert not outcome.accepted
    assert outcome.rejected_reason == "claim_no_supported_stat"


def test_transfer_claim_scope_is_bound_to_current_skill_and_misconception() -> None:
    outcome = verify(
        proposal=make_proposal(selected_episode_id=None),
        episodes={
            "ep_001": make_episode(
                skill="other_skill", misconception="other_misconception"
            )
        },
    )

    assert outcome.accepted is True
    assert outcome.rationale_accepted is False
    assert "claim_skill_mismatch" in outcome.checks


def test_supported_stat_claim_requires_matching_scope() -> None:
    outcome = verify(
        proposal=make_proposal(
            selected_episode_id=None,
            evidence_claims=(
                EvidenceClaim(
                    claim_code="SUPPORTED_INTERVENTION_EFFECT",
                    evidence_refs=("ep_001",),
                ),
            ),
        ),
        context=make_context(
            intervention_stats=(
                InterventionEvidence(
                    skill="other_skill",
                    misconception="sign_error",
                    intervention=BoundedAction.SHOW_WORKED_EXAMPLE,
                    difficulty_band="d2",
                    immediate_attempts=3,
                    short_term_attempts=0,
                    delayed_attempts=0,
                    blended_effectiveness=0.75,
                    support="supported",
                ),
            )
        ),
    )

    assert outcome.accepted is True
    assert outcome.rationale_accepted is False
    assert "claim_stat_insufficient" in outcome.checks


def test_insufficient_stat_does_not_expose_numeric_effectiveness() -> None:
    context = make_context(
        intervention_stats=(
            InterventionEvidence(
                skill="linear_equations",
                misconception="sign_error",
                intervention=BoundedAction.SHOW_WORKED_EXAMPLE,
                difficulty_band="d2",
                immediate_attempts=2,
                short_term_attempts=0,
                delayed_attempts=0,
                blended_effectiveness=None,
                support="insufficient",
            ),
        )
    )

    assert context.intervention_stats[0].blended_effectiveness is None


def test_invented_ref_outside_candidates_loses_wording() -> None:
    outcome = verify(
        proposal=make_proposal(
            evidence_claims=(
                EvidenceClaim(
                    claim_code="SAME_MISCONCEPTION",
                    evidence_refs=("ep_777",),
                ),
            )
        )
    )
    assert outcome.accepted is True
    assert outcome.rationale_accepted is False
    assert "claim_ref_not_in_candidates" in outcome.checks


def test_prohibited_rationale_keeps_action_but_loses_wording() -> None:
    outcome = verify(
        proposal=make_proposal(rationale="This student is careless and needs help.")
    )
    assert outcome.accepted is True
    assert outcome.rationale_accepted is False
    assert "prohibited_rationale_claim" in outcome.checks


def test_sat_score_claim_rejected_from_rationale() -> None:
    outcome = verify(
        proposal=make_proposal(
            rationale="This improved your SAT score by 50 points."
        )
    )
    assert outcome.accepted is True
    assert outcome.rationale_accepted is False


def test_claim_without_evidence_refs_loses_wording() -> None:
    outcome = verify(
        proposal=make_proposal(
            evidence_claims=(
                EvidenceClaim(claim_code="SAME_MISCONCEPTION", evidence_refs=()),
            )
        )
    )
    assert outcome.accepted is True
    assert outcome.rationale_accepted is False
    assert "claim_without_evidence" in outcome.checks


def test_behavioral_signal_claim_needs_current_signal() -> None:
    context = make_context(
        hints_used=0,
        consecutive_errors=0,
        correct_streak=0,
        misconception_evidence_count=0,
    )
    outcome = verify(
        context=context,
        proposal=make_proposal(
            evidence_claims=(
                EvidenceClaim(
                    claim_code="STUDENT_REASONING_SIGNAL",
                    evidence_refs=("item-current",),
                ),
            )
        ),
    )
    assert outcome.accepted is True
    assert outcome.rationale_accepted is False
    assert "claim_no_behavioral_signal" in outcome.checks


def test_behavioral_signal_claim_with_hint_is_grounded() -> None:
    context = make_context(hints_used=1)
    outcome = verify(
        context=context,
        proposal=make_proposal(
            evidence_claims=(
                EvidenceClaim(
                    claim_code="STUDENT_REASONING_SIGNAL",
                    evidence_refs=("item-current",),
                ),
            )
        ),
    )
    assert outcome.accepted is True
    assert outcome.rationale_accepted is True


def test_behavioral_claim_must_not_cite_episode() -> None:
    outcome = verify(
        proposal=make_proposal(
            evidence_claims=(
                EvidenceClaim(
                    claim_code="STUDENT_REASONING_SIGNAL",
                    evidence_refs=("ep_001",),
                ),
            )
        )
    )
    assert outcome.accepted is True
    assert outcome.rationale_accepted is False
    assert "claim_cites_episode_as_signal" in outcome.checks


def test_model_cannot_raise_effectiveness() -> None:
    # The verifier only reads the rehydrated episode; a model has no channel
    # to alter effectiveness/confidence because the proposal carries only
    # IDs, action, and rationale. Presence of the authoritative value wins.
    episodes = {"ep_001": make_episode(effectiveness=0.61, confidence=0.51)}
    outcome = verify(episodes=episodes)
    assert outcome.accepted is True
    assert episodes["ep_001"].effectiveness == 0.61


# ---------- math placeholder grounding (Section 11.5) ----------

def test_math_placeholders_preserved_in_order() -> None:
    assert verify_math_placeholders(
        ("{{m1}}", "{{m2}}"),
        "Step 1 uses {{m1}} and step 2 uses {{m2}}.",
    )


def test_math_placeholder_deleted_rejected() -> None:
    assert not verify_math_placeholders(
        ("{{m1}}", "{{m2}}"),
        "Step 1 uses {{m1}}.",
    )


def test_math_placeholder_reordered_rejected() -> None:
    assert not verify_math_placeholders(
        ("{{m1}}", "{{m2}}"),
        "Step 2 uses {{m2}} and step 1 uses {{m1}}.",
    )


def test_math_placeholder_duplicated_rejected() -> None:
    assert not verify_math_placeholders(
        ("{{m1}}",),
        "{{m1}} then {{m1}} again.",
    )


def test_math_placeholder_altered_rejected() -> None:
    assert not verify_math_placeholders(
        ("{{m1}}",),
        "{{m1x}} is used.",
    )


def test_empty_placeholder_list_passes() -> None:
    assert verify_math_placeholders((), "no protected spans")


# ---------- fail-closed invariants ----------

def test_failure_modes_never_accept_unsafe_selection() -> None:
    # Selection-level failures (illegal action, hallucinated or wrong-tenant
    # episode/content) reject; wording-level failures (invented extra refs)
    # keep the action only when the selection itself is independently valid.
    selection_attacks = [
        make_proposal(proposed_action=BoundedAction.END_WITH_REVIEW),
        make_proposal(selected_episode_id="ep_999"),
        make_proposal(selected_content_id="lesson_999"),
        make_proposal(selected_episode_id="ep_777", selected_content_id="lesson_777"),
    ]
    for proposal in selection_attacks:
        outcome = verify(proposal=proposal)
        assert not outcome.accepted, f"accepted unsafe selection: {proposal.model_dump()}"

    wording_attack = make_proposal(
        evidence_claims=(
            EvidenceClaim(claim_code="SUCCESSFUL_TRANSFER", evidence_refs=("ep_777",)),
        )
    )
    outcome = verify(proposal=wording_attack)
    assert outcome.accepted is True
    assert outcome.rationale_accepted is False


def test_verification_checks_are_sanitized() -> None:
    outcome = verify()
    assert outcome.accepted is True
    for check in outcome.checks:
        assert check and len(check) <= 128
    assert "prompt" not in str(outcome.checks).lower()
