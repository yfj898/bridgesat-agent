"""Content validation: schema, uniqueness, hashes, exact math, similarity gate.

- ``validate_item`` enforces the item contract and the rewrite similarity gate.
- ``validate_lesson`` enforces the lesson contract.
- Exact answer verification uses pinned sympy exact arithmetic (Rational),
  never floating-point approximation.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable

from sympy import Eq, Rational, solve, sympify
from sympy.core.sympify import SympifyError

from .contracts import (
    HASH_FIELDS,
    ITEM_SCHEMA_FILE,
    LESSON_SCHEMA_FILE,
    MISCONCEPTIONS,
    SCHEMAS_DIR,
    SCHEMA_VERSION,
)

SIMILARITY_THRESHOLD = 0.55
TOKEN_RE = re.compile(r"[a-z0-9]+")


def load_schema(schema_file: str = ITEM_SCHEMA_FILE, schemas_dir: Path = SCHEMAS_DIR) -> dict:
    return json.loads((schemas_dir / schema_file).read_text(encoding="utf-8"))


def _tokenize(text: str) -> list[str]:
    return TOKEN_RE.findall(text.lower())


def _ngrams(tokens: list[str], n: int = 2) -> set[str]:
    return {" ".join(tokens[i:i + n]) for i in range(len(tokens) - n + 1)}


def rewrite_similarity(original: str, draft: str) -> float:
    """Token 1-gram and 2-gram Jaccard similarity between candidate and draft.

    A value of 1.0 means identical token streams; 0.0 means no overlap.
    """
    left = _tokenize(original)
    right = _tokenize(draft)
    if not left or not right:
        return 0.0
    left_set = set(left)
    right_set = set(right)
    unigram = len(left_set & right_set) / len(left_set | right_set)
    left_bi = _ngrams(left)
    right_bi = _ngrams(right)
    if not left_bi and not right_bi:
        bigram = 0.0
    else:
        bigram = len(left_bi & right_bi) / len(left_bi | right_bi)
    return 0.5 * unigram + 0.5 * bigram


def _normalize_choice_text(text: str) -> str:
    return str(Rational(text))


def _expected_ok(expected: str, choice_text: str) -> bool:
    try:
        return _normalize_choice_text(choice_text) == str(Rational(expected))
    except Exception:
        return False


def _verify_exact_math(author_metadata: dict, choices: list[dict], answer_choice_id: str) -> list[str]:
    errors: list[str] = []
    answer = next((c["text"] for c in choices if c["id"] == answer_choice_id), None)
    if answer is None:
        return ["answer_choice_id not found in choices"]
    kind = author_metadata.get("kind")
    try:
        if kind == "equation":
            lhs = sympify(author_metadata["lhs"])
            rhs = sympify(author_metadata["rhs"])
            variable = sympify(author_metadata["variable"])
            solutions = solve(Eq(lhs, rhs), variable)
            if len(solutions) != 1:
                errors.append(f"expected exactly one solution, found {len(solutions)}")
            elif not _expected_ok(author_metadata["expected"], str(solutions[0])):
                errors.append("author_metadata expected does not match exact solution")
            if answer is None or not _expected_ok(answer, str(solutions[0])):
                errors.append("correct choice text is not the exact solution")
        elif kind == "system":
            variables = [sympify(v) for v in author_metadata["variables"]]
            lhs1, lhs2 = (sympify(e) for e in author_metadata["equations"])
            c1, c2 = author_metadata["constants"]
            solutions = solve([Eq(lhs1, c1), Eq(lhs2, c2)], variables)
            if not solutions:
                errors.append("system has no solution")
            else:
                solution_map = solutions[0] if isinstance(solutions, list) else solutions
                got = tuple(Rational(solution_map[variable]) for variable in variables)
                expected_pair = tuple(Rational(v) for v in author_metadata["expected"])
                if expected_pair != got:
                    errors.append("author_metadata expected does not match exact system solution")
                if answer is not None:
                    match = re.fullmatch(r"\(\s*([-\d/]+)\s*,\s*([-\d/]+)\s*\)", answer)
                    if not match or tuple(Rational(m) for m in match.groups()) != expected_pair:
                        errors.append("correct choice text is not the exact system solution")
        elif kind in ("expression", "evaluate"):
            expression = author_metadata["expression"]
            if kind == "evaluate":
                variable = sympify(author_metadata["variable"])
                at = Rational(author_metadata["at"])
                value = sympify(expression).subs(variable, at)
            else:
                value = sympify(expression)
            exact = Rational(value)
            if not _expected_ok(author_metadata["expected"], str(exact)):
                errors.append("author_metadata expected does not match exact evaluation")
            if not _expected_ok(answer, str(exact)):
                errors.append("correct choice text is not the exact evaluation")
        else:
            errors.append(f"unknown author_metadata kind {kind!r}")
    except (SympifyError, ValueError, TypeError, ZeroDivisionError) as exc:
        errors.append(f"exact math verification failed: {exc}")
    return errors


def validate_item(
    item: dict,
    schema: dict | None = None,
    *,
    original_question: str | None = None,
    similarity_threshold: float = SIMILARITY_THRESHOLD,
) -> list[str]:
    errors: list[str] = []
    if schema is None:
        schema = load_schema()
    try:
        from jsonschema import validate
        from jsonschema.exceptions import ValidationError

        validate(instance=item, schema=schema)
    except ValidationError as exc:
        errors.append(f"schema: {exc.message}")
    except ImportError:
        errors.append("jsonschema is not installed")

    if item.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if item.get("review_status") not in ("draft", "schema_validated", "approved"):
        errors.append(f"unexpected review_status {item.get('review_status')!r}")

    choice_ids = [c.get("id") for c in item.get("choices", [])]
    if len(choice_ids) != 4 or len(set(choice_ids)) != 4:
        errors.append("exactly four distinct choice ids required")
    choice_texts = [str(c.get("text")) for c in item.get("choices", [])]
    if len(set(choice_texts)) != 4:
        errors.append("choice texts must all be distinct")

    answer_choice_id = item.get("answer_choice_id")
    if answer_choice_id not in choice_ids:
        errors.append("answer_choice_id must be one of the choice ids")

    misconception_map = item.get("misconception_map") or {}
    distractor_ids = [cid for cid in choice_ids if cid != answer_choice_id]
    if set(misconception_map.keys()) != set(distractor_ids):
        errors.append("misconception_map must cover exactly the three distractors")
    for misconception in misconception_map.values():
        if misconception not in MISCONCEPTIONS:
            errors.append(f"unmapped misconception {misconception!r}")

    hints = item.get("hints", [])
    if len(hints) != 3 or {h.get("level") for h in hints} != {1, 2, 3}:
        errors.append("exactly three hints with levels 1, 2, 3 required")

    if not item.get("prompt") or not str(item.get("worked_explanation")):
        errors.append("prompt and worked_explanation must be non-empty")

    source_lineage = item.get("source_lineage") or {}
    if not source_lineage.get("lineage_id") or not source_lineage.get("source_id"):
        errors.append("source_lineage must record lineage_id and source_id")

    license = item.get("license") or {}
    if not license.get("id") or not license.get("name"):
        errors.append("license must record id and name")

    expected_hash = _compute_hash(item)
    if item.get("content_hash") != expected_hash:
        errors.append("content_hash does not match canonical body")

    errors.extend(_verify_exact_math(item.get("author_metadata") or {}, item.get("choices", []), answer_choice_id))

    if original_question:
        similarity = rewrite_similarity(original_question, str(item.get("prompt", "")))
        if similarity >= similarity_threshold:
            errors.append(
                f"rewrite similarity {similarity:.3f} exceeds threshold {similarity_threshold}"
            )
    return errors


def _compute_hash(item: dict) -> str:
    from .contracts import content_hash

    return content_hash(item)


def validate_lesson(
    lesson: dict,
    schema: dict | None = None,
) -> list[str]:
    errors: list[str] = []
    if schema is None:
        schema = load_schema(LESSON_SCHEMA_FILE)
    try:
        from jsonschema import validate
        from jsonschema.exceptions import ValidationError

        validate(instance=lesson, schema=schema)
    except ValidationError as exc:
        errors.append(f"schema: {exc.message}")
    except ImportError:
        errors.append("jsonschema is not installed")
    if lesson.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if lesson.get("content_type") not in ("micro_lesson", "worked_example"):
        errors.append("content_type must be micro_lesson or worked_example")
    if not lesson.get("title") or not lesson.get("body"):
        errors.append("title and body must be non-empty")
    if lesson.get("content_hash") != _compute_hash(lesson):
        errors.append("content_hash does not match canonical body")
    return errors


def validate_all(
    items: Iterable[dict],
    lessons: Iterable[dict] | None = None,
    *,
    original_by_lineage: dict[str, str] | None = None,
) -> dict[str, list[str]]:
    """Validate items (and optionally lessons), returning id -> errors."""
    schema = load_schema()
    lesson_schema = load_schema(LESSON_SCHEMA_FILE)
    report: dict[str, list[str]] = {}
    for item in items:
        lineage = (item.get("source_lineage") or {}).get("lineage_id")
        original = (original_by_lineage or {}).get(lineage)
        errors = validate_item(item, schema, original_question=original)
        if errors:
            report[item["id"]] = errors
    if lessons is not None:
        for lesson in lessons:
            errors = validate_lesson(lesson, lesson_schema)
            if errors:
                report[lesson["id"]] = errors
    return report
