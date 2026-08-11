from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel

from app.domain.events import AgentDecision
from app.domain.learner import should_promote_difficulty, should_support
from app.domain.memory import BoundedAction
from app.domain.sessions import SessionState

POLICY_VERSION = "policy-0.1.0"

# Canonical action -> next session state mapping. Used by the Hybrid
# ProposalVerifier to validate the state transition implied by a proposed
# action; per-situation overrides (e.g. CONTINUE_PRACTICE) are applied in the
# derived constraints instance, never here.
NEXT_STATE_BY_ACTION: dict[BoundedAction, SessionState] = {
    BoundedAction.SHOW_WORKED_EXAMPLE: SessionState.WORKED_EXAMPLE_ACTIVE,
    BoundedAction.SHOW_MICRO_LESSON: SessionState.MICRO_LESSON_ACTIVE,
    BoundedAction.RETRY_SAME_SKILL: SessionState.QUESTION_ACTIVE,
    BoundedAction.LOWER_DIFFICULTY: SessionState.PRACTICE_ADAPTED,
    BoundedAction.RAISE_DIFFICULTY: SessionState.PRACTICE_ADAPTED,
    BoundedAction.SWITCH_TO_PREREQUISITE: SessionState.PRACTICE_ADAPTED,
    BoundedAction.END_WITH_REVIEW: SessionState.SESSION_SUMMARY,
}


@dataclass
class PolicyInput:
    student_id: str
    session_id: str
    skill: str
    subskill: str | None = None
    difficulty: int = 2
    mastery: float = 0.5
    confidence: float = 0.0
    consecutive_errors: int = 0
    correct_streak: int = 0
    repeated_misconception: bool = False
    active_misconception: str | None = None
    misconception_observation_count: int = 0
    misconception_distinct_items: int = 0
    requires_unmastered_prerequisite: bool = False
    minutes_remaining: int = 20
    hints_used_this_item: int = 0
    state: SessionState = SessionState.ANSWER_EVALUATED
    recalled_successful_episode: bool = False
    recalled_episode_ids: list[str] = field(default_factory=list)
    recent_correct_without_high_hint: int = 0
    recent_total: int = 0


@dataclass
class PolicyResult:
    decision: AgentDecision
    next_state: SessionState


class PolicyEvidence(BaseModel):
    """Structured evidence that may widen or narrow the allowed action set.

    H1 starts with empty evidence: the deterministic policy is the only
    authority and allowed actions mirror its branches. H6+ populates
    ``supported_interventions`` from validated InterventionStat windows and
    ``conflicting_episodes`` from scoped PostgreSQL recall so the Reasoning
    Gate can distinguish real semantic ambiguity.
    """

    supported_interventions: tuple[BoundedAction, ...] = ()
    conflicting_episodes: bool = False

    @staticmethod
    def empty() -> "PolicyEvidence":
        return PolicyEvidence()


class PolicyConstraints(BaseModel):
    """The safe decision boundary derived from one policy source.

    ``hard_action`` is the single mandated action (time guard, prerequisite
    guard, exact recalled episode, ...). ``allowed_actions`` is the ordered
    set of legal, defensible teaching moves; ``preferred_fallback`` is what
    deterministic policy executes today. Model proposals are verified against
    ``allowed_actions`` and ``next_states`` before execution (Hybrid
    Integration Plan Section 9).
    """

    hard_action: BoundedAction | None = None
    allowed_actions: tuple[BoundedAction, ...]
    preferred_fallback: AgentDecision
    next_states: dict[BoundedAction, SessionState]
    reasons: tuple[str, ...]
    policy_version: str

    def preferred_fallback_as_result(self) -> PolicyResult:
        action = BoundedAction(self.preferred_fallback.action)
        return PolicyResult(
            decision=self.preferred_fallback,
            next_state=self.next_states[action],
        )


