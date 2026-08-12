# BridgeSAT final evaluation summary

Every result below is labeled by EVALUATION_SPEC section 2:
`synthetic simulation`, `controlled internal test`, or design target.
Synthetic simulation is never presented as real student improvement.

## Policy (synthetic simulation)

- trajectories: 24 (target >= 20)
- overall pass rate: 100% (design target >= 90%)
- safety-critical pass rate: 100% (design target 100%)
- categories covered: 12/12
- report: reports/policy_eval.json

## Educational behavior (synthetic simulation)

- disclaimer: synthetic simulation, not real student improvement
- correctness delta: +0.057
- hint usage delta: -260
- immediate transfer delta: +0.225
- delayed retention delta: +0.067
- mastery change delta: +0.049
- interventions deployed: 199
- report: reports/educational_eval.json

## Retrieval (controlled internal test)

- dev set: 6 queries, recall@1 100%, MRR 100%, citation coverage 100%, license coverage 100%, restricted-source hits 0
- golden set: 8 queries, recall@1 88%, MRR 88%, citation coverage 100%, license coverage 100%
- report: reports/rag_eval.json

## Long-term memory (controlled internal test)

- probes: 10
- PostgreSQL similarity recall@3: 100%
- PostgreSQL similarity next-action accuracy: 100%
- Mnemis dual-route recall@3: 100%
- Mnemis dual-route next-action accuracy: 100%
- fallback success: PostgreSQL two-session loop and timeout fallback tested in the test suite
- report: reports/memory_eval.json

## Hybrid shadow ablation (controlled internal test)

- cases: 15 golden, 22 scripted model variants (no live provider)
- accepted allowed-action violations: 0 (target 0)
- accepted hallucinated episode/content: 0 (target 0)
- deterministic fallback success: 100% (target 100%)
- decisive cases with zero model calls: 100%
- adversarial rejection: 100%
- verified beneficial differences: ['h6-01']
- report: reports/hybrid_eval.json

## Hybrid final gate (controlled internal test)

- final competition mode: `deterministic`
- frozen feature flags: BRIDGESAT_HYBRID_ENABLED=0, BRIDGESAT_HYBRID_SHADOW_ENABLED=0, BRIDGESAT_HYBRID_EXPLANATION_ENABLED=0, BRIDGESAT_HYBRID_ACTION_RANKING_ENABLED=0, BRIDGESAT_HYBRID_SUMMARY_ENABLED=0
- H7 action-ranking final decision: **No-Go** (No-Go is a legal evidence outcome, not a gate failure)
- gate status: PASS (report shape and safety checks)
- evidence split: legacy H6/H7 10 cases/17 variants; H8 summary 5 cases/5 variants (excluded from legacy metrics)
- safety checks: allowed-action violations 0, hallucinated acceptance 0, fallback 100%, decisive zero calls 100%, explanation grounding 100%, H8 summary grounding 100%, unavailable H8 fallback 100%
- beneficial difference cases: ['h6-01'] (controlled synthetic/scripted evidence only)
- rationale: Default competition mode remains deterministic. H7 action-ranking evidence is limited to a controlled synthetic evaluation with scripted provider responses. Scripted p50/p95 values of 0 ms are not real-provider latency evidence. No repeated real-provider latency and lock-duration run was supplied.
- latency disclaimer: The hybrid report latency values are scripted-transport harness timings from a controlled internal test; a scripted p50/p95 of 0 ms is not real-provider latency evidence, a lock-duration measurement, or an SLA.
- missing evidence: repeated real-provider latency runs, real-provider lock-duration measurements under the sync path, real student outcome measurements
- rollback profile: deterministic-default; Keep deterministic policy authoritative, leave all five Hybrid flags at 0, and disable any action-changing path.
- report: reports/hybrid_final_gate.json

## Python test suite (controlled internal test)

- pytest: 850 passed, 0 failed
- report: reports/python_tests.json

## Offline and synchronization (controlled internal test)

- scenarios: 10, pass rate: 100%
- report: reports/offline_sync_eval.json

## Security (controlled internal test)

- pytest security + sync suites: 84 passed
- report: reports/security_eval.json

## Web core-flow tests (controlled internal test)

- node --test web/tests: 57 passed, 0 failed (offline flow, refresh, weak network, accessibility core paths)
- report: reports/web_tests.json

## Content audit (controlled internal test)

- checks: 1799, pass rate: 100%
- pack: /media/bili-guo/1235578e-e896-4ce5-9fdc-6318e4960f4c2/any/bridgesat-agent/content/packs/bridgesat-math-0.3.0
- limitation: reviewer IDs are simulated (`sim.*`); this is not a real human approval
- report: reports/content_audit_eval.json

## Performance gates (controlled internal test, this machine)

- local policy p95: 0.01 ms (target < 150 ms)
- PostgreSQL tsvector p95: 1.11 ms (target < 200 ms)
- session restore p95: 2.42 ms (target < 500 ms)
- sync throughput: 363.4 events/s, max RSS 80.2 MB
- report: reports/performance_eval.json

## Accessibility

- checklist: reports/accessibility_eval.md
- items needing a human usability pass are marked 'manual check required'

## Design targets vs measured results

| Target | Kind | Measured |
|---|---|---|
| policy overall >= 90% | design target | 100% |
| policy safety-critical 100% | design target | 100% |
| offline core-flow completion = 100% | design target | PASS |
| duplicate scoring incidents = 0 | design target | PASS |
| restart recovery = 100% | design target | PASS |
| known-version scoring consistency = 100% | design target | PASS |
| unacknowledged-event loss = 0 | design target | PASS |
| content audit 100% | controlled internal test | 100% |
| local policy p95 < 150 ms | controlled internal test | 0.01 ms |
| PostgreSQL tsvector p95 < 200 ms | controlled internal test | 1.11 ms |
| session restore p95 < 500 ms | controlled internal test | 2.42 ms |
| educational improvement over control | synthetic simulation | +5.7% correctness |

## Reproduction

```bash
.venv/bin/python scripts/import_content_pack.py
.venv/bin/python -m pytest
node --test web/tests/*.test.js
.venv/bin/python -m evals.run_all
.venv/bin/python scripts/seed_demo.py
```

## Recovery capabilities (API_AND_OPERATIONS sections 7-8)

| Capability | Implementation | Evidence |
|---|---|---|
| PostgreSQL schema migration | `app/infrastructure/migration_runner.py` applies versioned `migrations_pg` transactions under an advisory lock | `tests/test_pg_migration_runner.py` |
| Rebuild learner projections from events | `scripts/rebuild_learner_projections.py` replays `learning_events` through the sync apply path | `tests/test_sync_protocol.py::test_rebuild_projection_from_events_restores_state` (golden equality) |
| Rebuild PostgreSQL tsvector index from approved content | `app/knowledge/local_backend.index_pack` via `scripts/import_content_pack.py` | `tests/test_pg_retrieval.py`, `tests/test_retrieval.py` |
| Rebuild Mnemis from validated episodes/facts | `scripts/rebuild_memory_index.py` (idempotency keys, dead-letter replay) | `tests/test_scripts.py` |
| Verify content-pack checksums | `verify_pack_hashes` in `app/content_pipeline/packaging.py` | `scripts/build_content_pack.py` + `scripts/run_content_audit.py` |

