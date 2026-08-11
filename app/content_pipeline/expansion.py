"""Deterministic first-party content for the 2026 competition expansion.

These candidates are BridgeSAT-authored educational content. They do not
derive wording, numbers, or answer options from an external SAT question bank.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sympy import Rational

from .contracts import CANDIDATES_DIR, PREREQUISITES
from .generation import _base_item, _choices, _hints, _source_license, rng_for

EXPANSION_FILE = "math-expansion-v2.jsonl"
EXPANSION_SOURCE_ID = "bridgesat_original"
EXPANSION_SKILLS = (
    "inequalities",
    "quadratic_equations",
    "exponents_radicals",
    "coordinate_geometry",
)
DIFFICULTIES = (1, 1, 1, 1, 2, 2, 2, 2, 2, 3, 3, 3)

_SUBSKILLS = {
    "inequalities": ("solve_inequalities", "interpret_solution_sets"),
    "quadratic_equations": ("factor_quadratics", "analyze_roots"),
    "exponents_radicals": ("apply_exponent_rules", "simplify_radicals"),
    "coordinate_geometry": ("slope_distance", "midpoint_lines"),
}

_LESSONS = {
    "inequalities": {
        "micro_lesson": (
            "Keep the Inequality Direction Honest",
            "Solve an inequality as you would an equation, but reverse the inequality sign "
            "when multiplying or dividing both sides by a negative number. A strict sign "
            "(< or >) excludes the boundary; an inclusive sign (≤ or ≥) includes it.",
            "solve_inequalities",
            ["inequality_sign_flip", "boundary_inclusion_error"],
        ),
        "worked_example": (
            "Negative-Coefficient Inequality — Worked Example",
            "Solve -3x + 5 ≤ -7. Subtract 5 to get -3x ≤ -12. Divide by -3 and reverse "
            "the sign: x ≥ 4. This uses the same inverse operation on both sides; because 4 "
            "satisfies the original inequality, the boundary is included.",
            "solve_inequalities",
            [
                "inequality_sign_flip",
                "boundary_inclusion_error",
                "inverse_operation_error",
                "arithmetic_error",
            ],
        ),
    },
    "quadratic_equations": {
        "micro_lesson": (
            "Read Both Roots from the Factors",
            "If a quadratic factors as (x - p)(x - q) = 0, the zero-product rule gives "
            "x = p or x = q. Track the signs inside each factor and keep both roots unless "
            "the problem context rules one out.",
            "factor_quadratics",
            ["factoring_sign_error", "missing_second_root", "arithmetic_error"],
        ),
        "worked_example": (
            "Two Roots from One Quadratic — Worked Example",
            "For x² - 7x + 12 = 0, factor to (x - 3)(x - 4) = 0. Therefore x = 3 "
            "or x = 4. Checking both values in the original equation confirms both roots.",
            "factor_quadratics",
            ["factoring_sign_error", "missing_second_root", "arithmetic_error"],
        ),
    },
    "exponents_radicals": {
        "micro_lesson": (
            "Choose the Exponent Rule by Operation",
            "With the same base, multiply by adding exponents and divide by subtracting "
            "exponents. A negative exponent means reciprocal. For radicals, pull out only "
            "complete square factors.",
            "apply_exponent_rules",
            ["exponent_rule_confusion", "negative_exponent_error", "radical_simplification_error"],
        ),
        "worked_example": (
            "Exponent Rules and Radicals — Worked Example",
            "x⁵/x² = x³ because 5 - 2 = 3. Also, √72 = √(36·2) = 6√2. The first "
            "step uses quotient exponents; the second extracts the largest square factor. "
            "For a negative exponent, 1/x³ = x⁻³: the reciprocal changes the exponent's sign.",
            "apply_exponent_rules",
            ["exponent_rule_confusion", "negative_exponent_error", "radical_simplification_error"],
        ),
    },
    "coordinate_geometry": {
        "micro_lesson": (
            "Match Each Coordinate Formula to Its Job",
            "Slope is change in y divided by change in x. Distance squares both coordinate "
            "changes before taking a square root. A midpoint averages the x-coordinates and "
            "the y-coordinates separately.",
            "slope_distance",
            ["slope_sign_error", "distance_formula_error", "midpoint_formula_error"],
        ),
        "worked_example": (
            "Slope, Distance, and Midpoint — Worked Example",
            "From (1, 2) to (4, 6), slope = (6 - 2)/(4 - 1) = 4/3 and distance = "
            "√(3² + 4²) = 5. The midpoint is ((1 + 4)/2, (2 + 6)/2) = (5/2, 4). "
            "For y = mx + b through (4, 6), substitute x=4 and y=6 before isolating b; "
            "keeping coordinate roles explicit prevents substitution and arithmetic errors.",
            "slope_distance",
            [
                "slope_sign_error",
                "distance_formula_error",
                "midpoint_formula_error",
                "input_substitution",
                "arithmetic_error",
            ],
        ),
    },
}


def expansion_manifest_rows() -> list[dict]:
    """Return the immutable-shape manifest rows for 48 original candidates."""
    rows: list[dict] = []
    for skill in EXPANSION_SKILLS:
        for number in range(1, 13):
            subskill_index = 0 if number <= 6 else 1
            if skill == "exponents_radicals" and number >= 10:
                subskill_index = 0
            rows.append(
                {
                    "content_id": f"math.{skill}.{number:03d}",
                    "lineage_id": f"bridgesat-expansion-v2:{skill}:{number:03d}",
                    "source_id": EXPANSION_SOURCE_ID,
                    "target_skill": skill,
                    "target_subskill": _SUBSKILLS[skill][subskill_index],
                    "selection_rank": number,
                    "skill_total": 12,
                    "mapping_method": "authored_content_expansion",
                    "concept_source_only": True,
                }
            )
    return rows


def write_expansion_manifest(path: Path = CANDIDATES_DIR / EXPANSION_FILE) -> Path:
    """Write the reproducible first-party candidate manifest."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
        for row in expansion_manifest_rows()
    )
    if path.exists() and path.read_text(encoding="utf-8") != body:
        raise FileExistsError(f"Refusing to mutate expansion manifest: {path}")
    checksum = hashlib.sha256(body.encode("utf-8")).hexdigest() + "\n"
    checksum_path = path.with_suffix(".checksum")
    if checksum_path.exists() and checksum_path.read_text(encoding="utf-8") != checksum:
        raise FileExistsError(f"Expansion manifest checksum mismatch: {checksum_path}")
    path.write_text(body, encoding="utf-8")
    checksum_path.write_text(checksum, encoding="utf-8")
    return path


