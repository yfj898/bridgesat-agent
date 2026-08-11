"""Deterministic draft generation for the 55 math items and per-skill lessons.

Every draft is seeded from its lineage ID so regeneration is reproducible.
The generator never reuses the candidate's numbers, phrasing, or option
structure; the candidate only provides the concept (skill/subskill/module).
"""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path

from sympy import Rational

from .contracts import (
    APPROVED_DIR,
    DRAFTS_DIR,
    LICENSE_ID,
    MISCONCEPTIONS,
    PREREQUISITES,
    SCHEMA_VERSION,
    SOURCE_LINEAGE_ID,
)
from .selection import load_manifest

DRAFT_STATUS = "draft"
QUESTIONS_FILE = "math-v1.jsonl"
LESSONS_FILE = "math-lessons-v1.jsonl"

UNKNOWN_MISCONCEPTION = "unmapped"

_MISCONCEPTION_ROWS = {
    "linear_equations": "sign_error, inverse_operation_error, arithmetic_error",
    "systems_equations": "sign_error, inverse_operation_error, arithmetic_error",
    "ratios_percentages": "ratio_inversion, unit_conversion, arithmetic_error",
    "functions_models": "slope_intercept_swap, input_substitution, sign_error",
}


def rng_for(lineage_id: str, salt: str = "math-v1") -> random.Random:
    seed = int(hashlib.sha256(f"{lineage_id}:{salt}".encode("utf-8")).hexdigest()[:16], 16)
    return random.Random(seed)


def _fmt(r: Rational) -> str:
    return str(Rational(r))


def _signed_term(coefficient: int, variable: str) -> str:
    """Render ax or -ax for the lead term of an expression."""
    if coefficient == 1:
        return variable
    if coefficient == -1:
        return f"-{variable}"
    return f"{coefficient}{variable}"


def _signed_constant(value: int) -> str:
    if value >= 0:
        return f"+ {value}"
    return f"- {-value}"


def _distinct(*values: object) -> list[object]:
    out: list[object] = []
    for value in values:
        if value not in out:
            out.append(value)
    return out


def _choices(
    answer: object,
    distractors: list[object],
    labels: list[str],
    rng: random.Random,
) -> tuple[list[dict], str, dict[str, str]]:
    """Build four distinct choices plus a value-aligned misconception map.

    Requires the answer and all three distractors to be pairwise distinct.
    A collision is an authoring error because shifting a value would sever the
    distractor's semantic link to its misconception.
    """
    answer = str(answer)
    seen = {answer}
    mapped: list[tuple[str, str | None]] = []
    for distractor, label in zip(distractors, labels):
        text = str(distractor)
        if text in seen:
            raise ValueError(
                f"choice collision for {distractor!r}; author a distinct misconception value"
            )
        seen.add(text)
        mapped.append((text, label))

    shuffled = [(answer, None), *mapped]
    rng.shuffle(shuffled)
    choices = [
        {"id": cid, "text": text}
        for cid, (text, _) in zip(["A", "B", "C", "D"], shuffled)
    ]
    correct = next(
        choice["id"]
        for choice, (_, misconception) in zip(choices, shuffled)
        if misconception is None
    )
    text_to_label = dict(mapped)
    misconception_map = {
        c["id"]: text_to_label[c["text"]] for c in choices if c["text"] != answer
    }
    return choices, correct, misconception_map


def _base_item(
    *,
    lineage_id: str,
    content_id: str,
    skill: str,
    subskill: str,
    difficulty: int,
    prompt: str,
    choices: list[dict],
    answer_choice_id: str,
    misconception_map: dict[str, str],
    hints: list[dict],
    worked_explanation: str,
    estimated_seconds: int,
    author_metadata: dict,
    license: dict,
    source_id: str = SOURCE_LINEAGE_ID,
) -> dict:
    return {
        "id": content_id,
        # Version 1 questions remain in the historical 0.1.0 pack. The
        # expanded catalog was regenerated after answer-label review, so its
        # reviewed release uses a new immutable version.
        "version": 3,
        "schema_version": SCHEMA_VERSION,
        "domain": "math",
        "content_type": "question",
        "target_skill": skill,
        "target_subskill": subskill,
        "required_prerequisites": PREREQUISITES[skill],
        "difficulty": difficulty,
        "prompt": prompt,
        "choices": choices,
        "answer_choice_id": answer_choice_id,
        "misconception_map": misconception_map,
        "hints": hints,
        "worked_explanation": worked_explanation,
        "estimated_seconds": estimated_seconds,
        "source_lineage": {
            "source_id": source_id,
            "lineage_id": lineage_id,
            "role": "concept_source_only",
        },
        "license": license,
        "review_status": DRAFT_STATUS,
        "reviewers": {},
        "author_metadata": author_metadata,
    }


