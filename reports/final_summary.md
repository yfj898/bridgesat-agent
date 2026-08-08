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
- similarity recall@3: 100%
- similarity next-action accuracy: 100%
- Mnemis dual-route recall@3: 100%
- Mnemis dual-route next-action accuracy: 100%
- fallback success: SQLite two-session loop and timeout fallback tested in the test suite
- report: reports/memory_eval.json

## Offline and synchronization (controlled internal test)

- scenarios: 10, pass rate: 100%
- report: reports/offline_sync_eval.json

## Security (controlled internal test)

- pytest security + sync suites: 77 passed
- report: reports/security_eval.json

## Web core-flow tests (controlled internal test)

- node --test web/tests: 21 passed, 0 failed (offline flow, refresh, weak network, accessibility core paths)
- report: reports/web_tests.json

## Content audit (controlled internal test)

- checks: 889, pass rate: 100%
- pack: /media/bili-guo/1235578e-e896-4ce5-9fdc-6318e4960f4c2/any/bridgesat-agent/content/packs/bridgesat-math-0.1.0
- report: reports/content_audit_eval.json

## Performance gates (controlled internal test, this machine)

- local policy p95: 0.0 ms (target < 150 ms)
- FTS5 p95: 1.77 ms (target < 200 ms)
- session restore p95: 3.34 ms (target < 500 ms)
- sync throughput: 662.0 events/s, max RSS 81.3 MB
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
| local policy p95 < 150 ms | controlled internal test | 0.0 ms |
| FTS5 p95 < 200 ms | controlled internal test | 1.77 ms |
| session restore p95 < 500 ms | controlled internal test | 3.34 ms |
| educational improvement over control | synthetic simulation | +5.7% correctness |

## Reproduction

```bash
python -m evals.run_all   # regenerates every report above
python scripts/seed_demo.py   # seeds the offline demo data
pytest                    # full test suite
```

## Recovery capabilities (API_AND_OPERATIONS sections 7-8)

| Capability | Implementation | Evidence |
|---|---|---|
| Pre-migration backup | `app/infrastructure/migration_runner.py` copies the DB to `data/backups/` before pending migrations | `tests/test_migrations.py` (backup created, no backup for fresh DB, no second backup on idempotent rerun) |
| Restore SQLite backup | `scripts/restore_sqlite_backup.py` (refuses same-path, missing, non-SQLite) | `tests/test_migrations.py::test_restore_backup_round_trip` |
| Rebuild learner projections from events | `scripts/rebuild_learner_projections.py` replays `learning_events` through the sync apply path | `tests/test_sync_protocol.py::test_rebuild_projection_from_events_restores_state` (golden equality) |
| Rebuild FTS5 index from approved content | `app/knowledge/local_backend.index_pack` via `scripts/import_content_pack.py` | `tests/test_retrieval.py`, `tests/test_content_loader.py` |
| Rebuild Mnemis from validated episodes/facts | `scripts/rebuild_memory_index.py` (idempotency keys, dead-letter replay) | `tests/test_scripts.py` |
| Verify content-pack checksums | `verify_pack_hashes` in `app/content_pipeline/packaging.py` | `scripts/build_content_pack.py` + `scripts/run_content_audit.py` |

