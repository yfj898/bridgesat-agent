"""engine.adapt dual-mode tests (LLM decision at the route layer).

adapt() takes an optional LLM client: with one injected, the next action is
decided by the LLM inside the AdaptResponse action domain; on any failure,
unparseable output, or unknown action, the deterministic branches decide.
Without a client the behavior is byte-identical to the deterministic policy.
The mastery update is always computed deterministically — the LLM never
touches the numbers.
"""

from __future__ import annotations

import json
from typing import Any

from app.engine import adapt
from app.models import AdaptRequest, Skill

DETERMINISTIC_ACTIONS = {
    "increase_difficulty",
    "continue_practice",
    "decrease_difficulty",
    "insert_micro_lesson",
    "end_with_review",
}


class StubLLM:
    def __init__(self, content: str | None, *, fail: bool = False) -> None:
        self.content = content
        self.fail = fail
        self.prompts: list[str] = []

    async def complete(self, prompt: str, **kwargs: Any) -> str:
        self.prompts.append(prompt)
        if self.fail:
            from app.agent.llm_client import LLMUnavailableError

            raise LLMUnavailableError("stub down")
        return self.content or ""


def _request(**overrides: Any) -> AdaptRequest:
    base: dict[str, Any] = {
        "skill": Skill.LINEAR_EQUATIONS,
        "was_correct": False,
        "hint_level": 0,
        "consecutive_skill_errors": 0,
        "minutes_remaining": 20,
    }
    base.update(overrides)
    return AdaptRequest(**base)


def test_without_llm_unchanged() -> None:
    result = adapt(0.5, _request())
    assert result.action == "decrease_difficulty"
    assert result.mastery == round(0.5 - 0.09, 3)


def test_llm_decides_action_and_reason() -> None:
    llm = StubLLM(
        json.dumps(
            {
                "action": "insert_micro_lesson",
                "reason_code": "LLM_CONCEPT_GAP",
                "reason_text": "student misapplied inverse operations",
            }
        )
    )
    result = adapt(0.5, _request(), llm=llm)
    assert result.action == "insert_micro_lesson"
    assert result.reason == "student misapplied inverse operations"
    assert result.next_difficulty_delta == -1
    assert result.mastery == round(0.5 - 0.09, 3)
    assert len(llm.prompts) == 1
    assert "linear_equations" in llm.prompts[0]
    assert "insert_micro_lesson" in llm.prompts[0]


def test_llm_end_with_review_maps_delta_zero() -> None:
    llm = StubLLM(
        json.dumps(
            {
                "action": "end_with_review",
                "reason_code": "LLM_TIME",
                "reason_text": "session should close with review",
            }
        )
    )
    result = adapt(0.5, _request(), llm=llm)
    assert result.action == "end_with_review"
    assert result.next_difficulty_delta == 0


def test_llm_failure_falls_back_to_policy() -> None:
    llm = StubLLM(None, fail=True)
    result = adapt(0.5, _request(), llm=llm)
    assert result.action == "decrease_difficulty"


def test_llm_garbage_falls_back_to_policy() -> None:
    llm = StubLLM("definitely not json")
    result = adapt(0.5, _request(), llm=llm)
    assert result.action == "decrease_difficulty"


def test_llm_unknown_action_falls_back_to_policy() -> None:
    llm = StubLLM(
        json.dumps({"action": "DELETE_ALL_MEMORY", "reason_code": "X", "reason_text": "y"})
    )
    result = adapt(0.5, _request(), llm=llm)
    assert result.action == "decrease_difficulty"


def test_llm_does_not_touch_mastery_numbers() -> None:
    llm = StubLLM(
        json.dumps(
            {
                "action": "continue_practice",
                "reason_code": "LLM_PROGRESS",
                "reason_text": "keep going",
            }
        )
    )
    result = adapt(0.5, _request(was_correct=True, hint_level=1), llm=llm)
    assert result.mastery == round(0.5 + 0.07 - 1 * 0.02, 3)


def test_llm_action_still_obeys_minutes_guard() -> None:
    """The deterministic time guard is a floor even when the LLM is up:
    end_with_review is the only legal action with <= 2 minutes left, so a
    non-review LLM action is discarded and the guard wins."""
    llm = StubLLM(
        json.dumps(
            {
                "action": "increase_difficulty",
                "reason_code": "LLM_STRONG",
                "reason_text": "student is strong",
            }
        )
    )
    result = adapt(0.5, _request(was_correct=True, minutes_remaining=1), llm=llm)
    assert result.action == "end_with_review"