def _source_license() -> dict:
    return {
        "id": LICENSE_ID,
        "name": "BridgeSAT original content",
        "recorded_authorship": "ai_assisted_bridgesat",
        "independent_answer_verification": True,
    }


def _hints(lines: list[str]) -> list[dict]:
    return [{"level": i + 1, "text": text} for i, text in enumerate(lines)]


def generate_linear_item(row: dict) -> dict:
    rng = rng_for(row["lineage_id"])
    a = rng.randint(2, 9)
    b = rng.randint(-18, 18)
    s = rng.choice([v for v in range(-12, 13) if v != 0])
    c = a * s + b
    answer = _fmt(s)
    d1 = _fmt(-s)  # sign_error
    d2 = _fmt(a * (c - b))  # inverse_operation_error: multiplied instead of divided
    d3 = _fmt(s + 1)  # arithmetic_error
    choices, correct, misconception_map = _choices(
        answer,
        [d1, d2, d3],
        ["sign_error", "inverse_operation_error", "arithmetic_error"],
        rng,
    )
    answer_text = next(c["text"] for c in choices if c["id"] == correct)
    lhs = f"{_signed_term(a, 'x')} {_signed_constant(b)}"
    hint1 = (
        f"Start by isolating the term containing x: "
        f"{'add' if b < 0 else 'subtract'} {abs(b)} to both sides."
    )
    return _base_item(
        lineage_id=row["lineage_id"],
        content_id=row["content_id"],
        skill="linear_equations",
        subskill="isolate_variables",
        difficulty=rng.randint(1, 2),
        prompt=f"If {lhs} = {c}, what is the value of x?",
        choices=choices,
        answer_choice_id=correct,
        misconception_map=misconception_map,
        hints=_hints([
            hint1,
            f"You now have {_signed_term(a, 'x')} = {c} - ({b}).",
            f"Divide both sides of {_signed_term(a, 'x')} = {a * s} by {a}.",
        ]),
        worked_explanation=(
            f"Subtract {b} from both sides: {_signed_term(a, 'x')} = {c} - ({b}) = {a * s}. "
            f"Then divide both sides by {a}: x = {a * s} / {a} = {answer_text}."
        ),
        estimated_seconds=60,
        author_metadata={
            "kind": "equation",
            "lhs": f"{a}*x + {b}",
            "rhs": f"{c}",
            "variable": "x",
            "expected": answer_text,
        },
        license=_source_license(),
    )


