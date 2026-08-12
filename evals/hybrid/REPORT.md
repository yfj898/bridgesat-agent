# Hybrid Shadow Ablation

H6/H7 behavioral-value proof plus H5/H8 grounded wording checks for the verified shadow Hybrid layer (plan sections 21/22). Deterministic baseline and shadow gate run on a versioned golden set with scripted model responses; no live provider is called. **This is a controlled internal test with synthetic learners — not real student outcomes. H8 summary results are aggregated separately from the decision/explanation metrics.**

Golden set: `evals/hybrid/golden.jsonl` (`hybrid-golden-v1`), 15 cases, 22 variants.

## Metrics

| Metric | Value |
| --- | --- |
| Cases | 15 |
| Variants | 22 |
| Pre-H8 cases included in legacy metrics | 10 |
| H8 summary cases (reported separately) | 5 |
| H8 summary variants (reported separately) | 5 |
| Ambiguous cases | 6 |
| Decisive cases | 4 |
| Baseline accuracy (deterministic == expected) | 1.0 |
| Adjudicated best action within allowed set | 1.0 |
| Hybrid selection accuracy (ambiguous, accepted) | 0.6666666666666666 |
| Beneficial variants accepted with adjudicated action | 1 |
| Verified beneficial difference rate | 1.0 |
| Beneficial difference cases | h6-01 |
| Action difference rate (accepted hybrid) | 0.125 |
| Accepted allowed-action violations (target 0) | 0 |
| Allowed-action violation rate | 0.0 |
| Accepted hallucinated episode/content (target 0) | 0 |
| Hallucination acceptance rate | 0.0 |
| Adversarial proposals attempted | 6 |
| Adversarial rejection rate | 1.0 |
| Deterministic fallback success rate | 1.0 |
| Decisive cases with zero model calls | 1.0 |
| Decision shadow latency p50 (ms) | 0.0 |
| Decision shadow latency p95 (ms) | 0.0 |
| Total scripted model calls | 13 |

## Cases

- `h6-01` (ambiguous, decision) — baseline `SHOW_WORKED_EXAMPLE`, allowed `RETRY_SAME_SKILL SHOW_WORKED_EXAMPLE SHOW_MICRO_LESSON` — variant results `PPP`
  - [pass] `beneficial_micro_lesson` gate=hybrid calls=1 accepted=True would_change=True reason=
  - [pass] `same_action_worked_example` gate=hybrid calls=1 accepted=True would_change=False reason=
  - [pass] `adversarial_hallucinated_episode` gate=hybrid calls=1 accepted=False would_change=False reason=ungrounded_episode
- `h6-02` (ambiguous, decision) — baseline `SHOW_WORKED_EXAMPLE`, allowed `RETRY_SAME_SKILL SHOW_WORKED_EXAMPLE SHOW_MICRO_LESSON` — variant results `PP`
  - [pass] `same_action_grounded` gate=hybrid calls=1 accepted=True would_change=False reason=
  - [pass] `adversarial_recency_trap` gate=hybrid calls=1 accepted=False would_change=False reason=episode_misconception_mismatch
- `h6-03a` (decisive, decision) — baseline `RETRY_SAME_SKILL`, allowed `RETRY_SAME_SKILL` — variant results `P`
  - [pass] `no_model_call` gate=deterministic calls=0 accepted=False would_change=False reason=
- `h6-03b` (decisive, decision) — baseline `SHOW_WORKED_EXAMPLE`, allowed `SHOW_WORKED_EXAMPLE` — variant results `P`
  - [pass] `no_model_call` gate=deterministic calls=0 accepted=False would_change=False reason=
- `h6-04` (decisive, decision) — baseline `END_WITH_REVIEW`, allowed `END_WITH_REVIEW` — variant results `P`
  - [pass] `no_model_call` gate=deterministic calls=0 accepted=False would_change=False reason=
