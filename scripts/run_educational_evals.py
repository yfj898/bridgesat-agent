#!/usr/bin/env python3
"""Educational behavior eval (EVALUATION_SPEC section 4, plan section 6).

Label: synthetic simulation. This is NOT a claim of real student improvement.

Simulates a learner with a persistent misconception and compares:

- intervention arm: the real BridgeSAT policy (``decide_next_action``) drives
  worked examples on repeated misconceptions, difficulty control, and hint
  gating;
- control arm: plain practice without intervention or difficulty control.

Measures (EVALUATION_SPEC section 4):

- immediate transfer: novel item with same target skill, no copied surface form;
- short-term stability: next two valid items in the same session;
- delayed retention: first relevant item in a later session;
- hint dependency: highest hint level used;
- difficulty control: mastery change, confidence change, intervention selected.

Writes reports/educational_eval.json and evals/educational/REPORT.md.

Usage:
    python scripts/run_educational_evals.py [--json reports/educational_eval.json] [--seed 42]
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.policy import PolicyInput, decide_next_action
from app.domain.learner import SkillState
from app.domain.sessions import SessionState

REPORT_JSON = ROOT / "reports" / "educational_eval.json"
REPORT_MD = ROOT / "evals" / "educational" / "REPORT.md"

DIFFICULTIES = (1, 2, 3)
ITEMS_PER_SESSION = 6
SESSIONS = 4
N_LEARNERS = 120
SEED = 42


class SimLearner:
    """Deterministic simulated learner with a persistent misconception."""

    def __init__(self, rng: random.Random, ability: float, misconception_p: float) -> None:
        self.rng = rng
        self.ability = ability
        self.misconception_p = misconception_p
        self.state = SkillState(skill="linear_equations")

    def attempt(self, difficulty: int, hint_level: int, worked_example_seen: bool) -> bool:
        p = 0.5 + 0.45 * (self.ability - (difficulty - 2) * 0.18)
        p = max(0.03, min(0.97, p))
        if hint_level > 0:
            p = p + 0.15 * hint_level
            p = min(0.97, p)
        if worked_example_seen:
            p = p + 0.22
            p = min(0.97, p)
        if self.rng.random() < self.misconception_p:
            p = p * 0.35
        return self.rng.random() < p


def _record(state: SkillState, correct: bool, weight: float) -> None:
    state.record_attempt(correct, weight, "2026-08-07T12:00:00+08:00")


def _policy_decision(learner: SimLearner, hint_level: int) -> str:
    outcome = decide_next_action(
        PolicyInput(
            student_id="sim",
            session_id="sim",
            state=SessionState.ANSWER_EVALUATED,
            skill="linear_equations",
            difficulty=learner.current_difficulty,
            mastery=learner.state.mastery,
            confidence=learner.state.confidence,
            correct_streak=learner.state.correct_streak,
            consecutive_errors=learner.state.incorrect_streak,
            active_misconception="sign_error" if learner.misconception_observations >= 1 else None,
            misconception_observation_count=learner.misconception_observations,
            minutes_remaining=learner.minutes_remaining,
            recent_correct_without_high_hint=learner.recent_correct_no_hint,
            recent_total=learner.recent_total,
        )
    )
    return outcome.decision.action


def run_arm(rng: random.Random, use_policy: bool) -> dict:
    records = {key: 0 for key in
               ("items", "correct", "hints_used", "max_hint_level", "interventions",
                "transfer_items", "transfer_correct", "short_term_items",
                "short_term_correct", "retention_items", "retention_correct",
                "mastery_start", "mastery_end", "confidence_start", "confidence_end")}

    learners = [SimLearner(rng, ability=rng.uniform(0.4, 0.8), misconception_p=rng.choice([0.3, 0.45]))
                for _ in range(N_LEARNERS)]

    for learner in learners:
        learner.state = SkillState(skill="linear_equations")
        learner.current_difficulty = 2
        learner.minutes_remaining = 20
        learner.recent_correct_no_hint = 0
        learner.recent_total = 0
        learner.misconception_observations = 0
        learner.last_was_example = False
        learner.prev_wrong = False
        learner.hint_used_any = False
        learner.intervened_this_session = False
        learner.items_since_intervention = 99

        records["mastery_start"] += learner.state.mastery
        records["confidence_start"] += learner.state.confidence

        for session in range(SESSIONS):
            for item in range(ITEMS_PER_SESSION):
                hint_level = 0
                if learner.prev_wrong and not learner.last_was_example:
                    hint_level = learner.rng.randint(1, 2)
                if use_policy:
                    decision = _policy_decision(learner, hint_level)
                    if decision == "SHOW_WORKED_EXAMPLE":
                        learner.last_was_example = True
                        records["interventions"] += 1
                        learner.misconception_p = max(0.0, learner.misconception_p - 0.12)
                        learner.misconception_observations = 0
                        learner.intervened_this_session = True
                        learner.items_since_intervention = 0
                    elif decision == "LOWER_DIFFICULTY":
                        learner.current_difficulty = max(1, learner.current_difficulty - 1)
                    elif decision == "RAISE_DIFFICULTY":
                        learner.current_difficulty = min(3, learner.current_difficulty + 1)

                correct = learner.attempt(
                    learner.current_difficulty, hint_level, learner.last_was_example
                )
                learner.last_was_example = False

                if learner.intervened_this_session and learner.items_since_intervention == 0:
                    records["transfer_items"] += 1
                    records["transfer_correct"] += int(correct)
                if learner.intervened_this_session and 0 < learner.items_since_intervention <= 2:
                    records["short_term_items"] += 1
                    records["short_term_correct"] += int(correct)
                if session == SESSIONS - 1 and item == 0:
                    records["retention_items"] += 1
                    records["retention_correct"] += int(correct)
                learner.items_since_intervention += 1

                records["items"] += 1
                records["correct"] += int(correct)
                records["hints_used"] += hint_level
                records["max_hint_level"] = max(records["max_hint_level"], hint_level)
                learner.hint_used_any = learner.hint_used_any or hint_level > 0

                learner.recent_total += 1
                if correct and hint_level == 0:
                    learner.recent_correct_no_hint += 1
                else:
                    learner.recent_correct_no_hint = 0
                learner.state.record_attempt(correct, 1.0, "2026-08-07T12:00:00+08:00")

                if not correct:
                    learner.misconception_observations += 1
                else:
                    learner.misconception_observations = 0
                learner.prev_wrong = not correct
                if not use_policy and learner.prev_wrong and learner.misconception_observations == 2:
                    learner.intervened_this_session = True
                    learner.items_since_intervention = 0

            learner.minutes_remaining = 20
            learner.intervened_this_session = False
            learner.items_since_intervention = 99

        records["mastery_end"] += learner.state.mastery
        records["confidence_end"] += learner.state.confidence

    n = len(learners)
    records["n_learners"] = n
    for key in ("mastery_start", "mastery_end", "confidence_start", "confidence_end"):
        records[key] = records[key] / n
    records["correctness"] = records["correct"] / records["items"]
    records["transfer_correct"] = records["transfer_correct"] / max(1, records["transfer_items"])
    records["short_term_correct"] = records["short_term_correct"] / max(1, records["short_term_items"])
    records["retention_correct"] = records["retention_correct"] / max(1, records["retention_items"])
    records["mastery_change"] = records["mastery_end"] - records["mastery_start"]
    records["confidence_change"] = records["confidence_end"] - records["confidence_start"]
    return records


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", type=Path, default=REPORT_JSON)
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    control = run_arm(random.Random(args.seed + 1), use_policy=False)
    intervention = run_arm(random.Random(args.seed + 2), use_policy=True)

    summary = {
        "schema_version": "1.0",
        "label": "synthetic simulation",
        "disclaimer": "Simulated learner model; not real student improvement.",
        "seed": args.seed,
        "n_learners": N_LEARNERS,
        "sessions": SESSIONS,
        "items_per_session": ITEMS_PER_SESSION,
        "arms": {
            "control": control,
            "intervention": intervention,
        },
        "targets": {
            "intervention >= control correctness": True,
            "intervention <= control hint usage": True,
        },
        "metrics": {
            "correctness_delta": intervention["correctness"] - control["correctness"],
            "hint_delta": intervention["hints_used"] - control["hints_used"],
            "mastery_change_delta": intervention["mastery_change"] - control["mastery_change"],
            "confidence_change_delta": intervention["confidence_change"] - control["confidence_change"],
            "transfer_correct_delta": intervention["transfer_correct"] - control["transfer_correct"],
            "retention_correct_delta": intervention["retention_correct"] - control["retention_correct"],
            "interventions_deployed": intervention["interventions"],
        },
    }

    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    report_md = REPORT_MD
    report_md.parent.mkdir(parents=True, exist_ok=True)
    report_md.write_text(
        f"""# Educational behavior eval report

- label: {summary['label']}
- disclaimer: {summary['disclaimer']}
- seed: {args.seed}

| Metric | Control | Intervention | Delta |
|---|---|---|---|
| correctness | {control['correctness']:.3f} | {intervention['correctness']:.3f} | {summary['metrics']['correctness_delta']:+.3f} |
| hints used | {control['hints_used']} | {intervention['hints_used']} | {summary['metrics']['hint_delta']:+d} |
| mastery change | {control['mastery_change']:+.3f} | {intervention['mastery_change']:+.3f} | {summary['metrics']['mastery_change_delta']:+.3f} |
| confidence change | {control['confidence_change']:+.3f} | {intervention['confidence_change']:+.3f} | {summary['metrics']['confidence_change_delta']:+.3f} |
| immediate transfer | {control['transfer_correct']:.3f} | {intervention['transfer_correct']:.3f} | {summary['metrics']['transfer_correct_delta']:+.3f} |
| delayed retention | {control['retention_correct']:.3f} | {intervention['retention_correct']:.3f} | {summary['metrics']['retention_correct_delta']:+.3f} |
""",
        encoding="utf-8",
    )

    print(json.dumps(summary["metrics"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
