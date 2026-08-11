"""Review records and pack building.

A pack may only contain items whose review records are complete: all four
reviewer roles filled, conclusion ``approved``, source lineage confirmed, and
a release batch present. Withdrawals only affect new selection and packs;
historical versions remain auditable.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from .contracts import (
    APPROVED_DIR,
    PACKS_DIR,
    REVIEW_REQUIRED_COLUMNS,
    REVIEWER_ROLES,
    REVIEWS_DIR,
    SCHEMA_VERSION,
)
from .selection import verify_selection_counts

MINIMUM_APP_VERSION = "0.1.0"
PACK_ID = "bridgesat-math"
PACK_VERSION = "0.3.0"
REVIEW_FILE = "math-v1.csv"
APPROVED_FILE = "math-v1.jsonl"


class ReviewIncompleteError(RuntimeError):
    pass


class ApprovalBlockedError(RuntimeError):
    pass


def review_provenance(
    entries: list[dict], *, allow_simulated_review: bool = False
) -> dict[str, object]:
    """Classify reviewer provenance and enforce the demo-only simulation gate."""
    simulated_review = any(
        str(reviewer).startswith("sim.")
        for entry in entries
        for reviewer in (entry.get("reviewers") or {}).values()
    )
    if simulated_review and not allow_simulated_review:
        raise ApprovalBlockedError(
            "simulated reviewers are competition-demo evidence, not human approval; "
            "pass allow_simulated_review explicitly for a demo-only pack"
        )
    return {
        "mode": "simulated_competition_review" if simulated_review else "human_review",
        "human_approved": not simulated_review,
    }


def _review_row_complete(row: dict) -> list[str]:
    missing = [
        column
        for column in REVIEW_REQUIRED_COLUMNS
        if column not in ("conclusion", "source_lineage_confirmed")
        and not str(row.get(column, "")).strip()
    ]
    if row.get("conclusion") != "approved":
        missing.append("conclusion=approved")
    if str(row.get("source_lineage_confirmed", "")).strip().lower() not in ("yes", "true"):
        missing.append("source_lineage_confirmed")
    return missing


def read_reviews(path: Path) -> dict[str, dict]:
    """Return content_id -> review row for a CSV review ledger."""
    reviews: dict[str, dict] = {}
    if not path.exists():
        return reviews
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            content_id = row.get("content_id", "")
            if content_id:
                reviews[content_id] = row
    return reviews


def write_review_template(items: list[dict], path: Path) -> None:
    """Write an empty review ledger with the required columns."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=REVIEW_REQUIRED_COLUMNS, lineterminator="\n"
        )
        writer.writeheader()
        for item in items:
            writer.writerow(
                {
                    "content_id": item["id"],
                    "version": item.get("version", 1),
                    "content_hash": item.get("content_hash", ""),
                    "educational_reviewer": "",
                    "answer_reviewer": "",
                    "license_reviewer": "",
                    "accessibility_reviewer": "",
                    "reviewed_at": "",
                    "conclusion": "",
                    "notes": "",
                    "source_lineage_confirmed": "",
                    "release_batch": "",
                }
            )


def review_row_complete_for_item(review: dict, item: dict) -> list[str]:
    missing = _review_row_complete(review)
    if str(review.get("content_id", "")) != str(item.get("id", "")):
        missing.append("content_id")
    if str(review.get("version", "")) != str(item.get("version", "")):
        missing.append("version")
    if str(review.get("content_hash", "")) != str(item.get("content_hash", "")):
        missing.append("content_hash")
    return missing


def approve_items(items: list[dict], reviews: dict[str, dict]) -> list[dict]:
    """Return items that carry complete approved reviews, tagged approved."""
    approved: list[dict] = []
    for item in items:
        review = reviews.get(item["id"])
        if not review:
            continue
        missing = review_row_complete_for_item(review, item)
        if missing:
            continue
        item = dict(item)
        item["review_status"] = "approved"
        item["reviewers"] = {
            role: str(review.get(f"{role}_reviewer", "")).strip()
            for role in REVIEWER_ROLES
        }
        item["reviewed_at"] = review.get("reviewed_at", "")
        item["release_batch"] = review.get("release_batch", "")
        approved.append(item)
    return approved