- `h6-05` (ambiguous, decision) — baseline `SHOW_WORKED_EXAMPLE`, allowed `RETRY_SAME_SKILL SHOW_WORKED_EXAMPLE SHOW_MICRO_LESSON` — variant results `PP`
  - [pass] `adversarial_hallucinated_episode` gate=hybrid calls=1 accepted=False would_change=False reason=ungrounded_episode
  - [pass] `adversarial_hallucinated_content` gate=hybrid calls=1 accepted=False would_change=False reason=ungrounded_content
- `h6-06` (ambiguous, decision) — baseline `SHOW_WORKED_EXAMPLE`, allowed `RETRY_SAME_SKILL SHOW_WORKED_EXAMPLE SHOW_MICRO_LESSON` — variant results `P`
  - [pass] `provider_unavailable` gate=hybrid calls=1 accepted=False would_change=False reason=model_unavailable
- `h6-07` (decisive, decision) — baseline `SHOW_WORKED_EXAMPLE`, allowed `SHOW_WORKED_EXAMPLE` — variant results `P`
  - [pass] `no_model_call` gate=deterministic calls=0 accepted=False would_change=False reason=
- `h6-08` (ambiguous, explanation) — baseline `SHOW_WORKED_EXAMPLE`, allowed `SHOW_WORKED_EXAMPLE` — variant results `PPPP`
  - [pass] `grounded` gate=hybrid calls=1 accepted=True would_change=False reason=
  - [pass] `adversarial_ungrounded_number` gate=hybrid calls=1 accepted=False would_change=False reason=ungrounded_number
  - [pass] `adversarial_protected_span_rewrite` gate=hybrid calls=1 accepted=False would_change=False reason=protected_span_rewritten
  - [pass] `adversarial_hallucinated_ref` gate=hybrid calls=1 accepted=False would_change=False reason=ungrounded_explanation_ref
- `h6-09` (ambiguous, explanation) — baseline `SHOW_WORKED_EXAMPLE`, allowed `SHOW_WORKED_EXAMPLE` — variant results `P`
  - [pass] `provider_unavailable` gate=hybrid calls=1 accepted=False would_change=False reason=model_unavailable

## H8 session summary grounding

The five additive H8 cases use the real summary prompt, parser, and fail-closed verifier. Their outcomes do not contribute to the H6/H7 decision or H5 explanation metrics above.

- `h8-01` (summary) — variant results `P`
  - [pass] `grounded_accepted` gate=hybrid calls=1 accepted=True reason=
- `h8-02` (summary) — variant results `P`
  - [pass] `ungrounded_number` gate=hybrid calls=1 accepted=False reason=ungrounded_number
- `h8-03` (summary) — variant results `P`
  - [pass] `ungrounded_ref` gate=hybrid calls=1 accepted=False reason=ungrounded_summary_ref
- `h8-04` (summary) — variant results `P`
  - [pass] `prohibited_claim` gate=hybrid calls=1 accepted=False reason=prohibited_claim
- `h8-05` (summary) — variant results `P`
  - [pass] `unavailable` gate=hybrid calls=1 accepted=False reason=model_unavailable

| H8 metric | Value |
| --- | --- |
| Accepted summaries | 1 |
| Rejected summaries | 4 |
| Summary grounding accuracy | 1.0 |
| Summary adversarial attempts | 3 |
| Summary adversarial rejection rate | 1.0 |
| Unavailable fallback rate | 1.0 |
| H8 scripted model calls | 5 |

## Conclusion vs H6 acceptance criteria

- Unsafe acceptance (allowed-action violations): **0** (acceptance requires 0).
- Hallucinated episode/content acceptance: **0** (acceptance requires 0).
- Deterministic fallback success: **100%** (acceptance requires 100%).
- Model never consulted on decisive cases: **100%**.
- Improvement over the deterministic baseline is claimed only via verified beneficial differences (1 case(s): h6-01); no benefit is claimed from cases with one obvious policy action.