def generate_systems_item(row: dict) -> dict:
    rng = rng_for(row["lineage_id"])
    x0 = rng.choice([v for v in range(-8, 9) if v != 0])
    y0 = rng.choice([v for v in range(-8, 9) if v != 0])
    while y0 == x0:
        y0 = rng.choice([v for v in range(-8, 9) if v != 0])
    a1 = rng.randint(1, 5)
    b1 = rng.randint(1, 5)
    a2 = rng.randint(1, 5)
    b2 = rng.randint(1, 5)
    for _ in range(50):
        if a1 * b2 - a2 * b1 != 0:
            break
        a1 = rng.randint(1, 5)
        b2 = rng.randint(1, 5)
    c1 = a1 * x0 + b1 * y0
    c2 = a2 * x0 + b2 * y0
    answer = (x0, y0)
    d1 = (-x0, y0)  # sign_error
    d2 = (y0, x0)  # inverse_operation_error: swapped variables
    d3 = (x0, y0 + 1)  # arithmetic_error
    choices, correct, misconception_map = _choices(
        answer,
        [d1, d2, d3],
        ["sign_error", "inverse_operation_error", "arithmetic_error"],
        rng,
    )
    return _base_item(
        lineage_id=row["lineage_id"],
        content_id=row["content_id"],
        skill="systems_equations",
        subskill="solve_systems",
        difficulty=rng.randint(2, 3),
        prompt=(
            f"What is the solution (x, y) to the system of equations "
            f"{_signed_term(a1, 'x')} + {_signed_term(b1, 'y')} = {c1} and "
            f"{_signed_term(a2, 'x')} + {_signed_term(b2, 'y')} = {c2}?"
        ),
        choices=choices,
        answer_choice_id=correct,
        misconception_map=misconception_map,
        hints=_hints([
            "Eliminate one variable by multiplying the equations so the coefficients of x match.",
            "Subtract the equations to eliminate x, then solve for y.",
            "Substitute the y-value back into either equation to find x.",
        ]),
        worked_explanation=(
            f"Eliminate x: multiplying and subtracting gives y = {y0}. "
            f"Substituting back yields x = {x0}. So the solution is ({x0}, {y0})."
        ),
        estimated_seconds=90,
        author_metadata={
            "kind": "system",
            "equations": [f"{a1}*x + {b1}*y", f"{a2}*x + {b2}*y"],
            "constants": [c1, c2],
            "variables": ["x", "y"],
            "expected": [x0, y0],
        },
        license=_source_license(),
    )


def generate_rates_item(row: dict) -> dict:
    rng = rng_for(row["lineage_id"])
    t = rng.randint(2, 8)
    rate = rng.randint(2, 12)
    n = rate * t
    T = rng.choice([m * t for m in range(2, 5)])
    answer = n * T // t
    d1 = t * T // n if n != 0 else answer  # ratio_inversion
    d2 = answer * 60  # unit_conversion: minutes treated as seconds
    d3 = answer + 1  # arithmetic_error
    if d1 == 0 or d1 == answer:
        d1 = answer - rate  # fallback: student subtracted the rate once
    choices, correct, misconception_map = _choices(
        answer,
        [d1, d2, d3],
        ["ratio_inversion", "unit_conversion", "arithmetic_error"],
        rng,
    )
    answer_text = next(c["text"] for c in choices if c["id"] == correct)
    item_number = int(row["content_id"].rsplit(".", 1)[1])
    contexts = [
        ("A machine", "parts", "produce"),
        ("A school printer", "pages", "print"),
        ("A volunteer team", "supply kits", "assemble"),
    ]
    subject, unit_name, verb = contexts[(item_number - 1) % len(contexts)]
    return _base_item(
        lineage_id=row["lineage_id"],
        content_id=row["content_id"],
        skill="ratios_percentages",
        subskill="unit_rates",
        difficulty=rng.randint(1, 2),
        prompt=(
            f"{subject} can {verb} {n} {unit_name} in {t} minutes. "
            f"At the same rate, how many {unit_name} can it {verb} in {T} minutes?"
        ),
        choices=choices,
        answer_choice_id=correct,
        misconception_map=misconception_map,
        hints=_hints([
            f"Find the unit rate: {n} {unit_name} per {t} minutes.",
            f"Multiply the unit rate by {T} minutes.",
            f"({n} / {t}) * {T} = {answer_text}.",
        ]),
        worked_explanation=(
            f"The unit rate is {n}/{t} = {rate} {unit_name} per minute. "
            f"In {T} minutes the total is {rate} * {T} = {answer_text} {unit_name}."
        ),
        estimated_seconds=60,
        author_metadata={
            "kind": "expression",
            "expression": f"({n})*({T})/{t}",
            "expected": answer_text,
        },
        license=_source_license(),
    )


