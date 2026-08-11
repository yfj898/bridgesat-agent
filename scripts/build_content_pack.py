#!/usr/bin/env python3
"""Build a published pack from approved items and lessons.

Usage:
    python scripts/build_content_pack.py

Reads validated items, the review ledger, and approved lessons. Only records
with complete review fields and conclusion=approved enter the pack. Blocks on
any unapproved record and prints the missing reviewer fields.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.content_pipeline.contracts import (
    APPROVED_DIR,
    PACKS_DIR,
    REVIEWS_DIR,
    VALIDATED_DIR,
)
from app.content_pipeline.packaging import (
    ApprovalBlockedError,
    approve_items,
    build_pack,
    read_reviews,
    review_provenance,
    review_row_complete_for_item,
    verify_pack_hashes,
    write_approved,
    write_review_template,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--write-template",
        action="store_true",
        help="write an empty review CSV template and exit",
    )
    parser.add_argument(
        "--allow-simulated-review",
        action="store_true",
        help=(
            "build a competition-demo pack from sim.* review fixtures; this is "
            "explicitly not human approval"
        ),
    )
    args = parser.parse_args()

    items = [
        json.loads(line)
        for line in (VALIDATED_DIR / "math-v1.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    lessons = [
        json.loads(line)
        for line in (VALIDATED_DIR / "math-lessons-v1.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    reviews_path = REVIEWS_DIR / "math-v1.csv"
    if args.write_template:
        write_review_template(items + lessons, reviews_path)
        print(f"Wrote review template for {len(items) + len(lessons)} records to {reviews_path}")
        return 0

    reviews = read_reviews(reviews_path)
    approved = approve_items(items, reviews)
    if len(approved) != len(items):
        print(
            f"Approval blocked: {len(items) - len(approved)} of {len(items)} items lack "
            "complete approved reviews. Run --write-template, fill the ledger, and retry.",
            file=sys.stderr,
        )
        for item in items:
            review = reviews.get(item["id"])
            if review is None:
                print(f"  {item['id']}: no review row", file=sys.stderr)
            else:
                missing = review_row_complete_for_item(review, item)
                print(f"  {item['id']}: missing {', '.join(missing)}", file=sys.stderr)
        return 1

    approved_lessons = approve_items(lessons, reviews)
    if len(approved_lessons) != len(lessons):
        print(
            f"Approval blocked: {len(lessons) - len(approved_lessons)} of {len(lessons)} "
            "lessons lack complete approved reviews.",
            file=sys.stderr,
        )
        return 1

    try:
        review_provenance(
            approved + approved_lessons,
            allow_simulated_review=args.allow_simulated_review,
        )
    except ApprovalBlockedError as exc:
        print(f"Publication blocked before artifact write: {exc}", file=sys.stderr)
        return 1
    write_approved(approved + approved_lessons, APPROVED_DIR / "math-v1.jsonl")
    manifest = build_pack(
        approved,
        approved_lessons,
        out_dir=PACKS_DIR,
        allow_simulated_review=args.allow_simulated_review,
    )
    mismatches = verify_pack_hashes(PACKS_DIR / f"{manifest['pack_id']}-{manifest['pack_version']}")
    if mismatches:
        print(f"Hash verification failed: {mismatches}", file=sys.stderr)
        return 1
    print(f"Built pack {manifest['pack_id']}-{manifest['pack_version']} "
          f"with {len(approved)} items and {len(approved_lessons)} lessons.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
