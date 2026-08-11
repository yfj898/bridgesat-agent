"""Runtime wiring proof for each newly published SAT Math skill."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agent.policy import PolicyInput, decide_next_action
from app.content_pipeline.packaging import PACK_VERSION
from app.domain.memory import BoundedAction
from app.sync.versioned_scoring import PackAnswerKey

ROOT = Path(__file__).resolve().parents[1]
PACK_DIR = ROOT / "content" / "packs" / f"bridgesat-math-{PACK_VERSION}"
NEW_SKILLS = (
    "inequalities",
    "quadratic_equations",
    "exponents_radicals",
    "coordinate_geometry",
)


def _items() -> list[dict]:
    return [
        json.loads(line)
        for line in (PACK_DIR / "items.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.mark.parametrize("skill", NEW_SKILLS)
def test_wrong_choice_reaches_targeted_intervention_and_transfer_item(skill: str) -> None:
    items = _items()
    trigger = next(
        item
        for item in items
        if item["target_skill"] == skill
        and item["author_metadata"].get("instruction_role") == "trigger"
    )
    transfer_group = trigger["author_metadata"]["transfer_group"]
    transfer = next(
        item
        for item in items
        if item["target_skill"] == skill
        and item["author_metadata"].get("transfer_group") == transfer_group
        and item["author_metadata"].get("instruction_role") == "transfer"
    )
    answer_key = PackAnswerKey(PACK_VERSION, PACK_DIR)
    default_lesson = answer_key.worked_example_meta(skill)
    misconception = next(
        value
        for value in trigger["misconception_map"].values()
        if value in default_lesson["target_misconceptions"]
    )

    decision = decide_next_action(
        PolicyInput(
            student_id="student-content-proof",
            session_id="session-content-proof",
            skill=skill,
            subskill=trigger["target_subskill"],
            difficulty=trigger["difficulty"],
            consecutive_errors=2,
            repeated_misconception=True,
            active_misconception=misconception,
            misconception_observation_count=2,
            misconception_distinct_items=2,
        )
    ).decision

    assert decision.action == BoundedAction.SHOW_WORKED_EXAMPLE.value
    lesson = answer_key.worked_example_meta(skill, misconception)
    assert lesson is not None
    assert lesson["review_status"] == "approved"
    assert misconception in lesson["target_misconceptions"]
    assert transfer["id"] != trigger["id"]
    assert transfer["prompt"] != trigger["prompt"]
