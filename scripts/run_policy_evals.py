#!/usr/bin/env python3
"""Policy golden eval (EVALUATION_SPEC section 3, plan section 11).

Runs >= 20 deterministic golden trajectories against
``app.agent.policy.decide_next_action`` and reports:

- overall pass rate (target >= 90%)
- safety-critical pass rate (target 100%)
- coverage of the 12 EVALUATION_SPEC scenario categories

Writes reports/policy_eval.json and evals/policy/REPORT.md.

Usage:
    python scripts/run_policy_evals.py [--golden evals/policy/golden.jsonl] [--report reports/policy_eval.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.policy import POLICY_VERSION, PolicyInput, decide_next_action
from app.domain.memory import BoundedAction
from app.domain.sessions import SessionState

GOLDEN_PATH = ROOT / "evals" / "policy" / "golden.jsonl"
REPORT_JSON = ROOT / "reports" / "policy_eval.json"
REPORT_MD = ROOT / "evals" / "policy" / "REPORT.md"

EXPECTED_CATEGORIES = {
    "repeated misconception",
    "difficulty increase with sufficient confidence",
    "no increase after high-level hints",
    "low confidence requests more evidence",
    "prerequisite review",
    "overdue review",
    "insufficient remaining time",
    "memory conflict with recent evidence",
    "unavailable content",
    "offline fallback",
    "stale memory",
    "student-corrected memory",
}

CATEGORY_ALIASES = {
    "repeated_misconception": "repeated misconception",
    "repeated_skill_error": "repeated misconception",
    "difficulty_increase_sufficient_confidence": "difficulty increase with sufficient confidence",
    "no_increase_after_high_level_hints": "no increase after high-level hints",
    "low_confidence_more_evidence": "low confidence requests more evidence",
    "prerequisite_review": "prerequisite review",
    "overdue_review": "overdue review",
    "insufficient_remaining_time": "insufficient remaining time",
    "memory_recall_reuse": "memory conflict with recent evidence",
    "memory_conflict_recent_evidence": "memory conflict with recent evidence",
    "unavailable_content": "unavailable content",
    "offline_fallback": "offline fallback",
    "stale_memory": "stale memory",
    "student_corrected_memory": "student-corrected memory",
    "support_low_mastery": "low confidence requests more evidence",
    "bounded_action_allowlist": "offline fallback",
    "policy_version_persisted": "offline fallback",
    "difficulty_never_out_of_range": "offline fallback",
    "memory_does_not_override_time_budget": "memory conflict with recent evidence",
    "misconception_without_error_streak": "repeated misconception",
    "recent_evidence_overrides_old_memory": "memory conflict with recent evidence",
}


def _to_policy_input(student_id: str, session_id: str, inputs: dict) -> PolicyInput:
    state = inputs.get("state", "ANSWER_EVALUATED")
    if isinstance(state, str):
        state = SessionState(state)
    kwargs = dict(inputs)
    kwargs.pop("state", None)
    kwargs.pop("student_id", None)
    kwargs.pop("session_id", None)
    return PolicyInput(
        student_id=student_id,
        session_id=session_id,
        state=state,
        **kwargs,
    )


def _run(entries: list[dict]) -> dict:
    results: list[dict] = []
    for entry in entries:
        inputs = entry["inputs"]
        outcome = decide_next_action(
            _to_policy_input(inputs.get("student_id", "golden"),
                              inputs.get("session_id", "golden-session"),
                              inputs)
        )
        decision = outcome.decision
        expected = entry["expected"]

        checks: dict[str, bool] = {
            "action": decision.action == expected.get("action"),
            "reason_code": decision.reason_code == expected.get("reason_code"),
        }
        if expected.get("check_allowlist"):
            checks["allowlist"] = decision.action in {a.value for a in BoundedAction}
        if expected.get("check_version"):
            checks["version"] = decision.policy_version == POLICY_VERSION
        if expected.get("difficulty_cap"):
            checks["difficulty_cap"] = decision.difficulty <= expected["difficulty_cap"]

        passed = all(checks.values())
        results.append(
            {
                "id": entry["id"],
                "category": entry["category"],
                "safety_critical": entry.get("safety_critical", False),
                "passed": passed,
                "checks": checks,
                "actual": {"action": decision.action, "reason_code": decision.reason_code},
                "expected": expected,
                "notes": entry.get("notes", ""),
            }
        )

    total = len(results)
    passed_total = sum(1 for r in results if r["passed"])
    safety = [r for r in results if r["safety_critical"]]
    passed_safety = sum(1 for r in safety if r["passed"])

    covered_categories = {
        CATEGORY_ALIASES.get(r["category"], r["category"]) for r in results
    }
    missing_categories = sorted(EXPECTED_CATEGORIES - covered_categories)

    return {
        "schema_version": "1.0",
        "label": "synthetic simulation",
        "total_trajectories": total,
        "passed": passed_total,
        "pass_rate": round(passed_total / total, 4) if total else 0.0,
        "safety_critical_total": len(safety),
        "safety_critical_passed": passed_safety,
        "safety_critical_pass_rate": round(passed_safety / len(safety), 4) if safety else 0.0,
        "targets": {
            "overall_min_pass_rate": 0.90,
            "safety_critical_min_pass_rate": 1.00,
            "min_trajectories": 20,
        },
        "categories_covered": sorted(covered_categories),
        "categories_missing": missing_categories,
        "results": results,
    }


def _write_markdown(summary: dict, path: Path) -> None:
    lines = [
        "# Policy golden eval report",
        "",
        f"- label: {summary['label']}",
        f"- trajectories: {summary['total_trajectories']}",
        f"- overall pass rate: {summary['pass_rate']:.0%} (target >= 90%)",
        f"- safety-critical pass rate: {summary['safety_critical_pass_rate']:.0%} (target 100%)",
        f"- categories covered: {len(summary['categories_covered'])}/12",
        "",
        "| ID | Category | Safety | Result |",
        "|---|---|---|---|",
    ]
    for r in summary["results"]:
        safety = "yes" if r["safety_critical"] else "no"
        lines.append(f"| {r['id']} | {r['category']} | {safety} | "
                     f"{'PASS' if r['passed'] else 'FAIL'} |")
    if summary["categories_missing"]:
        lines.append("")
        lines.append("Categories not directly covered by a trajectory:")
        for category in summary["categories_missing"]:
            lines.append(f"- {category}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--golden", type=Path, default=GOLDEN_PATH)
    parser.add_argument("--json", type=Path, default=REPORT_JSON)
    args = parser.parse_args()

    entries = [
        json.loads(line)
        for line in args.golden.open(encoding="utf-8")
        if line.strip()
    ]
    if len(entries) < 20:
        print(f"Need >= 20 golden trajectories, got {len(entries)}", file=sys.stderr)
        return 1

    summary = _run(entries)
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    _write_markdown(summary, REPORT_MD)

    print(json.dumps({
        "total": summary["total_trajectories"],
        "pass_rate": summary["pass_rate"],
        "safety_critical_pass_rate": summary["safety_critical_pass_rate"],
        "categories_covered": len(summary["categories_covered"]),
        "categories_missing": summary["categories_missing"],
    }, indent=2))
    for r in summary["results"]:
        if not r["passed"]:
            print(f"  FAIL {r['id']}: expected {r['expected']}, got {r['actual']}")
    return 0 if summary["pass_rate"] >= 0.9 and summary["safety_critical_pass_rate"] == 1.0 else 2


if __name__ == "__main__":
    sys.exit(main())