def _role_metadata(skill: str, number: int, metadata: dict) -> dict:
    group = (number - 1) // 3 + 1
    role = {1: "trigger", 2: "transfer"}.get((number - 1) % 3 + 1, "practice")
    return {
        **metadata,
        "transfer_group": f"{skill}-path-{group}",
        "instruction_role": role,
        "authorship": "bridgesat_original",
    }


def _make_item(
    row: dict,
    *,
    prompt: str,
    answer: object,
    distractors: list[object],
    misconceptions: list[str],
    hints: list[str],
    explanation: str,
    metadata: dict,
    seconds: int,
) -> dict:
    number = int(row["content_id"].rsplit(".", 1)[1])
    rng = rng_for(row["lineage_id"], "expansion-v2")
    choices, answer_choice_id, misconception_map = _choices(
        answer, distractors, misconceptions, rng
    )
    return _base_item(
        lineage_id=row["lineage_id"],
        content_id=row["content_id"],
        skill=row["target_skill"],
        subskill=row["target_subskill"],
        difficulty=DIFFICULTIES[number - 1],
        prompt=prompt,
        choices=choices,
        answer_choice_id=answer_choice_id,
        misconception_map=misconception_map,
        hints=_hints(hints),
        worked_explanation=explanation,
        estimated_seconds=seconds,
        author_metadata=_role_metadata(row["target_skill"], number, metadata),
        license=_source_license(),
        source_id=EXPANSION_SOURCE_ID,
    )


