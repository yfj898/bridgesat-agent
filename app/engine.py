from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from .models import (
    AdaptRequest,
    AdaptResponse,
    DiagnosticAnswer,
    DiagnosticResponse,
    PlanItem,
    Skill,
    Student,
)
from .question_bank import question_map


DEFAULT_MASTERY = 0.5

ADAPT_ACTIONS = (
    "increase_difficulty",
    "continue_practice",
    "decrease_difficulty",
    "insert_micro_lesson",
    "end_with_review",
)

ADAPT_DELTA = {
    "increase_difficulty": 1,
    "decrease_difficulty": -1,
    "insert_micro_lesson": -1,
    "continue_practice": 0,
    "end_with_review": 0,
}


def _clamp(value: float) -> float:
    return round(max(0.05, min(0.95, value)), 3)


def _adapt_with_llm(
    previous_mastery: float, request: AdaptRequest, llm: Any
) -> tuple[str, str] | None:
    """Ask the LLM for the next adapt action; None means fall back.

    The LLM answers inside the AdaptResponse action domain. Returns
    (action, reason) only when the action is a legal value, so an LLM can
    never steer the session outside the bounded set.
    """
    prompt = (
        "You are the adapt policy for an SAT math tutor. Decide the next "
        "action from this exact set: "
        + ", ".join(ADAPT_ACTIONS)
        + ". State: "
        + json.dumps(
            {
                "skill": request.skill.value,
                "mastery": round(previous_mastery, 3),
                "was_correct": request.was_correct,
                "hint_level": request.hint_level,
                "consecutive_skill_errors": request.consecutive_skill_errors,
                "minutes_remaining": request.minutes_remaining,
            },
            sort_keys=True,
        )
        + '. Respond with JSON only: {"action": "<ACTION>", '
        '"reason_code": "<UPPER_SNAKE>", "reason_text": "<short explanation>"}.'
    )
    try:
        content = llm.complete(prompt, max_tokens=120, temperature=0.0)
        if hasattr(content, "__await__"):
            from .infrastructure.async_utils import await_in_any_context

            content = await_in_any_context(content)
    except Exception:
        return None
    if not content:
        return None
    try:
        parsed = json.loads(content.strip())
    except (ValueError, AttributeError):
        return None
    action = parsed.get("action")
    if action not in ADAPT_ACTIONS:
        return None
    reason = str(parsed.get("reason_text") or "LLM-selected next action.")
    return action, reason


def score_diagnostic(
    student: Student,
    answers: list[DiagnosticAnswer],
) -> DiagnosticResponse:
    questions = question_map()
    deltas: dict[Skill, list[float]] = defaultdict(list)

    for answer in answers:
        question = questions.get(answer.question_id)
        if question is None:
            raise ValueError(f"Unknown question_id: {answer.question_id}")

        correct = answer.selected_answer == question.answer
        delta = 0.12 if correct else -0.16
        delta -= answer.hint_level * 0.025
        deltas[question.skill].append(delta)

    mastery = dict(student.mastery)
    for skill in Skill:
        previous = mastery.get(skill, DEFAULT_MASTERY)
        updates = deltas.get(skill, [])
        if updates:
            mastery[skill] = _clamp(previous + sum(updates) / len(updates))
        else:
            mastery[skill] = previous

    weakest = sorted(Skill, key=lambda skill: mastery[skill])[:2]
    plan = build_plan(weakest, student.daily_minutes)
    explanation = (
        f"I prioritized {weakest[0].value.replace('_', ' ')} because it has the "
        f"lowest estimated mastery ({mastery[weakest[0]]:.0%}). The plan keeps "
        f"the total session within {student.daily_minutes} minutes."
    )
    return DiagnosticResponse(
        student_id=student.id,
        mastery=mastery,
        weakest_skills=weakest,
        plan=plan,
        agent_explanation=explanation,
    )


def build_plan(weakest: list[Skill], daily_minutes: int) -> list[PlanItem]:
    primary = weakest[0]
    secondary = weakest[1] if len(weakest) > 1 else weakest[0]

    lesson_minutes = max(2, round(daily_minutes * 0.2))
    practice_minutes = max(2, round(daily_minutes * 0.45))
    review_minutes = max(1, round(daily_minutes * 0.2))
    reflection_minutes = max(1, daily_minutes - lesson_minutes - practice_minutes - review_minutes)

    return [
        PlanItem(
            activity="micro_lesson",
            skill=primary,
            minutes=lesson_minutes,
            reason="Build the missing concept before harder practice.",
        ),
        PlanItem(
            activity="practice",
            skill=primary,
            minutes=practice_minutes,
            reason="Use targeted questions on the weakest skill.",
        ),
        PlanItem(
            activity="review",
            skill=secondary,
            minutes=review_minutes,
            reason="Prevent the second-weakest skill from being ignored.",
        ),
        PlanItem(
            activity="reflection",
            skill=primary,
            minutes=reflection_minutes,
            reason="Record the error pattern and choose the next session focus.",
        ),
    ]


def adapt(
    previous_mastery: float,
    request: AdaptRequest,
    llm: Any | None = None,
) -> AdaptResponse:
    """Dual-mode next-action selection for /v1/adapt.

    The mastery update is always computed deterministically below — the LLM
    never touches the numbers. With an LLM attached, the next action is asked
    inside the AdaptResponse action domain and used only when it is a legal
    value; any failure, non-JSON output, or unknown action falls back to the
    deterministic branches. Without an LLM the behavior is byte-identical to
    the deterministic policy.
    """
    if request.was_correct:
        updated = _clamp(previous_mastery + 0.07 - request.hint_level * 0.02)
    else:
        updated = _clamp(previous_mastery - 0.09 - request.hint_level * 0.01)

    if request.minutes_remaining <= 2:
        return AdaptResponse(
            action="end_with_review",
            mastery=updated,
            reason="Only a few minutes remain, so the agent closes with a short review instead of starting a new concept.",
            next_difficulty_delta=0,
        )

    if llm is not None:
        llm_action = _adapt_with_llm(previous_mastery, request, llm)
        if llm_action is not None:
            return AdaptResponse(
                action=llm_action[0],
                mastery=updated,
                reason=llm_action[1],
                next_difficulty_delta=ADAPT_DELTA[llm_action[0]],
            )

    if request.consecutive_skill_errors >= 2:
        return AdaptResponse(
            action="insert_micro_lesson",
            mastery=updated,
            reason="Repeated errors on the same skill indicate a concept gap, so a short lesson is inserted before more practice.",
            next_difficulty_delta=-1,
        )

    if request.was_correct and request.hint_level == 0 and updated >= 0.7:
        return AdaptResponse(
            action="increase_difficulty",
            mastery=updated,
            reason="The answer was correct without hints and mastery is strong enough for a harder item.",
            next_difficulty_delta=1,
        )

    if not request.was_correct:
        return AdaptResponse(
            action="decrease_difficulty",
            mastery=updated,
            reason="The answer was incorrect, so the next item should isolate the same skill at a lower difficulty.",
            next_difficulty_delta=-1,
        )

    return AdaptResponse(
        action="continue_practice",
        mastery=updated,
        reason="The student is progressing, but more evidence is needed before changing difficulty.",
        next_difficulty_delta=0,
    )
