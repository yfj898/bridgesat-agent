"""Focused tests for the frozen Hybrid final decision gate."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.run_hybrid_final_gate import (  # noqa: E402
    FLAG_NAMES,
    build_final_gate,
)

HYBRID_REPORT = ROOT / "reports" / "hybrid_eval.json"
GATE_SCRIPT = ROOT / "scripts" / "run_hybrid_final_gate.py"


@pytest.fixture()
def hybrid_report() -> dict:
    return json.loads(HYBRID_REPORT.read_text(encoding="utf-8"))


def test_final_gate_freezes_deterministic_mode_and_records_no_go(hybrid_report: dict) -> None:
    gate = build_final_gate(hybrid_report)

    assert gate["gate_passed"] is True
    assert gate["final_mode"] == "deterministic"
    assert gate["action_ranking_decision"] == "No-Go"
    assert gate["frozen_feature_flags"] == {name: 0 for name in FLAG_NAMES}
    assert gate["source_schema_version"] == "1.0"
    assert gate["source_label"] == "controlled internal test"
    assert gate["report_shape"]["passed"] is True
    assert gate["safety_checks"]["passed"] is True
    assert gate["safety_checks"]["allowed_action_violations"] == 0
    assert gate["safety_checks"]["hallucinated_acceptance"] == 0
    assert gate["safety_checks"]["fallback_success_rate"] == 1.0
    assert gate["safety_checks"]["decisive_zero_model_calls"] == 1.0
    assert gate["safety_checks"]["explanation_grounding_accuracy"] == 1.0
    assert gate["safety_checks"]["summary_grounding_accuracy"] == 1.0
    assert gate["safety_checks"]["summary_unavailable_fallback_rate"] == 1.0
    assert gate["safety_checks"]["beneficial_difference_cases"] == ["h6-01"]
    assert "scripted" in gate["synthetic_latency_disclaimer"].lower()
    assert any("real-provider" in item for item in gate["missing_evidence"])
    assert gate["rollback_profile"]["frozen_feature_flags"] == gate["frozen_feature_flags"]


def test_final_gate_fails_nonzero_conditions_without_treating_no_go_as_failure(
    hybrid_report: dict,
) -> None:
    broken = copy.deepcopy(hybrid_report)
    broken["summary"]["accepted_allowed_action_violations"] = 1
    broken["summary"]["summary_variants"] = 4

    gate = build_final_gate(broken)

    assert gate["gate_passed"] is False
    assert gate["action_ranking_decision"] == "No-Go"
    assert gate["safety_checks"]["passed"] is False
    assert "allowed_action_violations" in gate["safety_checks"]["failed_checks"]
    assert "summary_variants" in gate["report_shape"]["errors"]


def test_final_gate_rejects_variant_that_does_not_match_golden(
    hybrid_report: dict,
) -> None:
    broken = copy.deepcopy(hybrid_report)
    variant = next(
        entry
        for result in broken["results"]
        for entry in result["variants"]
        if not entry["accepted"]
    )
    variant["accepted"] = True
    variant["pass"] = True

    gate = build_final_gate(broken)

    assert gate["gate_passed"] is False
    assert "variant_expectations" in gate["report_shape"]["errors"]


def test_final_gate_recomputes_derived_safety_metrics(hybrid_report: dict) -> None:
    broken = copy.deepcopy(hybrid_report)
    broken["summary"]["fallback_success_rate"] = 1.0
    broken["summary"]["decisive_zero_model_calls"] = 1.0
    broken["summary"]["summary_grounding_accuracy"] = 1.0
    broken["results"][0]["baseline_matches"] = False
    broken["results"][0]["baseline_action"] = "SHOW_MICRO_LESSON"
    broken["results"][2]["variants"][0]["calls"] = 1

    gate = build_final_gate(broken)

    assert gate["gate_passed"] is False
    assert "fallback_success_rate_recomputed" in gate["safety_checks"]["failed_checks"]
    assert "decisive_zero_model_calls_recomputed" in gate["safety_checks"]["failed_checks"]
    assert "result_0_baseline_matches" in gate["report_shape"]["errors"]


def test_final_gate_malformed_variant_is_structured_failure(hybrid_report: dict) -> None:
    broken = copy.deepcopy(hybrid_report)
    broken["results"][0]["variants"][0]["variant"] = []

    gate = build_final_gate(broken)

    assert gate["gate_passed"] is False
    assert gate["report_shape"]["passed"] is False
    assert "result_0_variant_0_variant_name" in gate["report_shape"]["errors"]


def test_final_gate_requires_top_level_variant_success(hybrid_report: dict) -> None:
    broken = copy.deepcopy(hybrid_report)
    broken["all_variants_passed"] = False

    gate = build_final_gate(broken)

    assert gate["gate_passed"] is False
    assert "all_variants_passed" in gate["report_shape"]["errors"]


def test_final_gate_recomputes_allowed_action_set(hybrid_report: dict) -> None:
    broken = copy.deepcopy(hybrid_report)
    broken["results"][0]["allowed_actions"] = ["SHOW_MICRO_LESSON"]

    gate = build_final_gate(broken)

    assert gate["gate_passed"] is False
    assert "policy_recompute" in gate["safety_checks"]["failed_checks"]


def test_final_gate_rejects_duplicate_variant_names(hybrid_report: dict) -> None:
    broken = copy.deepcopy(hybrid_report)
    variants = broken["results"][0]["variants"]
    variants[1] = copy.deepcopy(variants[0])

    gate = build_final_gate(broken)

    assert gate["gate_passed"] is False
    assert "duplicate_variant" in gate["report_shape"]["errors"]


def test_final_gate_requires_strict_summary_shape_types(hybrid_report: dict) -> None:
    broken = copy.deepcopy(hybrid_report)
    broken["summary"]["cases"] = 15.0

    gate = build_final_gate(broken)

    assert gate["gate_passed"] is False
    assert "cases" in gate["report_shape"]["errors"]


def test_final_gate_requires_strict_variant_boolean_types(hybrid_report: dict) -> None:
    broken = copy.deepcopy(hybrid_report)
    broken["results"][0]["variants"][0]["accepted"] = 1

    gate = build_final_gate(broken)

    assert gate["gate_passed"] is False
    assert "variant_expectations" in gate["report_shape"]["errors"]


def test_final_gate_requires_expected_proposed_action(hybrid_report: dict) -> None:
    broken = copy.deepcopy(hybrid_report)
    broken["results"][1]["variants"][0]["proposed_action"] = "SHOW_MICRO_LESSON"

    gate = build_final_gate(broken)

    assert gate["gate_passed"] is False
    assert "variant_expectations" in gate["report_shape"]["errors"]


def test_final_gate_requires_null_proposed_action_for_null_golden_proposal(
    hybrid_report: dict,
) -> None:
    broken = copy.deepcopy(hybrid_report)
    variant = next(
        entry
        for result in broken["results"]
        for entry in result["variants"]
        if entry["proposed_action"] is None
    )
    variant["proposed_action"] = "RETRY_SAME_SKILL"

    gate = build_final_gate(broken)

    assert gate["gate_passed"] is False
    assert "variant_expectations" in gate["report_shape"]["errors"]


def test_final_gate_rejects_unknown_variant_fields(hybrid_report: dict) -> None:
    broken = copy.deepcopy(hybrid_report)
    broken["results"][0]["variants"][0]["unexpected"] = "ignored"

    gate = build_final_gate(broken)

    assert gate["gate_passed"] is False
    assert "variant_extra_fields" in gate["report_shape"]["errors"]


def test_final_gate_rejects_unknown_root_result_and_summary_fields(
    hybrid_report: dict,
) -> None:
    broken = copy.deepcopy(hybrid_report)
    broken["unexpected"] = True
    broken["results"][0]["unexpected"] = True
    broken["summary"]["unexpected"] = True

    gate = build_final_gate(broken)

    assert gate["gate_passed"] is False
    assert "root_extra_fields" in gate["report_shape"]["errors"]
    assert "result_extra_fields" in gate["report_shape"]["errors"]
    assert "summary_extra_fields" in gate["report_shape"]["errors"]


def test_final_gate_requires_all_derived_summary_fields(hybrid_report: dict) -> None:
    broken = copy.deepcopy(hybrid_report)
    del broken["summary"]["baseline_accuracy"]

    gate = build_final_gate(broken)

    assert gate["gate_passed"] is False
    assert "baseline_accuracy" in gate["report_shape"]["errors"]


def test_final_gate_malformed_scalar_types_are_structured_failures(
    hybrid_report: dict,
) -> None:
    broken = copy.deepcopy(hybrid_report)
    broken["results"][0]["task"] = []
    broken["results"][1]["case_id"] = []

    gate = build_final_gate(broken)

    assert gate["gate_passed"] is False
    assert gate["report_shape"]["passed"] is False

    missing_results = copy.deepcopy(hybrid_report)
    missing_results["results"] = None
    missing_results_gate = build_final_gate(missing_results)
    assert missing_results_gate["gate_passed"] is False


def test_final_gate_cli_writes_configured_report_and_returns_zero_for_legal_no_go(
    tmp_path: Path,
) -> None:
    output = tmp_path / "nested" / "hybrid_final_gate.json"
    result = subprocess.run(
        [
            sys.executable,
            str(GATE_SCRIPT),
            "--input",
            str(HYBRID_REPORT),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["gate_passed"] is True
    assert payload["action_ranking_decision"] == "No-Go"


def test_final_gate_cli_returns_nonzero_for_invalid_shape(tmp_path: Path) -> None:
    source = tmp_path / "broken.json"
    output = tmp_path / "hybrid_final_gate.json"
    source.write_text(json.dumps({"schema_version": "1.0"}), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(GATE_SCRIPT),
            "--input",
            str(source),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["gate_passed"] is False
    assert payload["report_shape"]["passed"] is False


def test_run_all_runs_gate_after_hybrid_and_publishes_both_evidence_surfaces() -> None:
    source = (ROOT / "evals" / "run_all.py").read_text(encoding="utf-8")

    assert '"hybrid_final_gate"' in source
    assert "scripts/run_hybrid_final_gate.py" in source
    assert "reports/hybrid_final_gate.json" in source
    assert "reports/python_tests.json" in source
    assert "## Hybrid final gate" in source
    assert "| Hybrid final gate |" in source