def _formula_metadata(verifier: str, expected: object, **params: object) -> dict:
    """Record source parameters so validation independently recomputes answers."""
    return {
        "kind": "expansion_formula",
        "verifier": verifier,
        "params": params,
        "expected": str(expected),
    }


def _inequality_item(row: dict, number: int) -> dict:
    variant = (number - 1) % 3
    group = (number - 1) // 3
    if group == 0:
        a, boundary, b = 2 + variant, 3 + 2 * variant, 1 + variant
        rhs = a * boundary + b
        answer = boundary + 1
        return _make_item(
            row,
            prompt=f"What is the least integer x that satisfies {a}x + {b} > {rhs}?",
            answer=answer,
            distractors=[boundary, -answer, a * (rhs - b)],
            misconceptions=["boundary_inclusion_error", "inequality_sign_flip", "inverse_operation_error"],
            hints=["Isolate the variable term while preserving the inequality direction.", f"Subtract {b}, giving {a}x > {rhs - b}.", f"The boundary is x > {boundary}; choose the next integer."],
            explanation=f"Subtract {b} and divide by {a}: x > {boundary}. The boundary is excluded, so the least integer solution is {answer}.",
            metadata=_formula_metadata(
                "least_integer_strict_inequality", answer, a=a, b=b, rhs=rhs
            ),
            seconds=60,
        )
    if group == 1:
        a, boundary, b = 2 + variant, 2 + variant, 12 + variant
        rhs = b - a * boundary
        answer = boundary
        return _make_item(
            row,
            prompt=f"What is the least integer satisfying -{a}x + {b} ≤ {rhs}?",
            answer=answer,
            distractors=[boundary - 1, -boundary, a * (b - rhs)],
            misconceptions=["boundary_inclusion_error", "inequality_sign_flip", "inverse_operation_error"],
            hints=[f"Subtract {b} from both sides.", f"Divide -{a}x ≤ {-a * boundary} by a negative number and reverse the sign.", f"The result is x ≥ {boundary}, so include the boundary."],
            explanation=f"After subtracting {b}, -{a}x ≤ {-a * boundary}. Dividing by -{a} reverses the sign: x ≥ {boundary}. Thus {answer} is the least integer solution.",
            metadata=_formula_metadata(
                "least_integer_negative_inequality", answer, a=a, b=b, rhs=rhs
            ),
            seconds=70,
        )
    if group == 2:
        lower, upper = -2 + variant, 4 + 2 * variant
        answer = upper - lower
        return _make_item(
            row,
            prompt=f"How many integers satisfy {lower} < x ≤ {upper}?",
            answer=answer,
            distractors=[answer - 1, answer + 1, answer + 2],
            misconceptions=["boundary_inclusion_error", "boundary_inclusion_error", "arithmetic_error"],
            hints=["List which endpoint is excluded and which is included.", f"The integers begin at {lower + 1} and end at {upper}.", f"Use last - first + 1: {upper} - ({lower + 1}) + 1."],
            explanation=f"The allowed integers run from {lower + 1} through {upper}. Their count is {upper} - ({lower + 1}) + 1 = {answer}.",
            metadata=_formula_metadata(
                "integer_count_open_closed", answer, lower=lower, upper=upper
            ),
            seconds=75,
        )
    price, fee, answer = 4 + variant, 7 + 2 * variant, 8 + variant
    budget = fee + price * answer + variant
    return _make_item(
        row,
        prompt=f"A club pays a ${fee} setup fee and ${price} per notebook. With at most ${budget}, what is the greatest number of whole notebooks it can buy?",
        answer=answer,
        distractors=[answer + 1, -answer, price * (budget - fee)],
        misconceptions=["boundary_inclusion_error", "inequality_sign_flip", "inverse_operation_error"],
        hints=[f"Model the cost as {fee} + {price}n ≤ {budget}.", f"Subtract the setup fee: {price}n ≤ {budget - fee}.", "Divide by the per-notebook cost, then use a whole number that stays within the budget."],
        explanation=f"{fee} + {price}n ≤ {budget} gives {price}n ≤ {budget - fee}, so n ≤ {(budget - fee) // price}. The greatest whole-number choice is {answer}.",
        metadata=_formula_metadata(
            "maximum_whole_budget", answer, fee=fee, price=price, budget=budget
        ),
        seconds=90,
    )


