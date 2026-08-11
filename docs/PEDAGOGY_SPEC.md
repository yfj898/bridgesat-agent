# BridgeSAT Pedagogy Specification

## 1. Status and purpose

- Specification version: `pedagogy-v1.0-draft`
- Scope: AceSAT competition MVP
- Authority: this document defines the teaching behavior that code and evaluations must implement
- Boundary: this is a project-specific pilot taxonomy inspired by common digital SAT preparation needs; it is not an official College Board taxonomy and does not reproduce official test content

The purpose of this specification is to prevent the system from becoming a technically sophisticated chatbot with weak educational validity. Every diagnosis, mastery update, question selection, intervention, memory claim, and progress statement must follow the contracts below.

---

## 2. Frozen MVP curriculum scope

The MVP supports eight skill groups. The scope is intentionally narrow enough to review completely before submission.

### 2.0 Competition delivery scope (math closed-loop first)

Per `COMPETITION_MVP_EXECUTION_PLAN.md`, the competition MVP delivers **four math
skills only**: `linear_equations`, `systems_equations`, `ratios_percentages`,
`functions_models`, with 55 original items (12/12/13/18),
at least 2 micro-lessons and 2 worked examples per delivered skill. Reading and
writing skills and the remaining taxonomy entries below are **extension scope
for future releases** and are not claimed as delivered capabilities in the
competition demo. The full taxonomy is retained here as the reviewed extension
target.

The automated content gate passes, but the current reviewer IDs are simulated
(`sim.*`). Real human educational, answer, license, and accessibility review is
required before student deployment and is not claimed as complete.

### 2.1 Mathematics

| Skill ID | Display name | Subskills | Prerequisites |
|---|---|---|---|
| `linear_equations` | Linear equations | isolate variables, distribute, combine like terms, sign handling | integer operations |
| `systems_equations` | Systems of equations | substitution, elimination, interpreting intersections | linear equations |
| `ratios_percentages` | Ratios and percentages | ratios, proportions, percent change, unit rates | arithmetic operations |
| `functions_models` | Functions and models | function notation, tables, linear models, slope interpretation | linear equations, ratios |

### 2.2 Reading and writing

| Skill ID | Display name | Subskills | Prerequisites |
|---|---|---|---|
| `main_idea_inference` | Main idea and inference | central idea, supported inference, purpose | passage comprehension |
| `evidence_selection` | Evidence selection | matching claims to evidence, eliminating unsupported choices | main idea and inference |
| `words_in_context` | Words in context | contextual meaning, tone, transition meaning | passage comprehension |
| `sentence_boundaries` | Sentence boundaries | fragments, run-ons, punctuation, clause boundaries | basic grammar |

### 2.3 Explicit exclusions

The competition MVP does not claim complete SAT coverage. It excludes advanced geometry, trigonometry, full rhetorical synthesis, complete grammar coverage, score prediction, and official practice-test equivalence.

---

## 3. Prerequisite graph contract

The prerequisite graph is reviewed project content, not an automatically generated graph.

```text
integer_operations
  -> linear_equations
      -> systems_equations
      -> functions_models

arithmetic_operations
  -> ratios_percentages
      -> functions_models

passage_comprehension
  -> main_idea_inference
      -> evidence_selection
  -> words_in_context

basic_grammar
  -> sentence_boundaries
```

Rules:

1. Every skill node has a stable ID and version.
2. Every edge has an evidence note and reviewer status.
3. The Agent may expand at most two prerequisite hops in one decision.
4. A prerequisite is considered a likely blocker only after at least two pieces of supporting evidence.
5. An inferred blocker is displayed as a tentative observation, not a fixed learner trait.

---

## 4. Item specification

Every scored question must conform to the following logical schema:

```json
{
  "id": "math_linear_001",
  "version": 1,
  "domain": "math",
  "target_skill": "linear_equations",
  "target_subskill": "isolate_variables",
  "required_prerequisites": ["integer_operations"],
  "secondary_skills": [],
  "difficulty": 1,
  "prompt": "Solve 3x + 5 = 17.",
  "choices": ["2", "3", "4", "6"],
  "answer": "4",
  "misconception_map": {
    "2": "division_before_subtraction",
    "3": "arithmetic_error",
    "6": "ignored_constant"
  },
  "hints": [
    "First isolate the term containing x.",
    "Subtract 5 from both sides.",
    "Then divide both sides by 3."
  ],
  "micro_lesson_id": "lesson_linear_isolation",
  "estimated_seconds": 75,
  "source_type": "original",
  "license": "project-original",
  "review_status": "approved"
}
```