def generate_algebraic_models_item(row: dict) -> dict:
    rng = rng_for(row["lineage_id"])
    for _ in range(100):
        m = rng.randint(2, 9)
        b = rng.randint(3, 25)
        x = rng.randint(2, 8)
        answer = m * x + b
        d1 = b * x + m  # slope_intercept_swap
        d2 = (m + b) * x  # input_substitution: flat fee incorrectly multiplied by input
        d3 = answer - 1  # arithmetic_error
        if len({answer, d1, d2, d3}) == 4:
            break
    else:
        raise ValueError("could not author distinct algebraic-model distractors")
    choices, correct, misconception_map = _choices(
        answer,
        [d1, d2, d3],
        ["slope_intercept_swap", "input_substitution", "arithmetic_error"],
        rng,
    )
    answer_text = next(c["text"] for c in choices if c["id"] == correct)
    return _base_item(
        lineage_id=row["lineage_id"],
        content_id=row["content_id"],
        skill="functions_models",
        subskill="algebraic_models",
        difficulty=rng.randint(2, 3),
        prompt=(
            f"A taxi charges ${m} per mile plus a flat fee of ${b}. "
            f"How much, in dollars, does a ride of {x} miles cost?"
        ),
        choices=choices,
        answer_choice_id=correct,
        misconception_map=misconception_map,
        hints=_hints([
            "The cost is the per-mile charge times the miles, plus the flat fee.",
            f"Write the model as cost = {m} * {x} + {b}.",
            f"{m} * {x} = {m * x}, and {m * x} + {b} = {answer_text}.",
        ]),
        worked_explanation=(
            f"The linear model is C(x) = {m}x + {b}. "
            f"For x = {x}: C({x}) = {m} * {x} + {b} = {m * x} + {b} = ${answer_text}."
        ),
        estimated_seconds=75,
        author_metadata={
            "kind": "expression",
            "expression": f"{m}*{x} + {b}",
            "expected": answer_text,
        },
        license=_source_license(),
    )


def generate_function_evaluation_item(row: dict) -> dict:
    rng = rng_for(row["lineage_id"])
    for _ in range(100):
        a = rng.randint(2, 9)
        b = rng.choice([v for v in range(-15, 16) if v != 0])
        k = rng.choice([v for v in range(-9, 10) if v not in (0, 1)])
        answer = a * k + b
        d1 = a + b * k  # input_substitution
        d2 = a * k - b  # sign_error
        d3 = answer + 1  # arithmetic_error
        if len({answer, d1, d2, d3}) == 4:
            break
    else:
        raise ValueError("could not author distinct function-evaluation distractors")
    choices, correct, misconception_map = _choices(
        answer,
        [d1, d2, d3],
        ["input_substitution", "sign_error", "arithmetic_error"],
        rng,
    )
    answer_text = next(c["text"] for c in choices if c["id"] == correct)
    fn = f"f(x) = {a}x {_signed_constant(b)}"
    return _base_item(
        lineage_id=row["lineage_id"],
        content_id=row["content_id"],
        skill="functions_models",
        subskill="function_evaluation",
        difficulty=rng.randint(1, 2),
        prompt=f"If {fn}, what is the value of f({k})?",
        choices=choices,
        answer_choice_id=correct,
        misconception_map=misconception_map,
        hints=_hints([
            f"Substitute {k} for x in {fn}.",
            f"Compute {a} * {k} first, then add {b}.",
            f"{a} * {k} + {b} = {answer_text}.",
        ]),
        worked_explanation=(
            f"f({k}) = {a} * {k} + {b} = {a * k} + {b} = {answer_text}."
        ),
        estimated_seconds=45,
        author_metadata={
            "kind": "evaluate",
            "expression": f"{a}*x + {b}",
            "variable": "x",
            "at": k,
            "expected": answer_text,
        },
        license=_source_license(),
    )


_GENERATORS = {
    "linear_equations": generate_linear_item,
    "systems_equations": generate_systems_item,
    "ratios_percentages": generate_rates_item,
    "functions_models": None,
}