def _quadratic_item(row: dict, number: int) -> dict:
    variant = (number - 1) % 3
    group = (number - 1) // 3
    if group == 0:
        small, large = 2 + variant, 5 + 2 * variant
        total, product = small + large, small * large
        return _make_item(
            row,
            prompt=f"What is the greater solution of x² - {total}x + {product} = 0?",
            answer=large,
            distractors=[small, -large, total],
            misconceptions=["missing_second_root", "factoring_sign_error", "arithmetic_error"],
            hints=[f"Find two numbers whose product is {product} and sum is {total}.", f"Factor as (x - {small})(x - {large}) = 0.", "Set each factor equal to zero and compare the two roots."],
            explanation=f"The quadratic factors as (x - {small})(x - {large}) = 0, so the roots are {small} and {large}. The greater solution is {large}.",
            metadata=_formula_metadata(
                "greater_quadratic_root", large, root_a=small, root_b=large
            ),
            seconds=75,
        )
    if group == 1:
        left, right = -(2 + variant), 5 + 2 * variant
        total, product = left + right, left * right
        return _make_item(
            row,
            prompt=f"The equation x² - {total}x {product:+d} = 0 has two solutions. What is their sum?",
            answer=total,
            distractors=[right, -total, product],
            misconceptions=["missing_second_root", "factoring_sign_error", "arithmetic_error"],
            hints=["For x² + bx + c = 0, the sum of the roots is -b.", f"Here the coefficient of x is {-total}.", f"The roots are {left} and {right}; add both."],
            explanation=f"The roots sum to -({-total}) = {total}. Equivalently, {left} + {right} = {total}.",
            metadata=_formula_metadata(
                "quadratic_root_sum", total, coefficient_a=1, coefficient_b=-total
            ),
            seconds=80,
        )
    if group == 2:
        width, extra = 3 + variant, 4 + variant
        area = width * (width + extra)
        other = -(width + extra)
        return _make_item(
            row,
            prompt=f"A rectangle has width x and length x + {extra}. Its area is {area}. What positive value of x gives these dimensions?",
            answer=width,
            distractors=[other, -width, width + extra],
            misconceptions=["missing_second_root", "factoring_sign_error", "arithmetic_error"],
            hints=[f"Write x(x + {extra}) = {area}.", f"Move all terms to one side and factor using the known product {area}.", "A geometric length must be positive."],
            explanation=f"x(x + {extra}) = {area} becomes x² + {extra}x - {area} = 0 = (x - {width})(x + {width + extra}). The roots are {width} and {other}; only {width} is a positive length.",
            metadata=_formula_metadata(
                "positive_rectangle_root", width, extra=extra, area=area
            ),
            seconds=95,
        )
    positive, magnitude = 2 + variant, 4 + variant
    negative = -magnitude
    product = positive * negative
    return _make_item(
        row,
        prompt=f"If (x - {positive})(x + {magnitude}) = 0, what is the product of all solutions?",
        answer=product,
        distractors=[positive, -product, positive + negative],
        misconceptions=["missing_second_root", "factoring_sign_error", "arithmetic_error"],
        hints=["Use the zero-product rule on both factors.", f"The roots are {positive} and {-magnitude}.", "Multiply the two roots, including the negative sign."],
        explanation=f"The solutions are x = {positive} and x = {-magnitude}. Their product is {positive}({-magnitude}) = {product}.",
        metadata=_formula_metadata(
            "factored_root_product", product, positive=positive, magnitude=magnitude
        ),
        seconds=85,
    )


