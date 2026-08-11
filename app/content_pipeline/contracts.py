"""Shared contracts and canonical hashing for the math content pipeline.

The pipeline is deliberately isolated from the student-facing loader: packs
built here are the only publishable artifacts, and they are written under
``content/packs/`` where ``app/question_bank.py`` can load them.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

CONTENT_ROOT = PROJECT_ROOT / "content"
SCHEMAS_DIR = CONTENT_ROOT / "schemas"
TAXONOMY_DIR = CONTENT_ROOT / "taxonomy"
CANDIDATES_DIR = CONTENT_ROOT / "candidates"
DRAFTS_DIR = CONTENT_ROOT / "drafts"
VALIDATED_DIR = CONTENT_ROOT / "validated"
REVIEWS_DIR = CONTENT_ROOT / "reviews"
APPROVED_DIR = CONTENT_ROOT / "approved"
PACKS_DIR = CONTENT_ROOT / "packs"

REVIEWED_DATA = PROJECT_ROOT / "data" / "reviewed" / "routes" / "ready_for_rewrite.jsonl"

SCHEMA_VERSION = "v1"
ITEM_SCHEMA_FILE = "item-v1.json"
LESSON_SCHEMA_FILE = "lesson-v1.json"

SKILLS = [
    "linear_equations",
    "systems_equations",
    "ratios_percentages",
    "functions_models",
    "inequalities",
    "quadratic_equations",
    "exponents_radicals",
    "coordinate_geometry",
]

SKILL_COUNTS = {
    "linear_equations": 12,
    "systems_equations": 12,
    "ratios_percentages": 13,
    "functions_models": 18,
}

# Per-skill subskills assigned by the deterministic selection step.
SUBSKILLS = {
    "linear_equations": ["isolate_variables"],
    "systems_equations": ["solve_systems"],
    "ratios_percentages": ["unit_rates"],
    "functions_models": ["algebraic_models", "function_evaluation"],
    "inequalities": ["solve_inequalities", "interpret_solution_sets"],
    "quadratic_equations": ["factor_quadratics", "analyze_roots"],
    "exponents_radicals": ["apply_exponent_rules", "simplify_radicals"],
    "coordinate_geometry": ["slope_distance", "midpoint_lines"],
}

PREREQUISITES = {
    "linear_equations": ["integer_operations"],
    "systems_equations": ["linear_equations", "integer_operations"],
    "ratios_percentages": ["integer_operations"],
    "functions_models": ["linear_equations", "integer_operations"],
    "inequalities": ["linear_equations", "integer_operations"],
    "quadratic_equations": ["linear_equations", "integer_operations"],
    "exponents_radicals": ["integer_operations"],
    "coordinate_geometry": ["linear_equations", "integer_operations"],
}

MISCONCEPTIONS = [
    "sign_error",
    "inverse_operation_error",
    "missing_distribution",
    "ratio_inversion",
    "unit_conversion",
    "input_substitution",
    "arithmetic_error",
    "slope_intercept_swap",
    "inequality_sign_flip",
    "boundary_inclusion_error",
    "factoring_sign_error",
    "missing_second_root",
    "exponent_rule_confusion",
    "negative_exponent_error",
    "radical_simplification_error",
    "slope_sign_error",
    "distance_formula_error",
    "midpoint_formula_error",
]

SOURCE_LINEAGE_ID = "deepmind_mathematics_dataset"
LICENSE_ID = "bridgesat_original"

REVIEW_REQUIRED_COLUMNS = [
    "content_id",
    "version",
    "content_hash",
    "educational_reviewer",
    "answer_reviewer",
    "license_reviewer",
    "accessibility_reviewer",
    "reviewed_at",
    "conclusion",
    "notes",
    "source_lineage_confirmed",
    "release_batch",
]

REVIEWER_ROLES = [
    "educational",
    "answer",
    "license",
    "accessibility",
]

REVIEW_STATUSES = [
    "draft",
    "schema_validated",
    "educational_review",
    "license_review",
    "approved",
    "published",
]

# Fields included in the canonical content hash. Review fields are mutable
# and are deliberately excluded.
ITEM_HASH_FIELDS = [
    "id",
    "version",
    "schema_version",
    "domain",
    "content_type",
    "target_skill",
    "target_subskill",
    "required_prerequisites",
    "difficulty",
    "prompt",
    "choices",
    "answer_choice_id",
    "misconception_map",
    "hints",
    "worked_explanation",
    "estimated_seconds",
    "source_lineage",
    "license",
    "author_metadata",
]

LESSON_HASH_FIELDS = [
    "id",
    "version",
    "schema_version",
    "domain",
    "content_type",
    "target_skill",
    "target_subskill",
    "target_misconceptions",
    "required_prerequisites",
    "difficulty",
    "title",
    "body",
    "estimated_seconds",
    "source_lineage",
    "license",
]

# Backward-compatible public name used by older validation callers.
HASH_FIELDS = ITEM_HASH_FIELDS


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def content_hash(item: dict) -> str:
    fields = (
        LESSON_HASH_FIELDS
        if item.get("content_type") in {"micro_lesson", "worked_example"}
        and int(item.get("version", 1)) >= 3
        else ITEM_HASH_FIELDS
    )
    body = {field: item.get(field) for field in fields}
    return "sha256:" + hashlib.sha256(
        canonical_json(body).encode("utf-8")
    ).hexdigest()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
