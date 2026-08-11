#!/usr/bin/env python3
"""Create an explicitly simulated competition-demo review ledger.

This command does not represent human review. Production publication remains
blocked unless every ``sim.*`` record is replaced by an accountable reviewer.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.content_pipeline.contracts import REVIEW_REQUIRED_COLUMNS, REVIEWS_DIR, VALIDATED_DIR
from app.content_pipeline.validation import validate_all


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def main() -> int:
    items = _read_jsonl(VALIDATED_DIR / "math-v1.jsonl")
    lessons = _read_jsonl(VALIDATED_DIR / "math-lessons-v1.jsonl")
    failures = validate_all(items, lessons)
    if failures:
        print(f"Validation failed; refusing to create review fixture: {failures}", file=sys.stderr)
        return 1

    output = REVIEWS_DIR / "math-v1.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=REVIEW_REQUIRED_COLUMNS, lineterminator="\n"
        )
        writer.writeheader()
        for entry in [*items, *lessons]:
            writer.writerow(
                {
                    "content_id": entry["id"],
                    "version": entry["version"],
                    "content_hash": entry["content_hash"],
                    "educational_reviewer": "sim.educational",
                    "answer_reviewer": "sim.answer",
                    "license_reviewer": "sim.license",
                    "accessibility_reviewer": "sim.accessibility",
                    "reviewed_at": "2026-08-11T00:00:00Z",
                    "conclusion": "approved",
                    "notes": (
                        "Controlled simulated competition review after deterministic validation; "
                        "not human review"
                    ),
                    "source_lineage_confirmed": "yes",
                    "release_batch": "content-expansion-competition-demo-v1",
                }
            )
    print(f"Wrote {len(items) + len(lessons)} simulated demo review records to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