def _exponents_item(row: dict, number: int) -> dict:
    variant = (number - 1) % 3
    group = (number - 1) // 3
    if group == 0:
        first, second = 2 + variant, 4 + 2 * variant
        answer = first + second
        return _make_item(
            row,
            prompt=f"For nonzero y, y^{first} · y^{second} = y^n. What is n?",
            answer=answer,
            distractors=[first * second, second - first, -answer],
            misconceptions=["exponent_rule_confusion", "exponent_rule_confusion", "negative_exponent_error"],
            hints=["The factors have the same base.", "Multiplying equal bases adds their exponents.", f"n = {first} + {second}."],
            explanation=f"The product rule gives y^{first} · y^{second} = y^({first} + {second}) = y^{answer}, so n = {answer}.",
            metadata=_formula_metadata(
                "exponent_product", answer, first=first, second=second
            ),
            seconds=45,
        )
    if group == 1:
        numerator, denominator = 8 + 2 * variant, 3 + variant
        answer = numerator - denominator
        return _make_item(
            row,
            prompt=f"For z ≠ 0, z^{numerator}/z^{denominator} = z^k. What is k?",
            answer=answer,
            distractors=[numerator + denominator, denominator - numerator, numerator * denominator],
            misconceptions=["exponent_rule_confusion", "negative_exponent_error", "exponent_rule_confusion"],
            hints=["The quotient has the same nonzero base.", "Subtract the denominator exponent from the numerator exponent.", f"k = {numerator} - {denominator}."],
            explanation=f"The quotient rule gives z^({numerator} - {denominator}) = z^{answer}. Therefore k = {answer}.",
            metadata=_formula_metadata(
                "exponent_quotient", answer, numerator=numerator, denominator=denominator
            ),
            seconds=50,
        )
    coefficient, remainder = 3 + variant, (2, 3, 5)[variant]
    radicand = coefficient * coefficient * remainder
    return _make_item(
        row,
        prompt=f"When √{radicand} is written as a√{remainder}, what is the positive value of a?",
        answer=coefficient,
        distractors=[coefficient * coefficient, 1, radicand],
        misconceptions=["radical_simplification_error", "radical_simplification_error", "exponent_rule_confusion"],
        hints=["Find the largest perfect-square factor of the radicand.", f"{radicand} = {coefficient * coefficient} · {remainder}.", f"√{coefficient * coefficient} = {coefficient}."],
        explanation=f"√{radicand} = √({coefficient * coefficient}·{remainder}) = {coefficient}√{remainder}, so a = {coefficient}.",
        metadata=_formula_metadata(
            "radical_coefficient",
            coefficient,
            radicand=radicand,
            remainder=remainder,
        ),
        seconds=70,
    )


def _negative_exponent_item(row: dict, number: int) -> dict:
    variant = (number - 1) % 3
    exponent = 3 + 2 * variant
    answer = -exponent
    return _make_item(
        row,
        prompt=f"For q ≠ 0, 1/q^{exponent} = q^m. What is m?",
        answer=answer,
        distractors=[exponent, Rational(1, exponent), 0],
        misconceptions=["negative_exponent_error", "exponent_rule_confusion", "exponent_rule_confusion"],
        hints=["A reciprocal power can be written with a negative exponent.", f"Move q^{exponent} from the denominator by changing the exponent's sign.", f"1/q^{exponent} = q^(-{exponent})."],
        explanation=f"By the negative-exponent rule, 1/q^{exponent} = q^(-{exponent}). Thus m = {answer}.",
        metadata=_formula_metadata(
            "negative_exponent", answer, exponent=exponent
        ),
        seconds=60,
    )


