# BridgeSAT Candidate Review Report

- Generated: `2026-08-06T09:42:48.479607+00:00`
- Input records: **396**
- Unique records: **396**
- Duplicates removed: **0**
- Student-ready records: **0**
- Run fingerprint: `0a2b7ec958e8bf4a14078562ae1f5d92e3efbae217a036e5df3f9ad301ef813c`

## Review routes

| Category | Count |
|---|---:|
| `evaluation_only` | 100 |
| `hold_out_of_scope` | 7 |
| `manual_review_required` | 156 |
| `priority_manual_review` | 19 |
| `ready_for_rewrite` | 89 |
| `sensitive_context_review` | 25 |

## Source distribution

| Category | Count |
|---|---:|
| `deepmind_mathematics_dataset` | 96 |
| `gsm8k` | 100 |
| `library_of_congress_free_to_use` | 100 |
| `project_gutenberg` | 100 |

## Skill and prerequisite distribution

| Category | Count |
|---|---:|
| `arithmetic_operations` | 118 |
| `evidence_selection` | 45 |
| `functions_models` | 18 |
| `linear_equations` | 12 |
| `main_idea_inference` | 59 |
| `ratios_percentages` | 29 |
| `sentence_boundaries` | 1 |
| `systems_equations` | 12 |
| `unmapped` | 101 |
| `words_in_context` | 1 |

## License precheck decisions

| Category | Count |
|---|---:|
| `clear_for_candidate_generation` | 96 |
| `clear_for_isolated_evaluation` | 100 |
| `insufficient_item_rights_evidence` | 101 |
| `manual_rights_review_required` | 80 |
| `provisionally_clear_requires_human_confirmation` | 19 |

## Age-suitability precheck decisions

| Category | Count |
|---|---:|
| `clear_for_candidate_review` | 96 |
| `context_review_required` | 36 |
| `insufficient_content_for_age_review` | 87 |
| `no_automated_flags` | 177 |

## Quality summary

- Mean: **68.74**
- Minimum: **42**
- Maximum: **88**

| Category | Count |
|---|---:|
| `high` | 96 |
| `low` | 111 |
| `medium` | 189 |

## Limitations

- Automated license checks are pre-screening, not legal approval.
- Gutenberg candidates currently contain catalog metadata, not selected passage text.
- LOC candidates contain descriptive metadata and media links; educational and accessibility review remains manual.
- DeepMind output must be rewritten and reviewed before student use.
- GSM8K remains isolated from product RAG and offline packs.
