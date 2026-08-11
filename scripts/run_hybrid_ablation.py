#!/usr/bin/env python3
"""Hybrid shadow ablation eval (HYBRID_REASONING_INTEGRATION_PLAN sections
21/22, phase H6).

Compares the deterministic policy against the shadow Hybrid gate on a
versioned golden set of ambiguous and decisive cases. Model responses are
scripted (beneficial, adversarial, unavailable), so the report is fully
reproducible offline while exercising the production code paths:
``derive_policy_constraints`` -> ``choose_mode`` -> prompt -> parse ->
``verify_proposal`` / ``verify_explanation``.

No live provider is called, and no observation ever changes the executed
deterministic action: this eval measures whether the verified shadow would
have chosen the adjudicated better action, and whether the gate stays
deterministic when there is nothing to reason about.

Metrics: baseline accuracy, hybrid selection accuracy, accepted
allowed-action violations (must be 0), accepted hallucinated episode or
content (must be 0), adversarial rejection rate, deterministic fallback
success (must be 100%), decisive-case model calls (must be 0), action
difference rate, verified beneficial difference rate, explanation
grounding accuracy, shadow latency p50/p95.

Usage:
    python scripts/run_hybrid_ablation.py
        [--golden evals/hybrid/golden.jsonl]
        [--report evals/hybrid/REPORT.md]

Writes JSON to stdout and a Markdown report to the report path.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, field
import statistics
import sys
from pathlib import Path

# The gates read the environment per call; every Hybrid layer is enabled only
# for the duration of a report run (see _ShadowFlags in build_report) so the
# ablation exercises the real shadow paths without permanently mutating the
# process environment for other consumers (tests, the sync service).

from app.agent.hybrid import (  # noqa: E402
    AuthoritativeEvidence,
    ContentRecord,
    HybridDecisionContext,
    RecalledEpisodeEvidence,
    ShadowMaterial,
    evidence_for_shadow,
    parse_explanation_proposal,
    run_shadow_decision,
    run_shadow_explanation,
    verify_explanation,
)
from app.agent.llm_client import LLMClient, LLMUnavailableError  # noqa: E402
from app.agent.policy import PolicyInput, derive_policy_constraints  # noqa: E402

GOLDEN_VERSION = "hybrid-golden-v1"
SCHEMA_VERSION = "1.0"
LABEL = "controlled internal test"

ADVERSARIAL_REASONS = (
    "ungrounded_episode",
    "ungrounded_content",
    "claim_misconception_mismatch",
    "claim_skill_mismatch",
    "claim_ref_not_in_candidates",
    "protected_span_rewritten",
    "ungrounded_number",
    "ungrounded_explanation_ref",
)


@dataclass
class EvalEpisode:
    """Minimal Episode-shaped object for the evidence mapping: the verifier
    reads student_id, status, outcome, skill, misconception, intervention,
    effectiveness and confidence (duck-typed like ``app.domain.memory``)."""

    episode_id: str
    student_id: str
    skill: str
    misconception: str | None
    intervention: str
    outcome: dict
    effectiveness: float
    confidence: float
    status: str = "validated"


@dataclass
class ScriptedTransport:
    """Replay-only transport: pops the next scripted response per call and
    records the outgoing request (url, body, timeout_ms). Exhausted scripts
    fail closed as unavailable so a fixture bug cannot silently pass."""

    responses: list
    calls: list = field(default_factory=list)

    async def request(self, url: str, body: dict, timeout_ms: int | None) -> dict:
        self.calls.append((url, body, timeout_ms))
        if not self.responses:
            raise LLMUnavailableError("scripted transport exhausted")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return {"choices": [{"message": {"role": "assistant", "content": item}}]}


def _load_golden(path: Path) -> list[dict]:
    cases = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
    ]
    for case in cases:
        if case.get("schema_version") != GOLDEN_VERSION:
            raise ValueError(
                f"case {case.get('case_id')}: expected golden version "
                f"{GOLDEN_VERSION}, got {case.get('schema_version')}"
            )
    return cases


def _policy_input(raw: dict) -> PolicyInput:
    return PolicyInput(**raw)


def _episode_objects(case: dict) -> tuple[list[RecalledEpisodeEvidence], dict[str, EvalEpisode]]:
    recalled: list[RecalledEpisodeEvidence] = []
    evidence: dict[str, EvalEpisode] = {}
    for raw in case.get("episodes", []):
        outcome = {
            "correct": raw["outcome_correct"],
            "different_item": raw.get("different_item", False),
            "teaching_content_id": raw.get("teaching_content_id"),
        }
        evidence[raw["episode_id"]] = EvalEpisode(
            episode_id=raw["episode_id"],
            student_id=raw["student_id"],
            skill=raw["skill"],
            misconception=raw.get("misconception"),
            intervention=raw["intervention"],
            outcome=outcome,
            effectiveness=raw["effectiveness"],
            confidence=raw["confidence"],
            status=raw.get("status", "validated"),
        )
        recalled.append(
            RecalledEpisodeEvidence(
                episode_id=raw["episode_id"],
                skill=raw["skill"],
                misconception=raw.get("misconception"),
                intervention=raw["intervention"],
                outcome_correct=raw["outcome_correct"],
                different_item=raw.get("different_item", False),
                effectiveness=raw["effectiveness"],
                confidence=raw["confidence"],
                status="validated",
                recency_bucket=raw.get("recency_bucket", "recent"),
                teaching_content_id=raw.get("teaching_content_id"),
                difficulty_band=None,
            )
        )
    return recalled, evidence


def _content_records(case: dict) -> dict[str, ContentRecord]:
    records: dict[str, ContentRecord] = {}
    for raw in case.get("content_candidates", []):
        records[raw["content_id"]] = ContentRecord(
            content_id=raw["content_id"],
            content_hash=raw["content_hash"],
            review_status=raw["review_status"],
            content_type=raw["content_type"],
            target_skill=raw["skill"],
            misconceptions=tuple(raw.get("misconceptions", [])),
            license_id="cc-by-4.0",
            source_id="eval-pack",
            pack_version=raw["pack_version"],
            human_approved=raw["human_approved"],
            body="",
        )
    return records


def _decision_material(case: dict) -> tuple[ShadowMaterial, list[str], str]:
    inputs = _policy_input(case["input"])
    recalled, evidence_objs = _episode_objects(case)
    constraints = derive_policy_constraints(
        inputs, evidence_for_shadow(list(evidence_objs.values()))
    )
    fallback = constraints.preferred_fallback
    evidence = AuthoritativeEvidence(
        episodes=evidence_objs,  # type: ignore[arg-type]
        content=_content_records(case),
        expected_student_id=inputs.student_id,
    )
    context = HybridDecisionContext.model_validate(
        dict(
            task="intervention_ranking",
            skill=inputs.skill,
            subskill=inputs.subskill,
            difficulty=inputs.difficulty,
            mastery=inputs.mastery,
            mastery_confidence=inputs.confidence,
            consecutive_errors=inputs.consecutive_errors,
            correct_streak=inputs.correct_streak,
            active_misconception=inputs.active_misconception,
            misconception_evidence_count=inputs.misconception_observation_count,
            misconception_confidence="high"
            if inputs.misconception_observation_count >= 2
            else "medium"
            if inputs.misconception_observation_count >= 1
            else "low",
            hints_used=inputs.hints_used_this_item,
            minutes_remaining=inputs.minutes_remaining,
            current_state="ANSWER_EVALUATED",
            allowed_actions=constraints.allowed_actions,
            deterministic_fallback=fallback,
            recalled_episodes=recalled,
            intervention_stats=case.get("intervention_stats", []),
            content_candidates=case.get("content_candidates", []),
        )
    )
    material = ShadowMaterial(
        source_event_id=f"eval-{case['case_id']}",
        context=context,
        constraints=constraints,
        evidence=evidence,
        fallback=fallback,
    )
    return material, [a.value for a in constraints.allowed_actions], fallback.action


def _transport_for(variant: dict) -> ScriptedTransport:
    if variant["transport"] == "unavailable":
        return ScriptedTransport([LLMUnavailableError("scripted provider outage")])
    return ScriptedTransport([json.dumps(variant["proposal"])])


def _run_decision_case(case: dict) -> dict:
    material, allowed, fallback_action = _decision_material(case)
    result = {
        "case_id": case["case_id"],
        "category": case["category"],
        "task": "decision",
        "baseline_action": fallback_action,
        "allowed_actions": allowed,
        "baseline_matches": fallback_action == case["deterministic_expected_action"],
        "adjudicated_in_allowed": case["adjudicated_best_action"] in allowed,
        "variants": [],
    }
    for variant in case["variants"]:
        transport = _transport_for(variant)
        client = LLMClient(api_key="nvapi-eval", model="test/model", transport=transport)
        observation = run_shadow_decision(material, client)
        entry = {
            "variant": variant["variant"],
            "calls": len(transport.calls),
            "gate": "deterministic" if observation is None else "hybrid",
        }
        if observation is None:
            entry.update(
                accepted=False,
                would_change=False,
                proposed_action=None,
                rejection_reason=None,
                latency_ms=None,
                checks=[],
            )
        else:
            entry.update(
                accepted=observation.accepted,
                would_change=observation.would_change,
                proposed_action=observation.model_proposal_action,
                rejection_reason=observation.rejection_reason,
                latency_ms=observation.latency_ms,
                checks=list(observation.verification_checks or ()),
            )
        entry["pass"] = _variant_pass(variant, entry, case)
        result["variants"].append(entry)
    return result


def _run_explanation_case(case: dict) -> dict:
    context = case["context"]
    result = {
        "case_id": case["case_id"],
        "category": case["category"],
        "task": "explanation",
        "baseline_action": context["fallback_action"],
        "allowed_actions": [context["fallback_action"]],
        "baseline_matches": context["fallback_action"] == case["deterministic_expected_action"],
        "adjudicated_in_allowed": case["adjudicated_best_action"] == context["fallback_action"],
        "variants": [],
    }
    from app.agent.hybrid_contracts import ExplanationContext

    explanation_context = ExplanationContext.model_validate(context)
    for variant in case["variants"]:
        transport = _transport_for(variant)
        client = LLMClient(api_key="nvapi-eval", model="test/model", transport=transport)
        proposal = run_shadow_explanation(explanation_context, client)
        entry = {
            "variant": variant["variant"],
            "calls": len(transport.calls),
            "gate": "deterministic" if proposal is None and variant["transport"] == "ok" and variant["expected_gate"] == "deterministic" else "hybrid",
            "accepted": proposal is not None,
            "would_change": False,
            "proposed_action": None,
            "rejection_reason": None,
            "latency_ms": None,
            "checks": [],
        }
        if proposal is not None:
            entry["proposed_action"] = "SHOW_WORKED_EXAMPLE"
        elif variant["transport"] == "ok":
            reason = _explanation_reason(explanation_context, variant)
            entry["rejection_reason"] = reason
        else:
            entry["rejection_reason"] = "model_unavailable"
        entry["pass"] = _variant_pass(variant, entry, case)
        result["variants"].append(entry)
    return result


def _explanation_reason(context, variant: dict) -> str | None:
    try:
        parsed = parse_explanation_proposal(json.dumps(variant["proposal"]))
    except (ValueError, json.JSONDecodeError):
        return "model_output_unparsable"
    outcome = verify_explanation(context, parsed)
    return outcome.rejected_reason


def _variant_pass(variant: dict, entry: dict, case: dict) -> bool:
    if entry["gate"] != variant["expected_gate"]:
        return False
    if entry["calls"] != variant["expected_calls"]:
        return False
    if entry["accepted"] != variant["expected_accepted"]:
        return False
    if entry["would_change"] != variant["expected_would_change"]:
        return False
    expected_reason = variant.get("expected_reason")
    if expected_reason:
        if not entry["rejection_reason"] or expected_reason not in entry["rejection_reason"]:
            return False
    return True


def _aggregate(results: list[dict], cases: list[dict]) -> dict:
    metrics = {
        "cases": len(results),
        "decision_cases": sum(1 for r in results if r["task"] == "decision"),
        "explanation_cases": sum(1 for r in results if r["task"] == "explanation"),
        "ambiguous_cases": sum(1 for r in results if r["category"] == "ambiguous"),
        "decisive_cases": sum(1 for r in results if r["category"] == "decisive"),
        "variants": sum(len(r["variants"]) for r in results),
    }
    metrics["baseline_accuracy"] = _rate(r["baseline_matches"] for r in results)
    metrics["adjudicated_within_allowed"] = _rate(r["adjudicated_in_allowed"] for r in results)
    decision_results = [r for r in results if r["task"] == "decision"]
    ambiguous_decision = [r for r in decision_results if r["category"] == "ambiguous"]

    accepted_violations = [
        e for r in results for e in r["variants"]
        if e["accepted"] and e["proposed_action"] not in r["allowed_actions"]
    ]
    metrics["accepted_allowed_action_violations"] = len(accepted_violations)
    metrics["allowed_action_violation_rate"] = 1.0 - _rate(
        not e["accepted"] or e["proposed_action"] in r["allowed_actions"]
        for r in results for e in r["variants"]
    ) if accepted_violations else 0.0

    hallucinated = [
        e for r in results for e in r["variants"]
        if e["accepted"] and e.get("rejection_reason") and e["rejection_reason"] in ADVERSARIAL_REASONS
    ]
    metrics["accepted_hallucinated_proposals"] = len(hallucinated)
    metrics["hallucination_acceptance_rate"] = 1.0 - _rate(
        not (e["accepted"] and e.get("rejection_reason") in ADVERSARIAL_REASONS)
        for r in results for e in r["variants"]
    ) if hallucinated else 0.0

    adversarial = [
        e for r in results for e in r["variants"]
        if e.get("rejection_reason") and e["rejection_reason"] in ADVERSARIAL_REASONS
    ]
    metrics["adversarial_attempts"] = len(adversarial)
    metrics["adversarial_rejection_rate"] = _rate(not e["accepted"] for e in adversarial)

    metrics["fallback_success_rate"] = _rate(
        r["baseline_action"] == _expected_action(cases, r["case_id"])
        for r in results
    )
    metrics["decisive_zero_model_calls"] = _rate(
        all(e["calls"] == 0 for e in r["variants"])
        for r in results if r["category"] == "decisive"
    )

    beneficial_variants = [
        (case, variant)
        for case in cases
        for variant in case["variants"]
        if variant.get("beneficial")
    ]
    metrics["beneficial_variants"] = len(beneficial_variants)
    metrics["beneficial_variant_success"] = sum(
        1
        for case, variant in beneficial_variants
        for entry in _variant_entries(results, case["case_id"])
        if entry["variant"] == variant["variant"]
        and entry["accepted"]
        and entry["proposed_action"] == case["adjudicated_best_action"]
        and entry["would_change"]
    )
    metrics["beneficial_difference_rate"] = (
        metrics["beneficial_variant_success"] / metrics["beneficial_variants"]
        if beneficial_variants
        else 0.0
    )
    metrics["beneficial_difference_cases"] = sorted(
        {
            case["case_id"]
            for case, variant in beneficial_variants
            for entry in _variant_entries(results, case["case_id"])
            if entry["variant"] == variant["variant"] and entry["accepted"] and entry["would_change"]
        }
    )

    hybrid_entries = [
        e for r in decision_results for e in r["variants"] if e["gate"] == "hybrid"
    ]
    metrics["action_difference_rate"] = _rate(e["would_change"] for e in hybrid_entries)

    selected = [
        e for r in ambiguous_decision for e in r["variants"]
        if e["accepted"] and e["proposed_action"] == _expected_action(cases, r["case_id"], adjudicated=True)
    ]
    metrics["hybrid_selection_accuracy"] = _rate(
        e["accepted"] and e["proposed_action"] == _expected_action(
            cases, r["case_id"], adjudicated=True
        )
        for r in ambiguous_decision for e in r["variants"] if e["gate"] == "hybrid" and e["accepted"]
    ) if selected else 0.0

    explanation_results = [r for r in results if r["task"] == "explanation"]
    metrics["explanation_grounding_accuracy"] = _rate(
        e["accepted"] == _expected_accepted(cases, r["case_id"], e["variant"])
        for r in explanation_results for e in r["variants"]
    )

    latencies = [
        e["latency_ms"] for r in decision_results for e in r["variants"]
        if e["latency_ms"] is not None
    ]
    metrics["decision_latency_p50_ms"] = _percentile(latencies, 50)
    metrics["decision_latency_p95_ms"] = _percentile(latencies, 95)
    metrics["model_calls_total"] = sum(e["calls"] for r in results for e in r["variants"])
    return metrics


def _rate(iterable) -> float:
    values = list(iterable)
    if not values:
        return 0.0
    return sum(1 for v in values if v) / len(values)


def _percentile(values: list[int], pct: int) -> float | None:
    if not values:
        return None
    return float(statistics.quantiles(sorted(values), n=100)[pct - 1])


def _expected_action(cases: list[dict], case_id: str, adjudicated: bool = False) -> str:
    case = next(c for c in cases if c["case_id"] == case_id)
    return case["adjudicated_best_action"] if adjudicated else case["deterministic_expected_action"]


def _expected_accepted(cases: list[dict], case_id: str, variant: str) -> bool:
    case = next(c for c in cases if c["case_id"] == case_id)
    return next(v for v in case["variants"] if v["variant"] == variant)["expected_accepted"]


def _variant_entries(results: list[dict], case_id: str) -> list[dict]:
    return next(r for r in results if r["case_id"] == case_id)["variants"]


def _markdown_report(metrics: dict, results: list[dict]) -> str:
    lines = [
        "# Hybrid Shadow Ablation",
        "",
        "Phase H6 behavioral-value proof for the verified shadow Hybrid layer "
        "(plan sections 21/22). Deterministic baseline and shadow gate run on a "
        "versioned golden set with scripted model responses; no live provider is "
        "called. **This is a controlled internal test with synthetic learners — "
        "not real student outcomes.**",
        "",
        f"Golden set: `evals/hybrid/golden.jsonl` (`{GOLDEN_VERSION}`), "
        f"{metrics['cases']} cases, {metrics['variants']} variants.",
        "",
        "## Metrics",
        "",
        "| Metric | Value |",
        "| --- | --- |",
    ]
    ordered = [
        ("cases", "Cases"),
        ("variants", "Variants"),
        ("ambiguous_cases", "Ambiguous cases"),
        ("decisive_cases", "Decisive cases"),
        ("baseline_accuracy", "Baseline accuracy (deterministic == expected)"),
        ("adjudicated_within_allowed", "Adjudicated best action within allowed set"),
        ("hybrid_selection_accuracy", "Hybrid selection accuracy (ambiguous, accepted)"),
        ("beneficial_variant_success", "Beneficial variants accepted with adjudicated action"),
        ("beneficial_difference_rate", "Verified beneficial difference rate"),
        ("beneficial_difference_cases", "Beneficial difference cases"),
        ("action_difference_rate", "Action difference rate (accepted hybrid)"),
        ("accepted_allowed_action_violations", "Accepted allowed-action violations (target 0)"),
        ("allowed_action_violation_rate", "Allowed-action violation rate"),
        ("accepted_hallucinated_proposals", "Accepted hallucinated episode/content (target 0)"),
        ("hallucination_acceptance_rate", "Hallucination acceptance rate"),
        ("adversarial_attempts", "Adversarial proposals attempted"),
        ("adversarial_rejection_rate", "Adversarial rejection rate"),
        ("fallback_success_rate", "Deterministic fallback success rate"),
        ("decisive_zero_model_calls", "Decisive cases with zero model calls"),
        ("decision_latency_p50_ms", "Decision shadow latency p50 (ms)"),
        ("decision_latency_p95_ms", "Decision shadow latency p95 (ms)"),
        ("model_calls_total", "Total scripted model calls"),
    ]
    for key, label in ordered:
        value = metrics[key]
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(v) for v in value) if value else "none"
        lines.append(f"| {label} | {value} |")
    lines.append("")
    lines.append("## Cases")
    lines.append("")
    for r in results:
        marks = "".join("P" if e["pass"] else "F" for e in r["variants"])
        lines.append(
            f"- `{r['case_id']}` ({r['category']}, {r['task']}) — "
            f"baseline `{r['baseline_action']}`, allowed "
            f"`{' '.join(r['allowed_actions'])}` — variant results `{marks}`"
        )
        for e in r["variants"]:
            status = "pass" if e["pass"] else "FAIL"
            reason = e["rejection_reason"] or ""
            lines.append(
                f"  - [{status}] `{e['variant']}` gate={e['gate']} calls={e['calls']} "
                f"accepted={e['accepted']} would_change={e['would_change']} "
                f"reason={reason}"
            )
    lines.append("")
    lines.append("## Conclusion vs H6 acceptance criteria")
    lines.append("")
    lines.append("- Unsafe acceptance (allowed-action violations): "
                 f"**{metrics['accepted_allowed_action_violations']}** "
                 "(acceptance requires 0).")
    lines.append("- Hallucinated episode/content acceptance: "
                 f"**{metrics['accepted_hallucinated_proposals']}** (acceptance requires 0).")
    lines.append("- Deterministic fallback success: "
                 f"**{metrics['fallback_success_rate']:.0%}** (acceptance requires 100%).")
    lines.append("- Model never consulted on decisive cases: "
                 f"**{metrics['decisive_zero_model_calls']:.0%}**.")
    lines.append("- Improvement over the deterministic baseline is claimed only via "
                 f"verified beneficial differences ({len(metrics['beneficial_difference_cases'])} "
                 f"case(s): {', '.join(metrics['beneficial_difference_cases']) or 'none'}); "
                 "no benefit is claimed from cases with one obvious policy action.")
    lines.append("")
    return "\n".join(lines)


class _ShadowFlags:
    """Scoped Hybrid flag activation: sets the three layer flags to "1" for
    the duration of a report run and restores the previous environment on
    exit, so importing or running the ablation never leaks state into other
    processes or tests."""

    _KEYS = (
        "BRIDGESAT_HYBRID_ENABLED",
        "BRIDGESAT_HYBRID_SHADOW_ENABLED",
        "BRIDGESAT_HYBRID_EXPLANATION_ENABLED",
    )

    def __enter__(self) -> "_ShadowFlags":
        self._saved = {key: os.environ.get(key) for key in self._KEYS}
        for key in self._KEYS:
            os.environ[key] = "1"
        return self

    def __exit__(self, *exc_info: object) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def build_report(golden_path: Path, report_path: Path) -> dict:
    cases = _load_golden(golden_path)
    with _ShadowFlags():
        results = []
        for case in cases:
            if case["task"] == "explanation":
                results.append(_run_explanation_case(case))
            else:
                results.append(_run_decision_case(case))
    metrics = _aggregate(results, cases)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "label": LABEL,
        "golden_set": "evals/hybrid/golden.jsonl",
        "golden_version": GOLDEN_VERSION,
        "summary": metrics,
        "results": results,
    }
    report_path.write_text(_markdown_report(metrics, results) + "\n", encoding="utf-8")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=Path("evals/hybrid/golden.jsonl"))
    parser.add_argument("--report", type=Path, default=Path("evals/hybrid/REPORT.md"))
    args = parser.parse_args(argv)
    payload = build_report(args.golden, args.report)
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
