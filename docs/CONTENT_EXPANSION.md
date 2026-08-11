# Content Expansion Phase

## Competition-demo scope

The current governed artifact is `content/packs/bridgesat-math-0.3.0`.
It contains 103 questions and 24 adaptive lessons across eight SAT Math skills.
All new material is BridgeSAT-original first-party educational content. The
`sim.*` review ledger is a controlled simulation and is **not human approval**.

| Skill | Questions | Difficulty 1/2/3 | Misconceptions used | Micro / Worked | Transfer items |
|---|---:|---:|---:|---:|---:|
| `inequalities` | 12 | 4 / 5 / 3 | 4 | 1 / 1 | 4 |
| `quadratic_equations` | 12 | 4 / 5 / 3 | 3 | 1 / 1 | 4 |
| `exponents_radicals` | 12 | 4 / 5 / 3 | 3 | 1 / 1 | 4 |
| `coordinate_geometry` | 12 | 4 / 5 / 3 | 5 | 1 / 1 | 4 |

The new taxonomy adds `inequality_sign_flip`, `boundary_inclusion_error`,
`factoring_sign_error`, `missing_second_root`, `exponent_rule_confusion`,
`negative_exponent_error`, `radical_simplification_error`, `slope_sign_error`,
`distance_formula_error`, and `midpoint_formula_error`. Existing concepts such
as `inverse_operation_error`, `input_substitution`, and `arithmetic_error` are
reused where they describe the same learner error.

## Authoring and governance path

```text
content/candidates/math-expansion-v2.jsonl + checksum
→ deterministic draft generation
→ JSON Schema + exact-math + hash validation
→ controlled simulated review ledger (competition demo only)
→ explicitly demo-gated approved artifacts
→ versioned pack manifest
→ PostgreSQL content registry + tsvector index
→ PWA latest-pack installation
```

Each new question has a distractor-specific misconception map and
`author_metadata.transfer_group`. Choice collisions fail generation instead of
silently changing the misconception value. A path includes a `trigger` item and
a different `transfer` item; the PWA picker consumes that metadata after an
intervention so existing Learning Episode logic can validate success without a
memory-architecture change. Lessons declare `target_misconceptions`; each new
skill's worked example covers every misconception used by its questions. Both
question and lesson hashes are recorded in the pack manifest.

## Reproduction

```bash
.venv/bin/python scripts/generate_math_drafts.py
.venv/bin/python scripts/validate_content.py --write-validated
.venv/bin/python scripts/create_simulated_review_fixture.py
.venv/bin/python scripts/build_content_pack.py --allow-simulated-review
.venv/bin/python scripts/run_content_audit.py
.venv/bin/python scripts/import_content_pack.py
```

The two simulated-review commands above are only for the competition demo. For
student deployment, replace every `sim.*` row with accountable human review and
run `scripts/build_content_pack.py` without the override. The default command
blocks simulated provenance before writing approved or published artifacts.

The audit checks schema validity, independently recomputed answers, four
distinct choices, answer-label distribution, known
misconceptions, lesson references, duplicate and near-duplicate prompts,
review/license/lineage fields, manifest counts, hashes, and restricted sources.
Real human educational, answer, license, and accessibility review remains
required before student deployment.
