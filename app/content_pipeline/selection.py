"""Content pipeline: candidate selection and immutable manifest generation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .contracts import CANDIDATES_DIR, REVIEWED_DATA, SKILLS, SKILL_COUNTS

TARGET_SOURCE_ID = "deepmind_mathematics_dataset"
TARGET_REVIEW_ROUTE = "ready_for_rewrite"
TARGET_ROLE = "question_candidate"
TARGET_CONFIDENCE = 1.0
MANIFEST_NAME = "math-selection-v1.jsonl"
MANIFEST_CHECKSUM_NAME = "math-selection-v1.checksum"


def read_reviewed_candidates(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def selection_matches(row: dict) -> bool:
    mapping = row.get("skill_mapping") or {}
    return (
        row.get("source_id") == TARGET_SOURCE_ID
        and row.get("review_route") == TARGET_REVIEW_ROUTE
        and mapping.get("role") == TARGET_ROLE
        and mapping.get("mapping_confidence") == TARGET_CONFIDENCE
        and mapping.get("primary_skill") in SKILLS
    )


def select_math_candidates(
    reviewed_path: Path = REVIEWED_DATA,
) -> list[dict]:
    """Return the 55 math candidates in deterministic skill order."""
    selected = [row for row in read_reviewed_candidates(reviewed_path) if selection_matches(row)]
    order = {skill: index for index, skill in enumerate(SKILLS)}

    def sort_key(row: dict) -> tuple:
        mapping = row["skill_mapping"]
        return (
            order[mapping["primary_skill"]],
            row["id"],
        )

    return sorted(selected, key=sort_key)


def build_manifest_row(candidate: dict, sequence: int, count: int) -> dict:
    mapping = candidate["skill_mapping"]
    skill = mapping["primary_skill"]
    return {
        "manifest_schema_version": "1.0",
        "lineage_id": candidate["id"],
        "content_id": f"math.{skill}.{sequence:03d}",
        "target_skill": skill,
        "target_subskill": mapping.get("subskill"),
        "upstream_module": candidate.get("upstream_module"),
        "source_id": candidate["source_id"],
        "license_id": candidate.get("license_id"),
        "candidate_content_hash": candidate.get("content_hash"),
        "candidate_normalized_hash": candidate.get("normalized_content_hash"),
        "mapping_confidence": mapping.get("mapping_confidence"),
        "mapping_method": mapping.get("mapping_method"),
        "sequence_in_skill": sequence,
        "skill_total": count,
    }


def write_immutable_manifest(
    rows: list[dict],
    out_dir: Path = CANDIDATES_DIR,
    *,
    force: bool = False,
) -> Path:
    """Write the selection manifest and a checksum sibling.

    The manifest is immutable: unless ``force`` is set, writing to an existing
    path with different content raises FileExistsError.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / MANIFEST_NAME
    checksum_path = out_dir / MANIFEST_CHECKSUM_NAME
    body = "".join(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n" for row in rows)
    checksum = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if path.exists() and not force:
        existing = path.read_text(encoding="utf-8")
        if existing != body:
            raise FileExistsError(
                f"Selection manifest {path} already exists with different content; "
                "refusing to overwrite (use --force to replace)"
            )
        return path
    path.write_text(body, encoding="utf-8")
    checksum_path.write_text(checksum + "\n", encoding="utf-8")
    return path


def load_manifest(manifest_path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in manifest_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def verify_selection_counts(rows: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        skill = row["target_skill"]
        counts[skill] = counts.get(skill, 0) + 1
    if counts != SKILL_COUNTS:
        raise ValueError(f"Unexpected selection counts {counts}; expected {SKILL_COUNTS}")
    return counts