def build_pack(
    approved_items: list[dict],
    approved_lessons: list[dict],
    *,
    pack_id: str = PACK_ID,
    pack_version: str = PACK_VERSION,
    out_dir: Path = PACKS_DIR,
    withdrawn: list[dict] | None = None,
    allow_simulated_review: bool = False,
) -> dict:
    """Build a pack directory containing manifest, items, and lessons."""
    pack_dir = out_dir / f"{pack_id}-{pack_version}"
    pack_dir.mkdir(parents=True, exist_ok=True)

    for item in approved_items:
        if item.get("review_status") != "approved":
            raise ApprovalBlockedError(f"{item['id']} is not approved")
        review = item.get("reviewers") or {}
        if not review or len(review) < len(REVIEWER_ROLES):
            raise ApprovalBlockedError(f"{item['id']} has incomplete reviewers")
        if not item.get("release_batch"):
            raise ApprovalBlockedError(f"{item['id']} has no release batch")

    for lesson in approved_lessons:
        if lesson.get("review_status") != "approved":
            raise ApprovalBlockedError(f"{lesson['id']} is not approved")
        review = lesson.get("reviewers") or {}
        if not review or len(review) < len(REVIEWER_ROLES):
            raise ApprovalBlockedError(f"{lesson['id']} has incomplete reviewers")
        if not lesson.get("release_batch"):
            raise ApprovalBlockedError(f"{lesson['id']} has no release batch")
    lessons = list(approved_lessons)

    provenance = review_provenance(
        [*approved_items, *lessons],
        allow_simulated_review=allow_simulated_review,
    )

    skills = sorted({item["target_skill"] for item in approved_items})
    misconceptions = sorted(
        {
            misconception
            for item in approved_items
            for misconception in (item.get("misconception_map") or {}).values()
        }
    )

    manifest = {
        "pack_id": pack_id,
        "pack_version": pack_version,
        "status": "published",
        "schema_version": SCHEMA_VERSION,
        "minimum_app_version": MINIMUM_APP_VERSION,
        "allowed_item_schema_versions": [SCHEMA_VERSION],
        "item_hashes": {item["id"]: item.get("content_hash", "") for item in approved_items},
        "lesson_hashes": {lesson["id"]: lesson.get("content_hash", "") for lesson in lessons},
        "content_counts": {"questions": len(approved_items), "lessons": len(lessons)},
        "skills": skills,
        "misconceptions": misconceptions,
        "source_licenses": _source_license_manifest(approved_items + lessons),
        "withdrawn": withdrawn or [],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "reviewers_present": True,
        "review_provenance": provenance,
    }
    manifest_path = pack_dir / "manifest.json"
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        immutable_keys = ("item_hashes", "lesson_hashes", "content_counts")
        if any(existing.get(key) != manifest.get(key) for key in immutable_keys):
            raise ApprovalBlockedError(
                f"pack {pack_id}-{pack_version} already exists with different content; "
                "publish a new pack version"
            )
        return existing

    staged = {
        "items.jsonl": "".join(
            json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
            for item in approved_items
        ),
        "lessons.jsonl": "".join(
            json.dumps(lesson, ensure_ascii=False, sort_keys=True) + "\n"
            for lesson in lessons
        ),
        "manifest.json": json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, indent=2
        ),
    }
    for filename, body in staged.items():
        temporary = pack_dir / f".{filename}.tmp"
        temporary.write_text(body, encoding="utf-8")
        temporary.replace(pack_dir / filename)
    return manifest


def _source_license_manifest(items: list[dict]) -> dict[str, str]:
    sources: dict[str, str] = {}
    for item in items:
        lineage = item.get("source_lineage") or {}
        source_id = lineage.get("source_id")
        license = (item.get("license") or {}).get("id")
        if source_id and license:
            sources[source_id] = license
    return sources


def write_approved(items: list[dict], path: Path = APPROVED_DIR / APPROVED_FILE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in items),
        encoding="utf-8",
    )
    return path


def summarize_selection(items: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        skill = item["target_skill"]
        counts[skill] = counts.get(skill, 0) + 1
    return counts


def verify_pack_hashes(pack_dir: Path) -> dict[str, str]:
    """Recompute question and lesson hashes inside a built pack."""
    manifest = json.loads((pack_dir / "manifest.json").read_text(encoding="utf-8"))
    mismatches: dict[str, str] = {}
    from .contracts import content_hash

    for filename, hashes_key in (
        ("items.jsonl", "item_hashes"),
        ("lessons.jsonl", "lesson_hashes"),
    ):
        artifact = pack_dir / filename
        if not artifact.exists():
            continue
        for line in artifact.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            manifest_hashes = manifest.get(hashes_key)
            expected = (manifest_hashes or {}).get(item["id"])
            actual = item.get("content_hash")
            recomputed = content_hash(item)
            if (manifest_hashes is not None and expected != actual) or actual != recomputed:
                mismatches[item["id"]] = (
                    f"manifest={expected} item={actual} recomputed={recomputed}"
                )
    return mismatches
