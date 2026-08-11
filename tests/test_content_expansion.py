"""Public-contract tests for the competition content expansion pack."""

from __future__ import annotations

from collections import Counter, defaultdict

import pytest

from app.content_pipeline.contracts import MISCONCEPTIONS, SKILLS, content_hash
from app.content_pipeline.expansion import expansion_manifest_rows
from app.content_pipeline.generation import _choices, generate_item, generate_lessons, rng_for
from app.content_pipeline.validation import validate_all


NEW_SKILLS = {
    "inequalities",
    "quadratic_equations",
    "exponents_radicals",
    "coordinate_geometry",
}


def _expanded_items() -> list[dict]:
    return [generate_item(row) for row in expansion_manifest_rows()]


def test_expansion_manifest_adds_four_first_party_skills() -> None:
    rows = expansion_manifest_rows()

    assert len(rows) == 48
    assert Counter(row["target_skill"] for row in rows) == {
        skill: 12 for skill in NEW_SKILLS
    }
    assert NEW_SKILLS <= set(SKILLS)
    assert all(row["source_id"] == "bridgesat_original" for row in rows)
    assert all(row["content_id"].startswith("math.") for row in rows)


def test_each_new_skill_has_required_difficulty_and_misconception_coverage() -> None:
    items = _expanded_items()
    by_skill: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        by_skill[item["target_skill"]].append(item)

    assert set(by_skill) == NEW_SKILLS
    for skill, skill_items in by_skill.items():
        assert Counter(item["difficulty"] for item in skill_items) == {1: 4, 2: 5, 3: 3}
        misconceptions = {
            misconception
            for item in skill_items
            for misconception in item["misconception_map"].values()
        }
        assert len(misconceptions) >= 2, skill
        assert misconceptions <= set(MISCONCEPTIONS)


def test_expansion_answer_labels_are_distributed_and_choices_have_no_placeholders() -> None:
    items = _expanded_items()
    answer_labels = Counter(item["answer_choice_id"] for item in items)

    assert set(answer_labels) == {"A", "B", "C", "D"}
    assert min(answer_labels.values()) >= 6
    assert all(
        "?" not in str(choice["text"])
        for item in items
        for choice in item["choices"]
    )


def test_expansion_has_explicit_trigger_and_transfer_paths() -> None:
    roles_by_path: dict[tuple[str, str], set[str]] = defaultdict(set)
    for item in _expanded_items():
        metadata = item["author_metadata"]
        transfer_group = metadata.get("transfer_group")
        instruction_role = metadata.get("instruction_role")
        if transfer_group and instruction_role:
            roles_by_path[(item["target_skill"], transfer_group)].add(instruction_role)

    for skill in NEW_SKILLS:
        assert any(
            path_skill == skill and {"trigger", "transfer"} <= roles
            for (path_skill, _), roles in roles_by_path.items()
        ), skill


def test_new_skill_misconceptions_have_exact_worked_example_coverage() -> None:
    items = _expanded_items()
    lessons = generate_lessons(expansion_manifest_rows())
    for skill in NEW_SKILLS:
        used = {
            misconception
            for item in items
            if item["target_skill"] == skill
            for misconception in item["misconception_map"].values()
        }
        worked_targets = {
            misconception
            for lesson in lessons
            if lesson["target_skill"] == skill
            and lesson["content_type"] == "worked_example"
            for misconception in lesson["target_misconceptions"]
        }
        assert used <= worked_targets, skill


def test_negative_exponent_items_use_exponent_rule_subskill() -> None:
    items = {
        item["id"]: item
        for item in _expanded_items()
        if item["target_skill"] == "exponents_radicals"
    }

    assert all(
        items[f"math.exponents_radicals.{number:03d}"]["target_subskill"]
        == "apply_exponent_rules"
        for number in range(10, 13)
    )


def test_choice_collision_is_an_authoring_error_not_an_automatic_shift() -> None:
    with pytest.raises(ValueError, match="choice collision"):
        _choices(
            4,
            [4, 5, 6],
            ["arithmetic_error", "sign_error", "inverse_operation_error"],
            rng_for("collision-test"),
        )


def test_expansion_questions_and_lessons_pass_formal_validation() -> None:
    rows = expansion_manifest_rows()
    items = [generate_item(row) for row in rows]
    lessons = generate_lessons(rows)

    assert len(lessons) == 8
    assert Counter(lesson["content_type"] for lesson in lessons) == {
        "micro_lesson": 4,
        "worked_example": 4,
    }
    assert all(lesson["target_misconceptions"] for lesson in lessons)
    assert validate_all(items, lessons) == {}


def test_expansion_answer_verification_recomputes_from_source_parameters() -> None:
    item = _expanded_items()[0]
    item["author_metadata"]["params"]["rhs"] += 100
    item["content_hash"] = content_hash(item)

    errors = validate_all([item])

    assert item["id"] in errors
    assert any("expansion formula" in error for error in errors[item["id"]])
