#!/usr/bin/env python3
"""Generate deterministic drafts for the governed math catalog and lessons.

Usage:
    python scripts/generate_math_drafts.py

Drafts are seeded by lineage ID, never reuse candidate wording or numbers,
and are written with review_status=draft.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.content_pipeline.contracts import CANDIDATES_DIR, DRAFTS_DIR
from app.content_pipeline.expansion import (
    expansion_manifest_rows,
    write_expansion_manifest,
)
from app.content_pipeline.generation import generate_all_drafts
from app.content_pipeline.selection import load_manifest


def main() -> int:
    manifest_path = CANDIDATES_DIR / "math-selection-v1.jsonl"
    if not manifest_path.exists():
        print("Selection manifest missing; run scripts/select_math_candidates.py first", file=sys.stderr)
        return 1
    rows = load_manifest(manifest_path)
    write_expansion_manifest()
    rows.extend(expansion_manifest_rows())
    items, lessons = generate_all_drafts(rows, out_dir=DRAFTS_DIR)
    print(f"Wrote {len(items)} item drafts and {len(lessons)} lesson drafts to {DRAFTS_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
