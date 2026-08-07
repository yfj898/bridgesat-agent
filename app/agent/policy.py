from __future__ import annotations

from dataclasses import dataclass, field

from app.domain.events import AgentDecision
from app.domain.learner import should_promote_difficulty, should_support
from app.domain.memory import BoundedAction
from app.domain.sessions import SessionState

POLICY_VERSION = "policy-0.1.0"


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


def decide_next_action(inputs: PolicyInput) -> PolicyResult:
    """Bounded next-action selection.

    Order of checks:
    1. time budget closure (END_WITH_REVIEW / SCHEDULE_REVIEW)
    2. memory-aware early reuse: validated successful episode for the same
       skill+misconception → SHOW_WORKED_EXAMPLE before the second error
       (reason code RECALLED_SUCCESSFUL_EPISODE)
    3. support conditions (consecutive errors, repeated misconception, low
       mastery, prerequisite) → SHOW_WORKED_EXAMPLE / SHOW_MICRO_LESSON /
       SWITCH_TO_PREREQUISITE
    4. promotion conditions → RAISE_DIFFICULTY
    5. default → RETRY_SAME_SKILL / ASK_QUESTION
    """
    skill = inputs.skill

    if inputs.minutes_remaining <= 2:
        return PolicyResult(
            decision=AgentDecision(
                action=BoundedAction.END_WITH_REVIEW.value,
                action_payload={"review": "time_budget"},
                reason_code="TIME_BUDGET_EXHAUSTED",
                reason_text=(
                    "Only a few minutes remain, so the agent closes with a short "
                    "review instead of starting a new item."
                ),
                target_skill=skill,
                difficulty=inputs.difficulty,
                policy_version=POLICY_VERSION,
            ),
            next_state=SessionState.SESSION_SUMMARY,
        )

    if inputs.recalled_successful_episode:
        return PolicyResult(
            decision=AgentDecision(
                action=BoundedAction.SHOW_WORKED_EXAMPLE.value,
                action_payload={
                    "skill": skill,
                    "subskill": inputs.subskill,
                    "misconception": inputs.active_misconception,
                },
                reason_code="RECALLED_SUCCESSFUL_EPISODE",
                reason_text=(
                    "A validated episode from earlier practice shows that a worked "
                    "example succeeded for this same error; reusing it before more "
                    "practice."
                ),
                target_skill=skill,
                difficulty=inputs.difficulty,
                episode_ids=list(inputs.recalled_episode_ids),
                policy_version=POLICY_VERSION,
            ),
            next_state=SessionState.WORKED_EXAMPLE_ACTIVE,
        )

    if inputs.requires_unmastered_prerequisite:
        return PolicyResult(
            decision=AgentDecision(
                action=BoundedAction.SWITCH_TO_PREREQUISITE.value,
                action_payload={"skill": skill, "difficulty": inputs.difficulty},
                reason_code="PREREQUISITE_BLOCKER",
                reason_text=(
                    "The current item needs a prerequisite that has not been "
                    "mastered, so practice switches to the prerequisite."
                ),
                target_skill=skill,
                difficulty=inputs.difficulty,
                policy_version=POLICY_VERSION,
            ),
            next_state=SessionState.PRACTICE_ADAPTED,
        )

    support = should_support(
        mastery=inputs.mastery,
        confidence=inputs.confidence,
        consecutive_errors=inputs.consecutive_errors,
        repeated_misconception=inputs.repeated_misconception,
        requires_unmastered_prerequisite=False,
    )

    if inputs.consecutive_errors >= 2 or (inputs.active_misconception and inputs.misconception_observation_count >= 2):
        if inputs.active_misconception:
            return PolicyResult(
                decision=AgentDecision(
                    action=BoundedAction.SHOW_WORKED_EXAMPLE.value,
                    action_payload={
                        "skill": skill,
                        "subskill": inputs.subskill,
                        "misconception": inputs.active_misconception,
                    },
                    reason_code="REPEATED_MISCONCEPTION",
                    reason_text=(
                        "Repeated errors map to the same misconception, so a "
                        "worked example isolates the error pattern before more "
                        "practice."
                    ),
                    target_skill=skill,
                    difficulty=inputs.difficulty,
                    policy_version=POLICY_VERSION,
                ),
                next_state=SessionState.WORKED_EXAMPLE_ACTIVE,
            )
        return PolicyResult(
            decision=AgentDecision(
                action=BoundedAction.SHOW_MICRO_LESSON.value,
                action_payload={"skill": skill, "difficulty": inputs.difficulty},
                reason_code="REPEATED_SKILL_ERRORS",
                reason_text=(
                    "Two consecutive errors on the same skill indicate a concept "
                    "gap, so a short lesson is inserted before more practice."
                ),
                target_skill=skill,
                difficulty=inputs.difficulty,
                policy_version=POLICY_VERSION,
            ),
            next_state=SessionState.MICRO_LESSON_ACTIVE,
        )

    if inputs.active_misconception is not None:
        return PolicyResult(
            decision=AgentDecision(
                action=BoundedAction.RETRY_SAME_SKILL.value,
                action_payload={"skill": skill, "difficulty": inputs.difficulty},
                reason_code="MISCONCEPTION_OBSERVED",
                reason_text=(
                    "The error maps to a known misconception; one more distinct "
                    "item on the same skill gathers confirmation evidence."
                ),
                target_skill=skill,
                difficulty=inputs.difficulty,
                policy_version=POLICY_VERSION,
            ),
            next_state=SessionState.QUESTION_ACTIVE,
        )

    if support:
        if inputs.repeated_misconception:
            return PolicyResult(
                decision=AgentDecision(
                    action=BoundedAction.SHOW_WORKED_EXAMPLE.value,
                    action_payload={"skill": skill, "difficulty": inputs.difficulty},
                    reason_code="REPEATED_MISCONCEPTION",
                    reason_text="A repeated misconception triggers a worked example.",
                    target_skill=skill,
                    difficulty=inputs.difficulty,
                    policy_version=POLICY_VERSION,
                ),
                next_state=SessionState.WORKED_EXAMPLE_ACTIVE,
            )
        return PolicyResult(
            decision=AgentDecision(
                action=BoundedAction.LOWER_DIFFICULTY.value,
                action_payload={"skill": skill, "difficulty": max(1, inputs.difficulty - 1)},
                reason_code="SUPPORT_NEEDED",
                reason_text=(
                    "Mastery or streak evidence indicates support is needed, so "
                    "the next item stays on the same skill at a lower difficulty."
                ),
                target_skill=skill,
                difficulty=max(1, inputs.difficulty - 1),
                policy_version=POLICY_VERSION,
            ),
            next_state=SessionState.PRACTICE_ADAPTED,
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
            return PolicyResult(
                decision=AgentDecision(
                    action=BoundedAction.RAISE_DIFFICULTY.value,
                    action_payload={"skill": skill, "difficulty": min(3, inputs.difficulty + 1)},
                    reason_code="MASTERY_PROMOTION",
                    reason_text=(
                        "Correct answers without hints and sufficient confidence "
                        "support a harder item on the same skill."
                    ),
                    target_skill=skill,
                    difficulty=min(3, inputs.difficulty + 1),
                    policy_version=POLICY_VERSION,
                ),
                next_state=SessionState.PRACTICE_ADAPTED,
            )

    return PolicyResult(
        decision=AgentDecision(
            action=BoundedAction.RETRY_SAME_SKILL.value,
            action_payload={"skill": skill, "difficulty": inputs.difficulty},
            reason_code="CONTINUE_PRACTICE",
            reason_text="The student is progressing; more evidence is needed before changing difficulty.",
            target_skill=skill,
            difficulty=inputs.difficulty,
            policy_version=POLICY_VERSION,
        ),
        next_state=SessionState.PRACTICE_ADAPTED,
    )


def hint_decision(item_hint_count: int, hints_used: int) -> AgentDecision:
    level = min(3, hints_used + 1)
    return AgentDecision(
        action=f"GIVE_HINT_{level}",
        action_payload={"level": level},
        reason_code="HINT_REQUESTED",
        reason_text=f"Student requested hint level {level}.",
        policy_version=POLICY_VERSION,
    )
