#!/usr/bin/env python3
"""Fix content collisions found by the content audit.

Rewrites the seven items whose correct-answer text collides within a skill,
plus the byte-identical question pair, then validates every item through the
pipeline (validate_item) and recomputes content hashes.

The corrected items are re-reviewed by the content audit gate itself:
reviewers stay as the simulated formal review; the audit eval is the second
gate. Use `scripts/build_content_pack.py` afterwards to rebuild the pack.

Usage:
    python scripts/fix_content_collisions.py [--check]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.content_pipeline.contracts import content_hash
from app.content_pipeline.validation import validate_item

VALIDATED_ITEMS = ROOT / "content" / "validated" / "math-v1.jsonl"

EQUATION_KINDS = {
    "B": "sign_error",
    "C": "inverse_operation_error",
    "D": "arithmetic_error",
}
RATIO_KINDS = {
    "B": "ratio_inversion",
    "C": "unit_conversion",
    "D": "arithmetic_error",
}

REWRITES: dict[str, dict] = {
    "math.linear_equations.009": {
        "prompt": "If 2x - 7 = 11, what is the value of x?",
        "choices": [
            {"id": "A", "text": "9"},
            {"id": "B", "text": "-9"},
            {"id": "C", "text": "36"},
            {"id": "D", "text": "5"},
        ],
        "answer_choice_id": "A",
        "author_metadata": {
            "kind": "equation",
            "lhs": "2*x - 7",
            "rhs": "11",
            "variable": "x",
            "expected": "9",
        },
        "hints": [
            {"level": 1, "text": "Start by isolating the term containing x: add 7 to both sides."},
            {"level": 2, "text": "You now have 2x = 11 + (7)."},
            {"level": 3, "text": "Divide both sides of 2x = 18 by 2."},
        ],
        "worked_explanation": "Add 7 to both sides: 2x = 11 + (7) = 18. Then divide both sides by 2: x = 18 / 2 = 9.",
        "misconception_map": EQUATION_KINDS,
    },
    "math.linear_equations.007": {
        "prompt": "If 5x + 4 = -41, what is the value of x?",
        "choices": [
            {"id": "A", "text": "-9"},
            {"id": "B", "text": "9"},
            {"id": "C", "text": "-205"},
            {"id": "D", "text": "-37"},
        ],
        "answer_choice_id": "A",
        "author_metadata": {
            "kind": "equation",
            "lhs": "5*x + 4",
            "rhs": "-41",
            "variable": "x",
            "expected": "-9",
        },
        "hints": [
            {"level": 1, "text": "Start by isolating the term containing x: subtract 4 to both sides."},
            {"level": 2, "text": "You now have 5x = -41 - (4)."},
            {"level": 3, "text": "Divide both sides of 5x = -45 by 5."},
        ],
        "worked_explanation": "Subtract 4 from both sides: 5x = -41 - (4) = -45. Then divide both sides by 5: x = -45 / 5 = -9.",
        "misconception_map": EQUATION_KINDS,
    },
    "math.linear_equations.011": {
        "prompt": "If 4x - 3 = 29, what is the value of x?",
        "choices": [
            {"id": "A", "text": "8"},
            {"id": "B", "text": "-8"},
            {"id": "C", "text": "116"},
            {"id": "D", "text": "26"},
        ],
        "answer_choice_id": "A",
        "author_metadata": {
            "kind": "equation",
            "lhs": "4*x - 3",
            "rhs": "29",
            "variable": "x",
            "expected": "8",
        },
        "hints": [
            {"level": 1, "text": "Start by isolating the term containing x: add 3 to both sides."},
            {"level": 2, "text": "You now have 4x = 29 + (3)."},
            {"level": 3, "text": "Divide both sides of 4x = 32 by 4."},
        ],
        "worked_explanation": "Add 3 to both sides: 4x = 29 + (3) = 32. Then divide both sides by 4: x = 32 / 4 = 8.",
        "misconception_map": EQUATION_KINDS,
    },
    "math.linear_equations.012": {
        "prompt": "If 9x - 5 = 13, what is the value of x?",
        "choices": [
            {"id": "A", "text": "2"},
            {"id": "B", "text": "-2"},
            {"id": "C", "text": "117"},
            {"id": "D", "text": "8"},
        ],
        "answer_choice_id": "A",
        "author_metadata": {
            "kind": "equation",
            "lhs": "9*x - 5",
            "rhs": "13",
            "variable": "x",
            "expected": "2",
        },
        "hints": [
            {"level": 1, "text": "Start by isolating the term containing x: add 5 to both sides."},
            {"level": 2, "text": "You now have 9x = 13 + (5)."},
            {"level": 3, "text": "Divide both sides of 9x = 18 by 9."},
        ],
        "worked_explanation": "Add 5 to both sides: 9x = 13 + (5) = 18. Then divide both sides by 9: x = 18 / 9 = 2.",
        "misconception_map": EQUATION_KINDS,
    },
    "math.ratios_percentages.004": {
        "prompt": "A machine produces 45 parts in 9 minutes. At the same rate, how many parts will it produce in 8 minutes?",
        "choices": [
            {"id": "A", "text": "40"},
            {"id": "B", "text": "42"},
            {"id": "C", "text": "360"},
            {"id": "D", "text": "41"},
        ],
        "answer_choice_id": "A",
        "author_metadata": {
            "kind": "expression",
            "expression": "(45)*(8)/9",
            "expected": "40",
        },
        "hints": [
            {"level": 1, "text": "Find the unit rate: 45 parts per 9 minutes."},
            {"level": 2, "text": "Multiply the unit rate by 8 minutes."},
            {"level": 3, "text": "(45 / 9) * 8 = 40."},
        ],
        "worked_explanation": "The unit rate is 45/9 = 5 parts per minute. In 8 minutes the machine produces 5 * 8 = 40 parts.",
        "misconception_map": RATIO_KINDS,
    },
    "math.ratios_percentages.012": {
        "prompt": "A machine produces 32 parts in 8 minutes. At the same rate, how many parts will it produce in 3 minutes?",
        "choices": [
            {"id": "A", "text": "12"},
            {"id": "B", "text": "13"},
            {"id": "C", "text": "96"},
            {"id": "D", "text": "4"},
        ],
        "answer_choice_id": "A",
        "author_metadata": {
            "kind": "expression",
            "expression": "(32)*(3)/8",
            "expected": "12",
        },
        "hints": [
            {"level": 1, "text": "Find the unit rate: 32 parts per 8 minutes."},
            {"level": 2, "text": "Multiply the unit rate by 3 minutes."},
            {"level": 3, "text": "(32 / 8) * 3 = 12."},
        ],
        "worked_explanation": "The unit rate is 32/8 = 4 parts per minute. In 3 minutes the machine produces 4 * 3 = 12 parts.",
        "misconception_map": RATIO_KINDS,
    },
    "math.functions_models.013": {
        "prompt": "A taxi charges $6 per mile plus a flat fee of $9. How much, in dollars, does a ride of 6 miles cost?",
        "choices": [
            {"id": "A", "text": "45"},
            {"id": "B", "text": "44"},
            {"id": "C", "text": "90"},
            {"id": "D", "text": "15"},
        ],
        "answer_choice_id": "A",
        "author_metadata": {
            "kind": "expression",
            "expression": "6*6 + 9",
            "expected": "45",
        },
        "hints": [
            {"level": 1, "text": "The cost is the per-mile charge times the miles, plus the flat fee."},
            {"level": 2, "text": "Write the model as cost = 6 * 6 + 9."},
            {"level": 3, "text": "6 * 6 = 36, and 36 + 9 = 45."},
        ],
        "worked_explanation": "The linear model is C(x) = 6x + 9. For x = 6: C(6) = 6 * 6 + 9 = 36 + 9 = $45.",
        "misconception_map": {
            "B": "arithmetic_error",
            "C": "input_substitution",
            "D": "slope_intercept_swap",
        },
    },
}


def _load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n" for rec in records),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="dry run: report only")
    parser.add_argument("--items", type=Path, default=VALIDATED_ITEMS)
    args = parser.parse_args()

    items = _load_jsonl(args.items)
    by_id = {i["id"]: i for i in items}

    errors: list[str] = []
    for item_id, rewrite in REWRITES.items():
        item = by_id.get(item_id)
        if item is None:
            errors.append(f"missing item {item_id}")
            continue
        if args.check:
            continue
        for field, value in rewrite.items():
            item[field] = value
        if item["target_skill"] == "linear_equations":
            item["difficulty"] = 2
        item["content_hash"] = content_hash(item)
        item_errors = validate_item(item)
        if item_errors:
            errors.append(f"{item_id}: {item_errors}")

    if args.check:
        print(f"Would rewrite {len(REWRITES)} items (check mode, no changes written).")
        return 0

    def norm(s: str) -> str:
        return " ".join(s.split())

    answer_by_skill: dict[str, list[tuple[str, str]]] = {}
    bodies: dict[str, list[str]] = {}
    for item in items:
        answer = next(c["text"] for c in item["choices"] if c["id"] == item["answer_choice_id"])
        answer_by_skill.setdefault(item["target_skill"], []).append((item["id"], answer))
        bodies.setdefault(item["target_skill"], []).append((item["id"], norm(item["prompt"])))

    for skill, pairs in answer_by_skill.items():
        dup = {t: [iid for iid, tt in pairs if tt == t] for t in {tt for _, tt in pairs}}
        dup = {t: ids for t, ids in dup.items() if len(ids) > 1}
        if dup:
            errors.append(f"duplicate answer texts in {skill}: {dup}")
    for skill, pairs in bodies.items():
        dup = {b: [iid for iid, bb in pairs if bb == b] for b in {bb for _, bb in pairs}}
        dup = {b: ids for b, ids in dup.items() if len(ids) > 1}
        if dup:
            errors.append(f"identical prompts in {skill}: {dup}")

    if errors:
        print("Fix verification failed:", file=sys.stderr)
        for err in errors:
            print(f"  {err}", file=sys.stderr)
        return 1

    _write_jsonl(args.items, items)
    print(f"Rewrote {len(REWRITES)} items with fresh answers; hashes recomputed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
