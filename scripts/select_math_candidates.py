#!/usr/bin/env python3
"""Select the 55 math candidates into an immutable selection manifest.

Usage:
    python scripts/select_math_candidates.py [--force]

Only candidates with source_id=deepmind_mathematics_dataset,
review_route=ready_for_rewrite, role=question_candidate, a target math skill,
and mapping_confidence=1.0 are eligible. The manifest is immutable unless
--force is passed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.content_pipeline.contracts import CANDIDATES_DIR
from app.content_pipeline.selection import (
    build_manifest_row,
    select_math_candidates,
    verify_selection_counts,
    write_immutable_manifest,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="overwrite an existing manifest")
    args = parser.parse_args()

    selected = select_math_candidates()
    counts: dict[str, int] = {}
    for row in selected:
        counts[row["skill_mapping"]["primary_skill"]] = (
            counts.get(row["skill_mapping"]["primary_skill"], 0) + 1
        )

    sequence: dict[str, int] = {}
    rows = []
    for candidate in selected:
        skill = candidate["skill_mapping"]["primary_skill"]
        sequence[skill] = sequence.get(skill, 0) + 1
        rows.append(
            build_manifest_row(candidate, sequence[skill], counts[skill])
        )
    verify_selection_counts(rows)

    path = write_immutable_manifest(rows, CANDIDATES_DIR, force=args.force)
    print(f"Wrote {len(rows)} selection rows to {path}")
    for skill, count in counts.items():
        print(f"  {skill}: {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