def generate_item(manifest_row: dict) -> dict:
    skill = manifest_row["target_skill"]
    if skill not in _GENERATORS:
        from .expansion import generate_expansion_item

        item = generate_expansion_item(manifest_row)
        from .contracts import content_hash

        item["content_hash"] = content_hash(item)
        return item
    subskill = manifest_row.get("target_subskill") or ""
    if skill == "functions_models":
        generator = (
            generate_function_evaluation_item
            if subskill == "function_evaluation"
            else generate_algebraic_models_item
        )
    else:
        generator = _GENERATORS[skill]
    item = generator(manifest_row)
    from .contracts import content_hash

    item["content_hash"] = content_hash(item)
    return item


# Per-skill lesson content. The two micro_lessons and two worked_examples
# per skill are deliberately distinct (plan section 8.2: lessons remediate
# misconceptions, worked examples demonstrate subskills); the pair must
# never be byte-identical duplicates.
def _lesson_content(skill: str, kind: str, index: int) -> tuple[str, str, str]:
    if skill == "linear_equations":
        if kind == "micro_lesson":
            if index == 1:
                return (
                    "Solving Linear Equations",
                    (
                        "To solve ax + b = c, isolate the variable term by subtracting b "
                        "from both sides, then divide both sides by a. Check by substituting "
                        "the solution back into the original equation."
                    ),
                    "isolate_variables",
                )
            return (
                "Avoiding Sign Errors When Isolating",
                (
                    "A sign error happens when a term crosses the equals sign with the wrong "
                    "sign. For 8x - 1 = 87, add 1 to both sides so the left side becomes 8x "
                    "only: 8x = 88, not 8x = 86. Always state which inverse operation you "
                    "apply before rewriting the line."
                ),
                "isolate_variables",
            )
        if index == 1:
            return (
                "Solving a Linear Equation — Worked Example",
                "For 3x + 5 = 17: subtract 5 from both sides to get 3x = 12, then x = 4.",
                "isolate_variables",
            )
        return (
            "Solving with a Negative Constant — Worked Example",
            "For 8x - 1 = 87: add 1 to both sides to get 8x = 88, then x = 11.",
            "isolate_variables",
        )
    if skill == "systems_equations":
        if kind == "micro_lesson":
            if index == 1:
                return (
                    "Solving Systems of Equations",
                    (
                        "A system of two linear equations can be solved by elimination: "
                        "multiply one equation so a variable matches, subtract to eliminate "
                        "it, solve for one variable, then substitute back."
                    ),
                    "solve_systems",
                )
            return (
                "Eliminating When Coefficients Do Not Match",
                (
                    "When neither variable has matching coefficients, multiply one equation "
                    "first so a variable matches, then subtract. For 4x + y = 30 and "
                    "4x + 3y = 42 the x coefficients already match: subtract the first "
                    "equation from the second to get 2y = 12, so y = 6, then x = 6."
                ),
                "solve_systems",
            )
        if index == 1:
            return (
                "Solving a System — Worked Example",
                "For x + y = 5 and x - y = 1: adding gives 2x = 6, so x = 3, then y = 2.",
                "solve_systems",
            )
        return (
            "System with Matching Coefficient — Worked Example",
            "For 4x + y = 30 and 4x + 3y = 42: subtract to eliminate x, giving 2y = 12, "
            "so y = 6, then x = 6.",
            "solve_systems",
        )
    if skill == "ratios_percentages":
        if kind == "micro_lesson":
            if index == 1:
                return (
                    "Unit Rates and Proportional Reasoning",
                    (
                        "A unit rate expresses a quantity per one unit of another quantity. "
                        "Set up a proportion so the units match, then solve for the unknown."
                    ),
                    "unit_rates",
                )
            return (
                "Avoiding Ratio Inversion",
                (
                    "Keep the same quantity in the numerator of both ratios. If 20 parts "
                    "take 2 minutes, write 20/2 = 10 parts per minute; in 8 minutes the "
                    "machine makes 10 * 8 = 80 parts. Swapping the units inverts the ratio."
                ),
                "unit_rates",
            )
        if index == 1:
            return (
                "Unit Rate — Worked Example",
                "At 30 miles per 2 hours, the unit rate is 30 / 2 = 15 miles per hour.",
                "unit_rates",
            )
        return (
            "Parts per Minute — Worked Example",
            "A machine makes 20 parts in 2 minutes: 20 / 2 = 10 parts per minute, so in "
            "8 minutes it makes 10 * 8 = 80 parts.",
            "unit_rates",
        )
    # functions_models
    if kind == "micro_lesson":
        if index == 1:
            return (
                "Modeling Situations with Functions",
                (
                    "Linear models have the form f(x) = mx + b. The slope m is the rate "
                    "of change and b is the starting value."
                ),
                "algebraic_models",
            )
        return (
            "Evaluating Functions by Substitution",
            (
                "To evaluate a function, replace x with the given value everywhere it "
                "appears, then simplify. For f(x) = 7x - 6, f(-4) = 7(-4) - 6 = -34."
            ),
            "function_evaluation",
        )
    if index == 1:
        return (
            "Function Evaluation — Worked Example",
            "For f(x) = 2x + 3, f(5) = 2(5) + 3 = 13.",
            "function_evaluation",
        )
    return (
        "Evaluating at a Negative Value — Worked Example",
        "For f(x) = 7x - 6, f(-4) = 7(-4) - 6 = -34.",
        "function_evaluation",
    )


