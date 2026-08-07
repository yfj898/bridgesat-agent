"""Content pipeline tests: selection, schema, unique answers, four choices,
hash, reviewer, lineage, rewrite gate, and approval blocking."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.content_pipeline.contracts import (
    APPROVED_DIR,
    CANDIDATES_DIR,
    DRAFTS_DIR,
    MISCONCEPTIONS,
    PACKS_DIR,
    REVIEWS_DIR,
    SCHEMA_VERSION,
    VALIDATED_DIR,
)
from app.content_pipeline.generation import generate_all_drafts, generate_item
from app.content_pipeline.importing import import_pack, verify_import
from app.content_pipeline.packaging import (
    ApprovalBlockedError,
    approve_items,
    build_pack,
    read_reviews,
    verify_pack_hashes,
    write_review_template,
)
from app.content_pipeline.selection import (
    build_manifest_row,
    load_manifest,
    select_math_candidates,
    verify_selection_counts,
    write_immutable_manifest,
)
from app.content_pipeline.validation import (
    rewrite_similarity,
    validate_all,
    validate_item,
)

REVIEWED = Path("data/reviewed/routes/ready_for_rewrite.jsonl")


@pytest.fixture()
def selected() -> list[dict]:
    candidates = select_math_candidates()
    counts: dict[str, int] = {}
    for row in candidates:
        skill = row["skill_mapping"]["primary_skill"]
        counts[skill] = counts.get(skill, 0) + 1
    sequence: dict[str, int] = {}
    rows = []
    for candidate in candidates:
        skill = candidate["skill_mapping"]["primary_skill"]
        sequence[skill] = sequence.get(skill, 0) + 1
        rows.append(build_manifest_row(candidate, sequence[skill], counts[skill]))
    verify_selection_counts(rows)
    return rows


@pytest.fixture()
def drafts(selected: list[dict]) -> tuple[list[dict], list[dict]]:
    items = [generate_item(row) for row in selected]
    lessons = []
    from app.content_pipeline.generation import generate_lessons

    lessons = generate_lessons(selected, out_dir=Path("/tmp/opencode/bridgesat-lessons"))
    return items, lessons


# --- selection -----------------------------------------------------------


def test_selection_is_exactly_55_math_candidates(selected: list[dict]) -> None:
    assert len(selected) == 55
    counts: dict[str, int] = {}
    for row in selected:
        counts[row["target_skill"]] = counts.get(row["target_skill"], 0) + 1
    assert counts == {
        "linear_equations": 12,
        "systems_equations": 12,
        "ratios_percentages": 13,
        "functions_models": 18,
    }


def test_selection_filters_apply() -> None:
    rows = select_math_candidates()
    for row in rows:
        assert row["source_id"] == "deepmind_mathematics_dataset"
        assert row["review_route"] == "ready_for_rewrite"
        assert row["skill_mapping"]["role"] == "question_candidate"
        assert row["skill_mapping"]["mapping_confidence"] == 1.0


def test_selection_manifest_is_immutable(tmp_path: Path) -> None:
    candidates = select_math_candidates()
    counts = {
        "linear_equations": 12,
        "systems_equations": 12,
        "ratios_percentages": 13,
        "functions_models": 18,
    }
    sequence: dict[str, int] = {}
    rows = []
    for candidate in candidates:
        skill = candidate["skill_mapping"]["primary_skill"]
        sequence[skill] = sequence.get(skill, 0) + 1
        rows.append(build_manifest_row(candidate, sequence[skill], counts[skill]))

    path = write_immutable_manifest(rows, tmp_path)
    assert path.exists()

    mutated = [dict(rows[0], content_id="math.linear_equations.099")] + rows[1:]
    with pytest.raises(FileExistsError):
        write_immutable_manifest(mutated, tmp_path)

    write_immutable_manifest(rows, tmp_path)
    assert (tmp_path / "math-selection-v1.checksum").exists()


# --- draft generation ----------------------------------------------------


def test_drafts_are_deterministic_per_lineage(selected: list[dict]) -> None:
    first = generate_item(selected[0])
    second = generate_item(selected[0])
    assert first == second
    assert first["id"] == selected[0]["content_id"]


def test_drafts_never_reuse_candidate_question(selected: list[dict]) -> None:
    candidates = select_math_candidates()
    original_by_id = {row["id"]: row.get("question", "") for row in candidates}
    for row in selected:
        item = generate_item(row)
        original = original_by_id[row["lineage_id"]]
        similarity = rewrite_similarity(original, item["prompt"])
        assert similarity < 0.3, f"{item['id']} too similar to candidate: {similarity}"


def test_every_draft_has_four_choices_one_correct(drafts: tuple[list[dict], list[dict]]) -> None:
    items, _ = drafts
    for item in items:
        assert len(item["choices"]) == 4
        answer = item["answer_choice_id"]
        distractors = [c["id"] for c in item["choices"] if c["id"] != answer]
        assert set(item["misconception_map"].keys()) == set(distractors)


def test_all_distractors_map_to_known_misconceptions(drafts: tuple[list[dict], list[dict]]) -> None:
    items, _ = drafts
    for item in items:
        for misconception in item["misconception_map"].values():
            assert misconception in MISCONCEPTIONS


def test_each_skill_has_at_least_two_lessons_of_each_kind(drafts: tuple[list[dict], list[dict]]) -> None:
    _, lessons = drafts
    skills = sorted({lesson["target_skill"] for lesson in lessons})
    assert skills == ["functions_models", "linear_equations", "ratios_percentages", "systems_equations"]
    for skill in skills:
        kinds = {
            kind: sum(
                1
                for lesson in lessons
                if lesson["target_skill"] == skill and lesson["content_type"] == kind
            )
            for kind in ("micro_lesson", "worked_example")
        }
        assert kinds["micro_lesson"] >= 2, skill
        assert kinds["worked_example"] >= 2, skill


# --- validation ----------------------------------------------------------


def test_all_drafts_pass_validation(drafts: tuple[list[dict], list[dict]]) -> None:
    items, lessons = drafts
    report = validate_all(items, lessons)
    assert report == {}, report


def test_correct_answer_is_exactly_verified(drafts: tuple[list[dict], list[dict]]) -> None:
    from sympy import Rational

    items, _ = drafts
    for item in items:
        metadata = item["author_metadata"]
        answer = next(
            c["text"] for c in item["choices"] if c["id"] == item["answer_choice_id"]
        )
        if metadata["kind"] in ("expression", "evaluate"):
            from sympy import sympify

            if metadata["kind"] == "evaluate":
                value = sympify(metadata["expression"]).subs(
                    sympify(metadata["variable"]), Rational(metadata["at"])
                )
            else:
                value = sympify(metadata["expression"])
            assert str(Rational(value)) == str(Rational(answer))


def test_duplicate_choice_text_rejected(drafts: tuple[list[dict], list[dict]]) -> None:
    item = dict(drafts[0][0])
    item["choices"][1] = dict(item["choices"][0])
    errors = validate_item(item)
    assert any("distinct" in error for error in errors)


def test_tampered_hash_rejected(drafts: tuple[list[dict], list[dict]]) -> None:
    item = dict(drafts[0][0])
    item["content_hash"] = "sha256:" + "0" * 64
    errors = validate_item(item)
    assert any("content_hash" in error for error in errors)


def test_rewrite_similarity_gate_blocks_copies() -> None:
    assert rewrite_similarity("solve 2x + 3 = 7 for x", "solve 2x + 3 = 7 for x") == 1.0
    assert rewrite_similarity("completely unrelated text here", "totally different wording now") < 0.5


def test_unmapped_distractor_rejected(drafts: tuple[list[dict], list[dict]]) -> None:
    item = dict(drafts[0][0])
    item["misconception_map"] = dict(item["misconception_map"])
    first = next(iter(item["misconception_map"]))
    item["misconception_map"][first] = "not_a_real_misconception"
    errors = validate_item(item)
    assert any("unmapped misconception" in error for error in errors)


def test_misconception_map_must_cover_exactly_distractors(drafts: tuple[list[dict], list[dict]]) -> None:
    item = dict(drafts[0][0])
    item["misconception_map"] = {}
    errors = validate_item(item)
    assert any("exactly the three distractors" in error for error in errors)


def test_answers_are_unique_choices_texts(drafts: tuple[list[dict], list[dict]]) -> None:
    items, _ = drafts
    for item in items:
        texts = [c["text"] for c in item["choices"]]
        assert len(set(texts)) == 4


# --- reviewer and approval gate ------------------------------------------


def test_review_template_and_approval(drafts: tuple[list[dict], list[dict]], tmp_path: Path) -> None:
    items, lessons = drafts
    template = tmp_path / "reviews.csv"
    write_review_template(items, template)
    reviews = read_reviews(template)
    assert set(reviews.keys()) == {item["id"] for item in items}
    assert approve_items(items, reviews) == []

    filled = tmp_path / "filled.csv"
    write_review_template(items, filled)
    rows = read_reviews(filled)
    for row in rows.values():
        row.update(
            {
                "educational_reviewer": "edu@example.com",
                "answer_reviewer": "ans@example.com",
                "license_reviewer": "lic@example.com",
                "accessibility_reviewer": "acc@example.com",
                "reviewed_at": "2026-08-07T00:00:00Z",
                "conclusion": "approved",
                "notes": "ok",
                "source_lineage_confirmed": "yes",
                "release_batch": "batch-1",
            }
        )
    with filled.open("w", encoding="utf-8", newline="") as handle:
        import csv

        writer = csv.DictWriter(handle, fieldnames=list(rows[items[0]["id"]].keys()))
        writer.writeheader()
        writer.writerows(rows.values())

    approved = approve_items(items, read_reviews(filled))
    assert len(approved) == len(items)
    assert all(item["review_status"] == "approved" for item in approved)


def test_approval_blocked_without_license_reviewer(
    drafts: tuple[list[dict], list[dict]], tmp_path: Path
) -> None:
    items, _ = drafts
    filled = tmp_path / "reviews.csv"
    write_review_template(items, filled)
    rows = read_reviews(filled)
    for i, row in enumerate(rows.values()):
        row.update(
            {
                "educational_reviewer": "edu@example.com",
                "answer_reviewer": "ans@example.com",
                "license_reviewer": "lic@example.com" if i else "",
                "accessibility_reviewer": "acc@example.com",
                "reviewed_at": "2026-08-07T00:00:00Z",
                "conclusion": "approved",
                "notes": "ok",
                "source_lineage_confirmed": "yes",
                "release_batch": "batch-1",
            }
        )
    import csv

    with filled.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[items[0]["id"]].keys()))
        writer.writeheader()
        writer.writerows(rows.values())
    approved = approve_items(items, read_reviews(filled))
    assert len(approved) == len(items) - 1


def test_build_pack_rejects_unapproved(tmp_path: Path) -> None:
    item = {
        "id": "math.linear_equations.001",
        "version": 1,
        "schema_version": SCHEMA_VERSION,
        "domain": "math",
        "content_type": "question",
        "target_skill": "linear_equations",
        "target_subskill": "isolate_variables",
        "required_prerequisites": ["integer_operations"],
        "difficulty": 1,
        "prompt": "If 2x + 3 = 7, what is x?",
        "choices": [
            {"id": "A", "text": "2"},
            {"id": "B", "text": "-2"},
            {"id": "C", "text": "8"},
            {"id": "D", "text": "3"},
        ],
        "answer_choice_id": "A",
        "misconception_map": {"B": "sign_error", "C": "inverse_operation_error", "D": "arithmetic_error"},
        "hints": [
            {"level": 1, "text": "Subtract 3."},
            {"level": 2, "text": "Divide by 2."},
            {"level": 3, "text": "x = 2."},
        ],
        "worked_explanation": "2x = 4, x = 2.",
        "estimated_seconds": 60,
        "source_lineage": {"source_id": "deepmind_mathematics_dataset", "lineage_id": "x", "role": "concept_source_only"},
        "license": {"id": "bridgesat_original", "name": "BridgeSAT original"},
        "review_status": "draft",
        "reviewers": {},
        "content_hash": "",
        "author_metadata": {"kind": "expression", "expression": "2*2 + 3", "expected": "7"},
    }
    from app.content_pipeline.contracts import content_hash

    item["content_hash"] = content_hash(item)
    with pytest.raises(ApprovalBlockedError):
        build_pack([item], [], out_dir=tmp_path)


def test_build_pack_verifies_hashes(tmp_path: Path, drafts: tuple[list[dict], list[dict]]) -> None:
    items, _ = drafts
    approved = [dict(item, review_status="approved", reviewers={r: r for r in ("educational", "answer", "license", "accessibility")}, release_batch="b1") for item in items[:2]]
    manifest = build_pack(approved, [], out_dir=tmp_path)
    assert manifest["status"] == "published"
    assert len(manifest["item_hashes"]) == 2
    mismatches = verify_pack_hashes(tmp_path / f"{manifest['pack_id']}-{manifest['pack_version']}")
    assert mismatches == {}


def test_source_licenses_in_manifest(tmp_path: Path) -> None:
    item = _minimal_approved_item("math.linear_equations.002", "linear_equations")
    manifest = build_pack([item], [], out_dir=tmp_path)
    assert manifest["source_licenses"] == {
        "deepmind_mathematics_dataset": "bridgesat_original"
    }


def _minimal_approved_item(content_id: str, skill: str) -> dict:
    from app.content_pipeline.contracts import content_hash

    item = {
        "id": content_id,
        "version": 1,
        "schema_version": SCHEMA_VERSION,
        "domain": "math",
        "content_type": "question",
        "target_skill": skill,
        "target_subskill": "isolate_variables",
        "required_prerequisites": ["integer_operations"],
        "difficulty": 1,
        "prompt": "If 2x + 3 = 7, what is x?",
        "choices": [
            {"id": "A", "text": "2"},
            {"id": "B", "text": "-2"},
            {"id": "C", "text": "8"},
            {"id": "D", "text": "3"},
        ],
        "answer_choice_id": "A",
        "misconception_map": {"B": "sign_error", "C": "inverse_operation_error", "D": "arithmetic_error"},
        "hints": [
            {"level": 1, "text": "Subtract 3."},
            {"level": 2, "text": "Divide by 2."},
            {"level": 3, "text": "x = 2."},
        ],
        "worked_explanation": "2x = 4, x = 2.",
        "estimated_seconds": 60,
        "source_lineage": {"source_id": "deepmind_mathematics_dataset", "lineage_id": "x", "role": "concept_source_only"},
        "license": {"id": "bridgesat_original", "name": "BridgeSAT original"},
        "review_status": "approved",
        "reviewers": {r: r for r in ("educational", "answer", "license", "accessibility")},
        "release_batch": "b1",
        "content_hash": "",
        "author_metadata": {"kind": "expression", "expression": "2*2 + 3", "expected": "7"},
    }
    item["content_hash"] = content_hash(item)
    return item


# --- import --------------------------------------------------------------


def test_import_pack_into_registry(tmp_path: Path) -> None:
    item = _minimal_approved_item("math.linear_equations.003", "linear_equations")
    pack_dir = tmp_path / "packs"
    build_pack([item], [], out_dir=pack_dir)
    built = pack_dir / "bridgesat-math-0.1.0"

    db = tmp_path / "registry.db"
    imported = import_pack(db, built)
    assert imported == 1
    summary = verify_import(db)
    assert summary["content_items"] == 1
    assert summary["content_item_versions"] == 1
    assert summary["content_pack_items"] == 1
    assert summary["deepmind_source_rows"] == 1
