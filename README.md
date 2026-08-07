# BridgeSAT Agent

BridgeSAT is an offline-first adaptive SAT learning agent for students with
limited tutoring access, older devices, or unstable internet connections.

Competition MVP: a **math closed loop** — diagnose → plan → teach → observe →
diagnose misconception → recall learner memory → retrieve approved content →
choose and explain the next action → record outcome — completed fully offline
on-device (no external model, Mnemis, vector service, or network required).

## What is delivered

- a FastAPI application (`app/main.py`) and a mobile-first PWA shell (`web/`);
- a deterministic adaptive engine: diagnostic, skill-gap analysis, daily plan,
  mastery (Bayesian), misconception evidence, interventions;
- a published content pack `bridgesat-math-0.1.0`: 55 human-approved original
  math items across four skills (`linear_equations`, `systems_equations`,
  `ratios_percentages`, `functions_models`), plus 8 micro-lessons and 8
  worked examples, all with reviewer ledgers, licenses, and content hashes;
- an immutable SQLite event log with sync protocol: offline queuing,
  idempotent batches, refresh recovery, version-bound scoring;
- episodic/strategy long-term memory (SQLite) with an optional Mnemis gateway
  that degrades to SQLite fallback on timeout or unavailability;
- FTS5 retrieval over the approved pack with citation/license filtering and
  restricted-source exclusion;
- governed content pipeline: selection, drafting, exact-math validation,
  review ledger, approval blocking, pack build + hash verification;
- recovery tooling: pre-migration backups, SQLite restore, learner projection
  rebuild from the event log, memory index rebuild, dead-letter replay;
- security hardening and the full evaluation suite from
  `docs/EVALUATION_SPEC.md`.

## Out of scope (honest scope statement)

- Reading/writing skills and the full eight-skill taxonomy are **future
  extension scope**; the competition demo does not imply reading support.
- Original skeleton questions are quarantined (`content/quarantined/`) and
  never student-facing; only published packs under `content/packs/` load.
- GSM8K is evaluation-only and never enters the student content path.
- College Board / Khan Academy / OpenStax are `reference_only` sources: no
  acquisition, no crawler, no RAG (see `config/sources.yaml` and
  `docs/DATA_SOURCE_REGISTRY.md`).
- No external LLM is required for question selection, mastery updates,
  progress storage, or offline practice (design boundary, enforced by tests).
- Mnemis, embeddings, LightRAG are conditional enhancements, never
  dependencies of the main loop.

## Planning and contract documents

- `docs/COMPETITION_MVP_EXECUTION_PLAN.md`: dated plan, gates, demo path;
- `docs/ARCHITECTURE.md`: system architecture and design boundaries;
- `docs/IMPLEMENTATION_PLAN.md`: technology choices and module contracts;
- `docs/PEDAGOGY_SPEC.md`: curriculum, mastery, misconception, fairness;
- `docs/MEMORY_CONSISTENCY.md`: memory store, outbox, Mnemis, deletion;
- `docs/SYNC_PROTOCOL.md`: offline events, idempotency, conflicts, snapshots;
- `docs/THREAT_MODEL.md`: privacy and security threat model;
- `docs/API_AND_OPERATIONS.md`: API, migration, backup/restore, operations;
- `docs/EVALUATION_SPEC.md`: evaluation and acceptance criteria;
- `docs/DATA_SOURCE_REGISTRY.md` + `config/sources.yaml`: source permissions;
- `docs/DATA_ACQUISITION.md`: governed acquisition and review workflow;
- `docs/EVIDENCE_PACK.md`: measured results and reproduction commands;
- `docs/WORKLOG.md`: chronological session records.

## Quick start

```bash
cd bridgesat-agent
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

python scripts/import_content_pack.py   # build FTS5 index from the published pack
python scripts/seed_demo.py             # seed the offline demo learner (idempotent)
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000` (PWA shell at `web/`), then run the tests and
evaluations:

```bash
pytest                                  # full test suite (238 tests)
python -m evals.run_all                 # regenerate every eval report
node --test web/tests/*.test.js         # offline/weak-network/accessibility core paths
```

## Data sources

- `bridgesat_original`: authored originals (all published items/lessons);
- `deepmind_mathematics_dataset`: `candidate_generation_only` — concept
  source for drafting, never verbatim content, license
  `bridgesat_original` on derived items;
- `project_gutenberg`, `library_of_congress_free_to_use`: approved with item
  review (not yet used by the math pack);
- `gsm8k`, `belebele`: evaluation-only;
- `college_board_sat`, `khan_academy`, `openstax`: `reference_only` —
  acquisition, crawlers, and RAG are blocked and audited.

## Measured results (reproducible via `python -m evals.run_all`)

All results are labeled per `docs/EVALUATION_SPEC.md` section 2. Synthetic
simulation is never presented as real student improvement.

| Target | Kind | Measured |
|---|---|---|
| policy golden trajectories >= 20, overall >= 90% | design target | 24/24 (100%), 12/12 categories |
| policy safety-critical 100% | design target | 100% |
| two-session memory, sync, security-critical 100% | design target | 100% |
| offline core-flow / duplicate-sync / restart recovery 100% | design target | 10/10 scenarios |
| RAG citation/license coverage 100%, restricted-source recall 0 | controlled internal test | 100% / 0 hits |
| content audit 100% | controlled internal test | 889/889 checks |
| local policy p95 < 150 ms | controlled internal test | 0.01 ms (this machine) |
| FTS5 p95 < 200 ms | controlled internal test | 2.3 ms (this machine) |
| session restore p95 < 500 ms | controlled internal test | 3.2 ms (this machine) |
| security + sync suites | controlled internal test | 73 passed |
| web core-flow tests | controlled internal test | 21 passed, 0 failed |
| educational improvement over control | synthetic simulation | +5.7pp correctness |

Not yet measured (requires a human usability study): real educational
outcome; accessibility manual walkthrough items marked "manual check
required" in `reports/accessibility_eval.md`.

## Status

Competition MVP implemented end to end; every pre-submission checklist item
in `docs/COMPETITION_MVP_EXECUTION_PLAN.md` section 12 is closed except the
human usability study and the demo recording itself.
