from __future__ import annotations

import csv
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REVIEWED = ROOT / "data" / "reviewed"


def read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def main() -> int:
    report = json.loads((REVIEWED / "review-report.json").read_text(encoding="utf-8"))
    all_rows = read_jsonl(REVIEWED / "all-candidates.jsonl")
    review_rows = read_jsonl(REVIEWED / "review-queue.jsonl")
    evaluation_rows = read_jsonl(REVIEWED / "evaluation-only.jsonl")
    ready_rows = read_jsonl(REVIEWED / "routes" / "ready_for_rewrite.jsonl")

    assert len(all_rows) == report["unique_count"]
    assert len({row["id"] for row in all_rows}) == len(all_rows)
    assert all(len(str(row["normalized_content_hash"])) == 64 for row in all_rows)
    assert report["student_content_approved_count"] == 0
    assert all(row["source_id"] == "gsm8k" for row in evaluation_rows)
    assert all(row["review_route"] == "evaluation_only" for row in evaluation_rows)
    assert all(row["source_id"] == "deepmind_mathematics_dataset" for row in ready_rows)
    assert all(bool(row.get("human_approval_required_for_student_use")) for row in ready_rows)

    loc_rows = [
        row for row in all_rows if row["source_id"] == "library_of_congress_free_to_use"
    ]
    assert len(loc_rows) == 100
    assert all(row.get("title") and row.get("canonical_url") and row.get("upstream_id") for row in loc_rows)

    with (REVIEWED / "review-queue.csv").open(encoding="utf-8", newline="") as handle:
        csv_rows = list(csv.DictReader(handle))
    assert len(csv_rows) == len(review_rows)

    print(
        json.dumps(
            {
                "status": "valid",
                "all_candidates": len(all_rows),
                "review_queue": len(review_rows),
                "evaluation_only": len(evaluation_rows),
                "ready_for_rewrite": len(ready_rows),
                "loc_metadata_complete": len(loc_rows),
                "student_content_approved": 0,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