### 4.1 Item quality requirements

- one primary skill per scored item;
- secondary skills explicitly recorded;
- exactly one defensible correct answer;
- distractors linked to plausible misconceptions where possible;
- three hints that increase in specificity without immediately exposing the answer;
- a reviewed explanation;
- age-appropriate language;
- accessible mathematical notation and alternative text where needed;
- immutable historical versions after publication.

### 4.2 Difficulty levels

Only three levels are used in the MVP:

| Level | Meaning |
|---|---|
| 1 | direct application with minimal linguistic complexity |
| 2 | one additional transformation, inference, or distractor challenge |
| 3 | multi-step application or integration with a prerequisite |

Difficulty is initially assigned by review and later calibrated using aggregate attempt data. Student-specific performance does not silently change the global item difficulty.

---

## 5. Content review lifecycle

```text
draft
  -> schema_validated
  -> educational_review
  -> license_review
  -> approved
  -> published
  -> deprecated | withdrawn
```

Rules:

- `draft` and `schema_validated` content cannot be shown to students.
- `approved` content becomes immutable at that version.
- corrections create a new version.
- withdrawn content is excluded from new sessions immediately.
- sessions already using a withdrawn version may finish only when the issue is non-safety-critical; otherwise they are paused and replaced.
- every published item preserves reviewer ID, review timestamp, source record, and license record.

---

## 6. Diagnostic blueprint

### 6.1 Structure

The initial diagnostic has two stages:

1. **Coverage stage:** one item for each of the eight skill groups.
2. **Confirmation stage:** up to four additional items targeting the weakest or least certain skills.

Maximum total: 12 items.

### 6.2 Diagnostic selection rules

- begin at difficulty 1 or 2 depending on profile information;
- do not use the same subskill twice before all selected skills receive coverage;
- confirmation items must differ in wording and surface form;
- diagnostic questions do not use Level-3 hints before an answer is submitted;
- incomplete diagnostics produce low-confidence estimates rather than forced conclusions.

### 6.3 Diagnostic output

For each skill:

```text
mastery estimate
confidence
evidence count
observed misconceptions
recommended next evidence
```

The system must distinguish low mastery with sufficient evidence, uncertain mastery due to insufficient evidence, a possible prerequisite blocker, and a possible language or accessibility confound.

---

## 7. Mastery and confidence model

The MVP uses a weighted Beta evidence model because it is deterministic, explainable, and stable with small samples.

### 7.1 Stored state

For every student and skill:

```text
alpha
beta
mastery
confidence
evidence_count
last_practiced_at
review_due_at
correct_streak
incorrect_streak
```

Initial prior:

```text
alpha = 2.0
beta = 2.0
mastery = alpha / (alpha + beta) = 0.5
confidence = 0.0
```

### 7.2 Evidence weight

Base weight by difficulty:

| Difficulty | Weight |
|---|---:|
| 1 | 0.75 |
| 2 | 1.00 |
| 3 | 1.25 |

Hint multiplier:

| Highest hint used | Multiplier |
|---|---:|
| 0 | 1.00 |
| 1 | 0.80 |
| 2 | 0.55 |
| 3 | 0.30 |

Additional multipliers:

| Condition | Multiplier |
|---|---:|
| repeated same item | 0.35 |
| immediate transfer item after intervention | 1.10 |
| content or timing anomaly | 0.00 |
| answer changed after reveal | 0.00 |

Final weight:

```text
w = difficulty_weight × hint_multiplier × repeat_multiplier × validity_multiplier
```

Update:

```text
correct:   alpha = alpha + w
incorrect: beta  = beta  + w
mastery = alpha / (alpha + beta)
confidence = min(1.0, max(0.0, (alpha + beta - 4.0) / 8.0))
```

### 7.3 Time and fairness rule

Response time is not directly used to decrease mastery. It is recorded for session planning and anomaly detection only. Network delay, device delay, screen-reader use, pauses, and offline synchronization delay must never be interpreted as lower ability.

### 7.4 Staleness

Mastery does not automatically decay in the MVP. Confidence decays after inactivity:

```text
after 14 inactive days:
confidence = confidence × 0.98 per additional week
```

The review scheduler uses staleness to request new evidence rather than claiming that the learner forgot the material.

### 7.5 Promotion and support thresholds

Raise difficulty only when all are true:

- mastery `>= 0.72`;
- confidence `>= 0.55`;
- at least two of the last three valid attempts were correct without Level-2 or Level-3 hints;
- no repeated high-confidence misconception is active.

