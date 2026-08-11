"""H6 hybrid shadow ablation harness tests.

The harness compares the deterministic policy against the verified shadow
gate on the versioned golden set (phase H6 of
HYBRID_REASONING_INTEGRATION_PLAN sections 21/22). These tests exercise the
harness itself: fixture validity against the real policy, decisive-case
invariance, fail-closed rejection of hallucinated proposals and provider
outages, benefit measurement, and report payload shape.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.run_hybrid_ablation import (  # noqa: E402
    GOLDEN_VERSION,
    _decision_material,
    _load_golden,
    build_report,
)

GOLDEN = ROOT / "evals" / "hybrid" / "golden.jsonl"


@pytest.fixture(scope="module")
def report(tmp_path_factory) -> dict:
    report_dir = tmp_path_factory.mktemp("hybrid_report")
    return build_report(GOLDEN, report_dir / "REPORT.md")


@pytest.fixture(scope="module")
def cases() -> list[dict]:
    return _load_golden(GOLDEN)


def test_golden_has_all_required_shape(cases: list[dict]) -> None:
    assert len(cases) == 10
    for case in cases:
        assert case["schema_version"] == GOLDEN_VERSION
        for key in ("case_id", "category", "task", "deterministic_expected_action",
                    "adjudicated_best_action", "variants"):
            assert key in case, case["case_id"]
        assert case["category"] in ("ambiguous", "decisive")
        assert case["task"] in ("decision", "explanation")
        for variant in case["variants"]:
            for key in ("variant", "expected_gate", "expected_calls",
                        "expected_accepted", "expected_would_change"):
                assert key in variant, f"{case['case_id']}:{variant.get('variant')}"
            assert variant["expected_gate"] in ("hybrid", "deterministic")


def test_fixture_baselines_match_real_policy(cases: list[dict]) -> None:
    for case in cases:
        if case["task"] != "decision":
            continue
        _, allowed, fallback = _decision_material(case)
        assert fallback == case["deterministic_expected_action"], case["case_id"]
        assert case["adjudicated_best_action"] in allowed, case["case_id"]
        if case["category"] == "decisive":
            assert len(allowed) == 1, case["case_id"]


def test_decisive_cases_never_call_model(report: dict) -> None:
    decisive = [r for r in report["results"] if r["category"] == "decisive"]
    assert len(decisive) == 4
    for r in decisive:
        assert len(r["allowed_actions"]) == 1
        for entry in r["variants"]:
            assert entry["gate"] == "deterministic"
            assert entry["calls"] == 0
    assert report["summary"]["decisive_zero_model_calls"] == 1.0


def test_hallucinated_proposals_rejected(report: dict) -> None:
    case = next(r for r in report["results"] if r["case_id"] == "h6-05")
    variants = {e["variant"]: e for e in case["variants"]}
    assert not variants["adversarial_hallucinated_episode"]["accepted"]
    assert variants["adversarial_hallucinated_episode"]["rejection_reason"] == "ungrounded_episode"
    assert not variants["adversarial_hallucinated_content"]["accepted"]
    assert variants["adversarial_hallucinated_content"]["rejection_reason"] == "ungrounded_content"
    assert report["summary"]["accepted_hallucinated_proposals"] == 0
    assert report["summary"]["hallucination_acceptance_rate"] == 0.0
    assert report["summary"]["adversarial_rejection_rate"] == 1.0


def test_provider_outage_degrades_to_fallback(report: dict) -> None:
    case = next(r for r in report["results"] if r["case_id"] == "h6-06")
    entry = case["variants"][0]
    assert entry["gate"] == "hybrid"
    assert entry["calls"] == 1
    assert not entry["accepted"]
    assert entry["rejection_reason"] == "model_unavailable"
    assert case["baseline_action"] == "SHOW_WORKED_EXAMPLE"
    assert report["summary"]["fallback_success_rate"] == 1.0


def test_beneficial_difference_detected(report: dict) -> None:
    case = next(r for r in report["results"] if r["case_id"] == "h6-01")
    beneficial = next(e for e in case["variants"] if e["variant"] == "beneficial_micro_lesson")
    assert beneficial["accepted"]
    assert beneficial["would_change"]
    assert beneficial["proposed_action"] == "SHOW_MICRO_LESSON"
    assert report["summary"]["beneficial_variant_success"] == 1
    assert report["summary"]["beneficial_difference_rate"] == 1.0
    assert report["summary"]["beneficial_difference_cases"] == ["h6-01"]
    assert report["summary"]["action_difference_rate"] > 0.0


def test_explanation_grounding(report: dict) -> None:
    case = next(r for r in report["results"] if r["case_id"] == "h6-08")
    variants = {e["variant"]: e for e in case["variants"]}
    assert variants["grounded"]["accepted"]
    assert not variants["adversarial_ungrounded_number"]["accepted"]
    assert variants["adversarial_ungrounded_number"]["rejection_reason"] == "ungrounded_number"
    assert not variants["adversarial_protected_span_rewrite"]["accepted"]
    assert variants["adversarial_protected_span_rewrite"]["rejection_reason"] == "protected_span_rewritten"
    assert not variants["adversarial_hallucinated_ref"]["accepted"]
    assert variants["adversarial_hallucinated_ref"]["rejection_reason"] == "ungrounded_explanation_ref"
    assert report["summary"]["explanation_grounding_accuracy"] == 1.0


def test_report_acceptance_metrics(report: dict) -> None:
    summary = report["summary"]
    assert report["schema_version"] == "1.0"
    assert report["label"] == "controlled internal test"
    assert report["golden_version"] == GOLDEN_VERSION
    assert summary["baseline_accuracy"] == 1.0
    assert summary["allowed_action_violation_rate"] == 0.0
    assert summary["hallucination_acceptance_rate"] == 0.0
    assert summary["fallback_success_rate"] == 1.0
    assert summary["decisive_zero_model_calls"] == 1.0
    assert summary["adversarial_attempts"] > 0


def test_version_mismatch_rejected(tmp_path: Path) -> None:
    broken = tmp_path / "broken.jsonl"
    case = json.loads(GOLDEN.read_text(encoding="utf-8").splitlines()[0])
    case["schema_version"] = "hybrid-golden-v0"
    broken.write_text(json.dumps(case) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="expected golden version"):
        _load_golden(broken)


def test_report_markdown_written_with_conclusion(report: dict, tmp_path: Path) -> None:
    report_path = tmp_path / "REPORT.md"
    build_report(GOLDEN, report_path)
    text = report_path.read_text(encoding="utf-8")
    assert "Hybrid Shadow Ablation" in text
    assert "controlled internal test with synthetic learners" in text
    assert "zero protected-span mutations" in text or "permanent traits" in text or "Conclusion vs H6 acceptance criteria" in text


def test_run_all_has_hybrid_wired() -> None:
    source = (ROOT / "evals" / "run_all.py").read_text(encoding="utf-8")
    assert '("hybrid", run_hybrid)' in source
    assert 'reports/hybrid_eval.json' in source


def test_subprocess_smoke(tmp_path: Path) -> None:
    out = tmp_path / "REPORT.md"
    result = subprocess.run(
        [sys.executable, "scripts/run_hybrid_ablation.py", "--report", str(out)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr[-500:]
    payload = json.loads(result.stdout[result.stdout.find("{") : result.stdout.rfind("}") + 1])
    assert payload["summary"]["cases"] == 10
    assert payload["summary"]["fallback_success_rate"] == 1.0
    assert out.exists()