def _decision(
    *,
    inputs: PolicyInput,
    action: BoundedAction,
    reason_code: str,
    reason_text: str,
    payload: dict[str, object],
    episode_ids: list[str] | None = None,
    difficulty: int | None = None,
) -> AgentDecision:
    return AgentDecision(
        action=action.value,
        action_payload=payload,
        reason_code=reason_code,
        reason_text=reason_text,
        target_skill=inputs.skill,
        difficulty=inputs.difficulty if difficulty is None else difficulty,
        episode_ids=list(episode_ids or []),
        policy_version=POLICY_VERSION,
    )


def derive_policy_constraints(
    inputs: PolicyInput,
    evidence: PolicyEvidence | None = None,
) -> PolicyConstraints:
    """Derive hard action, allowed actions, legal next states, and the current
    deterministic fallback from one policy source.

    Order of checks (identical to the pre-H1 trajectory):
    1. time budget closure (END_WITH_REVIEW)
    2. memory-aware early reuse: validated successful episode for the same
       skill+misconception -> SHOW_WORKED_EXAMPLE before the second error
       (RECALLED_SUCCESSFUL_EPISODE)
    3. support conditions (consecutive errors, repeated misconception, low
       mastery, prerequisite) -> SHOW_WORKED_EXAMPLE / SHOW_MICRO_LESSON /
       SWITCH_TO_PREREQUISITE
    4. promotion conditions -> RAISE_DIFFICULTY
    5. default -> RETRY_SAME_SKILL
    """
    evidence = evidence or PolicyEvidence.empty()
    skill = inputs.skill

    if inputs.minutes_remaining <= 2:
        decision = _decision(
            inputs=inputs,
            action=BoundedAction.END_WITH_REVIEW,
            reason_code="TIME_BUDGET_EXHAUSTED",
            reason_text=(
                "Only a few minutes remain, so the agent closes with a short "
                "review instead of starting a new item."
            ),
            payload={"review": "time_budget"},
        )
        return PolicyConstraints(
            hard_action=BoundedAction.END_WITH_REVIEW,
            allowed_actions=(BoundedAction.END_WITH_REVIEW,),
            preferred_fallback=decision,
            next_states={BoundedAction.END_WITH_REVIEW: SessionState.SESSION_SUMMARY},
            reasons=("TIME_BUDGET_EXHAUSTED",),
            policy_version=POLICY_VERSION,
        )

    if inputs.recalled_successful_episode:
        decision = _decision(
            inputs=inputs,
            action=BoundedAction.SHOW_WORKED_EXAMPLE,
            reason_code="RECALLED_SUCCESSFUL_EPISODE",
            reason_text=(
                "A validated episode from earlier practice shows that a worked "
                "example succeeded for this same error; reusing it before more "
                "practice."
            ),
            payload={
                "skill": skill,
                "subskill": inputs.subskill,
                "misconception": inputs.active_misconception,
            },
            episode_ids=list(inputs.recalled_episode_ids),
        )
        return PolicyConstraints(
            hard_action=BoundedAction.SHOW_WORKED_EXAMPLE,
            allowed_actions=(BoundedAction.SHOW_WORKED_EXAMPLE,),
            preferred_fallback=decision,
            next_states={BoundedAction.SHOW_WORKED_EXAMPLE: SessionState.WORKED_EXAMPLE_ACTIVE},
            reasons=("RECALLED_SUCCESSFUL_EPISODE",),
            policy_version=POLICY_VERSION,
        )

    if inputs.requires_unmastered_prerequisite:
        decision = _decision(
            inputs=inputs,
            action=BoundedAction.SWITCH_TO_PREREQUISITE,
            reason_code="PREREQUISITE_BLOCKER",
            reason_text=(
                "The current item needs a prerequisite that has not been "
                "mastered, so practice switches to the prerequisite."
            ),
            payload={"skill": skill, "difficulty": inputs.difficulty},
        )
        return PolicyConstraints(
            hard_action=BoundedAction.SWITCH_TO_PREREQUISITE,
            allowed_actions=(BoundedAction.SWITCH_TO_PREREQUISITE,),
            preferred_fallback=decision,
            next_states={BoundedAction.SWITCH_TO_PREREQUISITE: SessionState.PRACTICE_ADAPTED},
            reasons=("PREREQUISITE_BLOCKER",),
            policy_version=POLICY_VERSION,
        )

    support = should_support(
        mastery=inputs.mastery,
        confidence=inputs.confidence,
        consecutive_errors=inputs.consecutive_errors,
        repeated_misconception=inputs.repeated_misconception,
        requires_unmastered_prerequisite=False,
    )

    repeated_errors = inputs.consecutive_errors >= 2 or (
        inputs.active_misconception and inputs.misconception_observation_count >= 2
    )
    if repeated_errors:
        if inputs.active_misconception:
            decision = _decision(
                inputs=inputs,
                action=BoundedAction.SHOW_WORKED_EXAMPLE,
                reason_code="REPEATED_MISCONCEPTION",
                reason_text=(
                    "Repeated errors map to the same misconception, so a "
                    "worked example isolates the error pattern before more "
                    "practice."
                ),
                payload={
                    "skill": skill,
                    "subskill": inputs.subskill,
                    "misconception": inputs.active_misconception,
                },
            )
        else:
            decision = _decision(
                inputs=inputs,
                action=BoundedAction.SHOW_MICRO_LESSON,
                reason_code="REPEATED_SKILL_ERRORS",
                reason_text=(
                    "Two consecutive errors on the same skill indicate a concept "
                    "gap, so a short lesson is inserted before more practice."
                ),
                payload={"skill": skill, "difficulty": inputs.difficulty},
            )
        return PolicyConstraints(
            hard_action=None,
            allowed_actions=(
                BoundedAction.RETRY_SAME_SKILL,
                BoundedAction.SHOW_WORKED_EXAMPLE,
                BoundedAction.SHOW_MICRO_LESSON,
            ),
            preferred_fallback=decision,
            next_states={
                BoundedAction.RETRY_SAME_SKILL: SessionState.QUESTION_ACTIVE,
                BoundedAction.SHOW_WORKED_EXAMPLE: SessionState.WORKED_EXAMPLE_ACTIVE,
                BoundedAction.SHOW_MICRO_LESSON: SessionState.MICRO_LESSON_ACTIVE,
            },
            reasons=(decision.reason_code,),
            policy_version=POLICY_VERSION,
        )

    if inputs.active_misconception is not None:
        decision = _decision(
            inputs=inputs,
            action=BoundedAction.RETRY_SAME_SKILL,
            reason_code="MISCONCEPTION_OBSERVED",
            reason_text=(
                "The error maps to a known misconception; one more distinct "
                "item on the same skill gathers confirmation evidence."
            ),
            payload={"skill": skill, "difficulty": inputs.difficulty},
        )
        return PolicyConstraints(
            hard_action=None,
            allowed_actions=(BoundedAction.RETRY_SAME_SKILL,),
            preferred_fallback=decision,
            next_states={BoundedAction.RETRY_SAME_SKILL: SessionState.QUESTION_ACTIVE},
            reasons=("MISCONCEPTION_OBSERVED",),
            policy_version=POLICY_VERSION,
        )

    if support:
        if inputs.repeated_misconception:
            decision = _decision(
                inputs=inputs,
                action=BoundedAction.SHOW_WORKED_EXAMPLE,
                reason_code="REPEATED_MISCONCEPTION",
                reason_text="A repeated misconception triggers a worked example.",
                payload={"skill": skill, "difficulty": inputs.difficulty},
            )
        else:
            decision = _decision(
                inputs=inputs,
                action=BoundedAction.LOWER_DIFFICULTY,
                reason_code="SUPPORT_NEEDED",
                reason_text=(
                    "Mastery or streak evidence indicates support is needed, so "
                    "the next item stays on the same skill at a lower difficulty."
                ),
                payload={"skill": skill, "difficulty": max(1, inputs.difficulty - 1)},
                difficulty=max(1, inputs.difficulty - 1),
            )
        return PolicyConstraints(
            hard_action=None,
            allowed_actions=(
                BoundedAction.SHOW_WORKED_EXAMPLE,
                BoundedAction.SHOW_MICRO_LESSON,
                BoundedAction.LOWER_DIFFICULTY,
            ),
            preferred_fallback=decision,
            next_states={
                BoundedAction.SHOW_WORKED_EXAMPLE: SessionState.WORKED_EXAMPLE_ACTIVE,
                BoundedAction.SHOW_MICRO_LESSON: SessionState.MICRO_LESSON_ACTIVE,
                BoundedAction.LOWER_DIFFICULTY: SessionState.PRACTICE_ADAPTED,
            },
            reasons=(decision.reason_code,),
            policy_version=POLICY_VERSION,
        )

    if inputs.correct_streak >= 2:
        promoted = should_promote_difficulty(
            mastery=inputs.mastery,
            confidence=inputs.confidence,
            recent_correct_without_high_hint=inputs.recent_correct_without_high_hint,
            recent_total=inputs.recent_total,
            has_active_high_confidence_misconception=False,
        )
        if promoted:
            decision = _decision(
                inputs=inputs,
                action=BoundedAction.RAISE_DIFFICULTY,
                reason_code="MASTERY_PROMOTION",
                reason_text=(
                    "Correct answers without hints and sufficient confidence "
                    "support a harder item on the same skill."
                ),
                payload={"skill": skill, "difficulty": min(3, inputs.difficulty + 1)},
                difficulty=min(3, inputs.difficulty + 1),
            )
            return PolicyConstraints(
                hard_action=None,
                allowed_actions=(
                    BoundedAction.RETRY_SAME_SKILL,
                    BoundedAction.RAISE_DIFFICULTY,
                ),
                preferred_fallback=decision,
                next_states={
                    BoundedAction.RETRY_SAME_SKILL: SessionState.PRACTICE_ADAPTED,
                    BoundedAction.RAISE_DIFFICULTY: SessionState.PRACTICE_ADAPTED,
                },
                reasons=("MASTERY_PROMOTION",),
                policy_version=POLICY_VERSION,
            )

    decision = _decision(
        inputs=inputs,
        action=BoundedAction.RETRY_SAME_SKILL,
        reason_code="CONTINUE_PRACTICE",
        reason_text="The student is progressing; more evidence is needed before changing difficulty.",
        payload={"skill": skill, "difficulty": inputs.difficulty},
    )
    return PolicyConstraints(
        hard_action=None,
        allowed_actions=(BoundedAction.RETRY_SAME_SKILL,),
        preferred_fallback=decision,
        next_states={BoundedAction.RETRY_SAME_SKILL: SessionState.PRACTICE_ADAPTED},
        reasons=("CONTINUE_PRACTICE",),
        policy_version=POLICY_VERSION,
    )


def decide_next_action(inputs: PolicyInput) -> PolicyResult:
    """Backward-compatible next-action selection.

    Delegates to the shared constraints derivation; output trajectory is
    identical to the historical deterministic policy (H1 parity contract).
    """
    constraints = derive_policy_constraints(inputs)
    return constraints.preferred_fallback_as_result()


def hint_decision(item_hint_count: int, hints_used: int) -> AgentDecision:
    level = min(3, hints_used + 1)
    return AgentDecision(
        action=f"GIVE_HINT_{level}",
        action_payload={"level": level},
        reason_code="HINT_REQUESTED",
        reason_text=f"Student requested hint level {level}.",
        policy_version=POLICY_VERSION,
    )