def _coordinate_item(row: dict, number: int) -> dict:
    variant = (number - 1) % 3
    group = (number - 1) // 3
    if group == 0:
        x1, y1 = -2 + variant, 1 + variant
        dx, slope = 2 + variant, -4 + variant
        x2, y2 = x1 + dx, y1 + slope * dx
        answer = Rational(y2 - y1, x2 - x1)
        reciprocal = Rational(x2 - x1, y2 - y1) if y2 != y1 else 1
        return _make_item(
            row,
            prompt=f"What is the slope of the line through ({x1}, {y1}) and ({x2}, {y2})?",
            answer=answer,
            distractors=[-answer, reciprocal, (y2 - y1) + (x2 - x1)],
            misconceptions=["slope_sign_error", "input_substitution", "arithmetic_error"],
            hints=["Use change in y divided by change in x in the same point order.", f"The y-change is {y2} - ({y1}); the x-change is {x2} - ({x1}).", f"Divide {y2 - y1} by {x2 - x1}."],
            explanation=f"m = ({y2} - {y1})/({x2} - ({x1})) = {y2 - y1}/{x2 - x1} = {answer}.",
            metadata=_formula_metadata(
                "slope", answer, x1=x1, y1=y1, x2=x2, y2=y2
            ),
            seconds=65,
        )
    if group == 1:
        scale = 1 + variant
        x1, y1 = variant - 2, variant + 1
        x2, y2 = x1 + 3 * scale, y1 + 4 * scale
        answer = 5 * scale
        return _make_item(
            row,
            prompt=f"What is the distance between ({x1}, {y1}) and ({x2}, {y2}) in the coordinate plane?",
            answer=answer,
            distractors=[7 * scale, 25 * scale * scale, -answer],
            misconceptions=["distance_formula_error", "distance_formula_error", "input_substitution"],
            hints=["Find the horizontal and vertical changes first.", f"The changes are {3 * scale} and {4 * scale}.", "Apply √(Δx² + Δy²), not Δx + Δy."],
            explanation=f"The distance is √(({3 * scale})² + ({4 * scale})²) = √{25 * scale * scale} = {answer}.",
            metadata=_formula_metadata(
                "distance", answer, x1=x1, y1=y1, x2=x2, y2=y2
            ),
            seconds=75,
        )
    if group == 2:
        x1, x2 = -4 + 2 * variant, 6 + 4 * variant
        y1, y2 = 1 + variant, 7 + 3 * variant
        answer = Rational(x1 + x2, 2)
        return _make_item(
            row,
            prompt=f"The midpoint of the segment from ({x1}, {y1}) to ({x2}, {y2}) is (m, n). What is m?",
            answer=answer,
        distractors=[x1 + x2, Rational(x1 + x2, 4), Rational(y1 + y2, 2)],
            misconceptions=["midpoint_formula_error", "midpoint_formula_error", "input_substitution"],
            hints=["The first midpoint coordinate uses only the two x-values.", f"Average {x1} and {x2}.", f"m = ({x1} + {x2})/2."],
            explanation=f"Average the x-coordinates: m = ({x1} + {x2})/2 = {answer}. The y-values are not used to find m.",
            metadata=_formula_metadata(
                "midpoint_x", answer, x1=x1, x2=x2
            ),
            seconds=65,
        )
    slope, x, y = -3 + variant, 2 + variant, 5 + 2 * variant
    intercept = y - slope * x
    return _make_item(
        row,
        prompt=f"A line with slope {slope} passes through ({x}, {y}). In y = mx + b, what is b?",
        answer=intercept,
        distractors=[-intercept, slope * x + y, slope],
        misconceptions=["slope_sign_error", "input_substitution", "arithmetic_error"],
        hints=["Substitute the point and slope into y = mx + b.", f"Write {y} = ({slope})({x}) + b.", "Isolate b by subtracting mx from y."],
        explanation=f"Substitute the point: {y} = ({slope})({x}) + b. Therefore b = {y} - ({slope})({x}) = {intercept}.",
        metadata=_formula_metadata(
            "line_intercept", intercept, slope=slope, x=x, y=y
        ),
        seconds=80,
    )


def generate_expansion_item(row: dict) -> dict:
    number = int(row["content_id"].rsplit(".", 1)[1])
    skill = row["target_skill"]
    if skill == "inequalities":
        return _inequality_item(row, number)
    if skill == "quadratic_equations":
        return _quadratic_item(row, number)
    if skill == "exponents_radicals":
        return _negative_exponent_item(row, number) if number >= 10 else _exponents_item(row, number)
    if skill == "coordinate_geometry":
        return _coordinate_item(row, number)
    raise KeyError(f"Unsupported expansion skill: {skill}")


def expansion_lesson(skill: str, kind: str) -> tuple[str, str, str, list[str]]:
    return _LESSONS[skill][kind]