def generate_lessons(
    manifest_rows: list[dict],
    out_dir: Path = DRAFTS_DIR,
) -> list[dict]:
    lessons: list[dict] = []
    seen_skills = sorted({row["target_skill"] for row in manifest_rows})
    lesson_misconceptions = {
        "linear_equations": ["sign_error", "inverse_operation_error"],
        "systems_equations": ["sign_error", "inverse_operation_error"],
        "ratios_percentages": ["ratio_inversion", "unit_conversion"],
        "functions_models": ["slope_intercept_swap", "input_substitution"],
    }
    for skill in seen_skills:
        for kind in ["micro_lesson", "worked_example"]:
            lesson_count = 1 if skill not in _GENERATORS else 2
            for index in range(1, lesson_count + 1):
                if skill in _GENERATORS:
                    title, body, subskill = _lesson_content(skill, kind, index)
                    target_misconceptions = lesson_misconceptions[skill]
                else:
                    from .expansion import expansion_lesson

                    title, body, subskill, target_misconceptions = expansion_lesson(
                        skill, kind
                    )
                content_id = f"math.{skill}.{kind}.{index:03d}"
                lesson = {
                    "id": content_id,
                    # Historical lessons use version 2. Targeted lesson
                    # metadata/body changes therefore ship as version 3.
                    "version": 4,
                    "schema_version": SCHEMA_VERSION,
                    "domain": "math",
                    "content_type": kind,
                    "target_skill": skill,
                    "target_subskill": subskill,
                    "target_misconceptions": target_misconceptions,
                    "required_prerequisites": PREREQUISITES[skill],
                    "difficulty": 1,
                    "title": title,
                    "body": body,
                    "estimated_seconds": 120,
                    "source_lineage": {
                        "source_id": "bridgesat_original",
                        "lineage_id": f"bridgesat-original:{content_id}",
                        "role": "concept_source_only",
                    },
                    "license": _source_license(),
                    "review_status": DRAFT_STATUS,
                    "reviewers": {},
                }
                from .contracts import content_hash

                lesson["content_hash"] = content_hash(lesson)
                lessons.append(lesson)
    return lessons


def write_drafts(items: list[dict], lessons: list[dict], out_dir: Path = DRAFTS_DIR) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    items_path = out_dir / QUESTIONS_FILE
    lessons_path = out_dir / LESSONS_FILE
    items_path.write_text(
        "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in items),
        encoding="utf-8",
    )
    lessons_path.write_text(
        "".join(json.dumps(lesson, ensure_ascii=False, sort_keys=True) + "\n" for lesson in lessons),
        encoding="utf-8",
    )
    return items_path, lessons_path


def generate_all_drafts(
    manifest_rows: list[dict],
    out_dir: Path = DRAFTS_DIR,
) -> tuple[list[dict], list[dict]]:
    items = [generate_item(row) for row in manifest_rows]
    lessons = generate_lessons(manifest_rows, out_dir=out_dir)
    write_drafts(items, lessons, out_dir=out_dir)
    return items, lessons
