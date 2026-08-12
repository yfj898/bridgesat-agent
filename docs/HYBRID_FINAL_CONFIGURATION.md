# BridgeSAT H9 Hybrid final configuration

This is the frozen competition/demo configuration produced by H9. The default
learning path is deterministic, does not require a live Hybrid provider, and
keeps PostgreSQL and the deterministic policy authoritative.

## Frozen mode and flags

`final_mode=deterministic`

Set `BRIDGESAT_HYBRID_COMPETITION_MODE=1` in the competition/demo deployment.
The application consumes this switch at startup and rejects any nonzero Hybrid
flag; runtime task gates also remain deterministic while it is set.

| Flag | Frozen value | Meaning when enabled in an explicit opt-in run |
|---|---:|---|
| `BRIDGESAT_HYBRID_ENABLED` | `0` | master Hybrid gate |
| `BRIDGESAT_HYBRID_SHADOW_ENABLED` | `0` | verified shadow decision reasoning |
| `BRIDGESAT_HYBRID_EXPLANATION_ENABLED` | `0` | grounded optional explanation |
| `BRIDGESAT_HYBRID_ACTION_RANKING_ENABLED` | `0` | verified H7 action replacement |
| `BRIDGESAT_HYBRID_SUMMARY_ENABLED` | `0` | grounded H8 session summary |

All five flags are frozen at `0` for the competition default. H7 action ranking
and H8 summary are opt-in, verified, and fail-closed paths; neither is required
for the student/memory/offline demo.

## H7 boundary and final decision

The implemented H7 path uses a bounded two-phase boundary: it commits the
deterministic evidence and fallback before the provider call, calls the provider
after the long transaction/advisory lock is released, and revalidates the source
event, session state, and decision token before serving a verified replacement.
Stale tokens, provider failures, rejected proposals, malformed responses, and
other races keep the deterministic fallback.

The H9 action-ranking decision is **No-Go for default enablement**. The evidence
is a controlled internal test over synthetic cases with scripted provider
responses: 15 cases and 22 variants, split into legacy H6/H7 (10/17) and H8
summary (5/5). Scripted p50/p95 values of `0 ms` are not real-provider latency
or lock-duration evidence, and no repeated real-provider latency/lock-duration
run was supplied. This is a legal evidence decision, not a safety-test failure.

H8 summary verification remains bounded by validated facts, claim grounding, and
deterministic fallback. It does not create a model-based learning-outcome claim.

## Rollback profile

**Profile:** `deterministic-default`

Trigger rollback for any report-shape or safety-check failure, provider
instability, or explicit rollback request. Keep deterministic policy
authoritative, leave all five flags at `0`, and disable every action-changing
Hybrid path. No database restoration is required for this flag-based rollback.

## Evidence scope

- `reports/hybrid_final_gate.json`: final mode, flags, H7 No-Go rationale, and
  rollback profile; status `PASS` for report shape and safety checks.
- `reports/hybrid_eval.json`: 15 cases/22 variants; allowed-action violations 0;
  hallucinated acceptance 0; deterministic fallback 100%; explanation grounding
  100%; summary grounding 100%; unavailable-summary fallback 100%; beneficial
  difference `h6-01`.
- `reports/content_audit_eval.json`: 1799/1799 checks for the published pack of
  8 math skills, 103 questions, and 24 lessons.
- `reports/offline_sync_eval.json`: 10/10 scenarios.
- `reports/final_summary.md` and `docs/EVIDENCE_PACK.md`: generated cross-layer
  evidence index and limitations.
- `reports/python_tests.json`: full-suite count captured by `evals.run_all`.
- Fresh closeout baselines: 850 Python tests passed and 55 Node tests passed.

All Hybrid and pack results are controlled internal tests or synthetic cases.
The provider in the Hybrid harness is scripted; no live-provider latency or SLA
is measured. The content ledger uses `sim.*` reviewers with
`human_approved=false`. No real student outcome, human content approval, public
deployment, video, or manual accessibility walkthrough is claimed.

## Exact reproduction

From the repository root, use the same order as `README.md`:

```bash
.venv/bin/python scripts/import_content_pack.py
.venv/bin/python -m pytest
node --test web/tests/*.test.js
.venv/bin/python -m evals.run_all
.venv/bin/python scripts/seed_demo.py
```

To rerun only the final gate after `reports/hybrid_eval.json` exists:

```bash
.venv/bin/python scripts/run_hybrid_final_gate.py \
  --input reports/hybrid_eval.json \
  --output reports/hybrid_final_gate.json
```
