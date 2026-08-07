#!/usr/bin/env python3
"""Validate generated drafts: schema, uniqueness, hashes, exact math, and
the rewrite similarity gate against the original candidates.

Usage:
    python scripts/validate_content.py [--write-validated]

Exit code 0 only when every item and lesson passes.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.content_pipeline.contracts import DRAFTS_DIR, REVIEWED_DATA, VALIDATED_DIR
from app.content_pipeline.selection import read_reviewed_candidates
from app.content_pipeline.validation import validate_all


def _original_by_lineage() -> dict[str, str]:
    originals: dict[str, str] = {}
    for row in read_reviewed_candidates(REVIEWED_DATA):
        originals[row["id"]] = row.get("question", "")
    return originals


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--write-validated",
        action="store_true",
        help="write validated items to content/validated/ as schema_validated",
    )
    args = parser.parse_args()

    items = [
        json.loads(line)
        for line in (DRAFTS_DIR / "math-v1.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    lessons = [
        json.loads(line)
        for line in (DRAFTS_DIR / "math-lessons-v1.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    report = validate_all(items, lessons, original_by_lineage=_original_by_lineage())

    if report:
        print(f"Validation failed for {len(report)} records:", file=sys.stderr)
        for content_id, errors in report.items():
            for error in errors:
                print(f"  {content_id}: {error}", file=sys.stderr)
        return 1

    if args.write_validated:
        VALIDATED_DIR.mkdir(parents=True, exist_ok=True)
        for name, records in (("math-v1.jsonl", items), ("math-lessons-v1.jsonl", lessons)):
            body = []
            for record in records:
                record = dict(record)
                record["review_status"] = "schema_validated"
                body.append(json.dumps(record, ensure_ascii=False, sort_keys=True))
            (VALIDATED_DIR / name).write_text("\n".join(body) + "\n", encoding="utf-8")

    print(f"Validated {len(items)} items and {len(lessons)} lessons; all passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
