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
}

PREREQUISITES = {
    "linear_equations": ["integer_operations"],
    "systems_equations": ["linear_equations", "integer_operations"],
    "ratios_percentages": ["integer_operations"],
    "functions_models": ["linear_equations", "integer_operations"],
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
]

SOURCE_LINEAGE_ID = "deepmind_mathematics_dataset"
LICENSE_ID = "bridgesat_original"

REVIEW_REQUIRED_COLUMNS = [
    "content_id",
    "version",
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
HASH_FIELDS = [
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


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def content_hash(item: dict) -> str:
    body = {field: item.get(field) for field in HASH_FIELDS}
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