Lower difficulty or insert support when any are true:

- two consecutive valid errors on the same skill;
- repeated mapped misconception;
- mastery `< 0.45` with confidence `>= 0.40`;
- the current item requires an unmastered prerequisite.

Low confidence alone triggers more evidence, not automatic remediation.

---

## 8. Misconception classification

### 8.1 Confidence levels

| Source | Label confidence |
|---|---|
| reviewed distractor mapping | high |
| deterministic symbolic rule | high |
| repeated behavioral pattern | medium |
| LLM classification of free text | low until confirmed |

### 8.2 Allowed states

```text
observed
suspected
confirmed
resolved
archived
```

Rules:

- one mapped distractor creates an `observed` event;
- two independent observations can create `suspected`;
- three supporting observations across at least two distinct items can create `confirmed`;
- five valid non-supporting attempts can reduce confidence or mark `resolved`;
- no student-facing message may present `observed` or `suspected` as a permanent weakness.

Free-text classification may suggest a label but cannot independently create a confirmed memory fact.

---

## 9. Intervention catalog

The bounded intervention set is:

```text
level_1_hint
level_2_hint
level_3_hint
worked_example
micro_lesson
lower_difficulty
prerequisite_review
retrieval_practice
switch_skill
end_with_review
```

Each intervention declares applicable skills and misconceptions, expected duration, required content IDs, offline availability, contraindications, and outcome measurement window.

---

## 10. Intervention effectiveness

An intervention is evaluated using three windows:

| Window | Measure | Weight |
|---|---|---:|
| immediate transfer | next distinct item on the same skill | 0.50 |
| short-term stability | next two valid items in the session | 0.30 |
| delayed retention | first valid item in a later session | 0.20 |

Component score:

```text
correct without high-level hint = 1.0
correct with Level-1 hint       = 0.8
correct with Level-2 hint       = 0.5
correct with Level-3 hint       = 0.2
incorrect                       = 0.0
```

Overall effectiveness is computed only from available windows and normalized by their available weights.

An intervention becomes preferred only when at least three comparable observations exist, effectiveness exceeds alternatives by at least `0.15`, confidence is at least `0.60`, and no strong recent contradiction exists.

---

## 11. Study-plan contract

A plan contains goals, evidence needs, time limits, and review reserve.

```json
{
  "session_minutes": 20,
  "goals": [
    {
      "skill": "linear_equations",
      "reason": "low_mastery_high_confidence",
      "target_valid_attempts": 4,
      "target_mastery": 0.60
    }
  ],
  "reserved_review_minutes": 3,
  "adaptation_budget_minutes": 5,
  "policy_version": "planner-v1"
}
```

Plan rules:

- no more than two active skill goals in one short session;
- preserve at least two minutes for closure and review;
- do not introduce a new skill with fewer than four minutes remaining;
- repeated misconceptions override ordinary difficulty progression;
- current valid evidence overrides stale memory;
- every plan modification emits a reason-coded Agent event.

---

## 12. Learner-facing language

Allowed:

> Recent practice suggests that sign changes need another review.

Not allowed:

> You are bad at algebra.

Every learner-facing memory or diagnosis must describe observed behavior, include uncertainty when evidence is limited, avoid fixed-ability labels, explain the next constructive action, and allow correction or forgetting of long-term memory.

---

## 13. Fairness rules

- network latency is excluded from response-time analysis;
- device performance is excluded from ability estimates;
- screen-reader and keyboard navigation time is not penalized;
- English comprehension and mathematics are tracked separately when possible;
- demographic attributes are not used to infer mastery;
- incomplete data lowers confidence rather than lowering mastery;
- accessibility accommodations are stored as preferences, not deficits.

---

## 14. Educational evaluation acceptance criteria

The MVP must demonstrate:

1. at least 20 policy golden scenarios with `>= 90%` pass rate;
2. immediate-transfer items use different surface forms from teaching examples;
3. delayed-retention evaluation exists for the scripted demo student;
4. hint dependency is reported separately from correctness;
5. difficulty changes are controlled when comparing interventions;
6. synthetic, internal, and real-user evidence are labeled separately;
7. no score-improvement claim is made without supporting data.

---

## 15. Versioning

The following versions must be retained in events and reports:

```text
skill-taxonomy-version
item-version
content-pack-version
mastery-policy-version
misconception-policy-version
planner-version
intervention-evaluation-version
```

Historical outcomes must remain reproducible under the versions active when the events occurred.
