# BridgeSAT

Every student deserves a tutor that remembers what actually helps them learn.

## Demo video

Final 2:44 submission demo (English hard subtitles + US-English neural narration):

https://youtu.be/BE-CNibViYk

## The student continuity problem

Students who do not have a personal tutor, a current device, or a stable internet
connection can lose momentum every time a study session ends or a connection
drops. Many practice tools record a wrong answer but do not remember the teaching
strategy that helped the student recover. BridgeSAT keeps that useful context so
you can receive it sooner when a similar stuck point returns.

## Student promise

BridgeSAT helps you:

- start with a useful next step without needing to diagnose your own mistake;
- continue cached SAT Math practice and keep your progress when your connection
  is unreliable; and
- return to a teaching strategy that previously helped you when a similar problem
  appears in a later session.

## Learning memory

```text
error -> evidence of the same misconception -> intervention -> exact approved content confirmation -> success on a different problem -> validated learning record -> earlier reuse in a later session
```

BridgeSAT only records a learning memory after it confirms the exact approved
content was presented and the student succeeds on a different problem. This makes
the later intervention a response to observed learning evidence, not a guess or
chat-history recall.

## What is delivered

- FastAPI modular monolith and a mobile-first, browser-only PWA;
- PostgreSQL as the authoritative learner state, event store, episodic memory,
  synchronization store, content registry, and `tsvector` retrieval backend;
- 16 ordered PostgreSQL migrations under
  `app/infrastructure/migrations_pg/`;
- deterministic diagnostic, weighted-Beta mastery, misconception evidence,
  bounded teaching actions, and cross-session intervention memory;
- IndexedDB active-session recovery, local scoring and bounded policy, pending
  events, device sequence numbers, reconnect sync, and duplicate protection;
- a governed math pack with 103 original items, 12 micro-lessons, and 12 worked
  examples across 8 skills, with hashes, lineage, misconception targets,
  transfer paths, license metadata, and review ledgers;
- license/review/skill filters, PostgreSQL full-text retrieval, prerequisite
  expansion, deterministic reranking, and citation validation;
- optional Mnemis and feature-flagged Hybrid adapters. Neither can change
  answers, mastery, the state machine, or authoritative records without the
  verified gates; failure falls back to PostgreSQL and deterministic policy.

## Honest scope

- The competition scope is math; reading/writing is future work.
- Core learning does not require an external LLM, Mnemis, embeddings, or a live
  network after initial profile/content-pack setup.
- The H9 competition configuration is `final_mode=deterministic`. H7 action
  ranking and H8 session summary are opt-in, verified, and fail-closed; all five
  Hybrid flags are frozen at `0`. H7 action ranking is **No-Go** for default
  enablement.
- GSM8K is evaluation-only. College Board, Khan Academy, OpenStax, and
  license-unclear sources are blocked from acquisition and product retrieval.
- The current content ledger is a controlled/simulated review artifact. A real
  human review of all 103 questions and 24 lessons remains a release blocker for
  student deployment.
- Reported educational gains are synthetic simulation, not real student or SAT
  score outcomes. A human study has not been completed.

## Repository map

- `app/`: API, policy, PostgreSQL infrastructure, sync, memory, retrieval;
- `web/`: PWA shell, IndexedDB/sync code, service worker, Node tests;
- `content/`: schemas, governed drafts/reviews, published packs, quarantine;
- `tests/`: unit, integration, security, and two-session golden tests;
- `evals/`, `scripts/`, `reports/`: reproducible evaluation and evidence;
- `docs/ONE_PAGE_WRITEUP.md`, `docs/SUBMISSION_READINESS.md`, and
  `docs/DEMO_SCRIPT.md`: submission assets and remaining gates.

## One reproducible setup and verification path

Requirements: Python 3.11+, Node 18+, Docker with Compose.

```bash
cd bridgesat-agent
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python scripts/dev_env.py up
.venv/bin/python -m pytest
node --test web/tests/*.test.js
.venv/bin/python -m evals.run_all
.venv/bin/python scripts/import_content_pack.py
.venv/bin/python scripts/seed_demo.py
.venv/bin/uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000`. The first profile creation requires the server;
after the content pack and strategy snapshot are cached, practice, hints, local
adaptation, refresh recovery, and event queuing continue offline.

The evaluation runner prepares approved PostgreSQL content and its `tsvector`
index before retrieval and performance evaluation; it does not rely on hidden
database state.

## Measured evidence

The latest values are regenerated by `.venv/bin/python -m evals.run_all` and
recorded in `reports/final_summary.md` and `docs/EVIDENCE_PACK.md`. Results are
labeled as synthetic simulation or controlled internal tests. The fresh
closeout baseline is 850 passed Python tests and 57 passed Node tests. The
current pack audit is 1799/1799 (100%), and the pack contains 103 questions and
24 lessons across 8 math skills. Controlled evidence also records 10/10
offline/sync scenarios, PostgreSQL similarity recall@3 and next-action accuracy
of 100%, and restricted-source retrieval hits of zero. H9 freezes deterministic
mode with all five Hybrid flags at `0`; see
`docs/HYBRID_FINAL_CONFIGURATION.md` for the exact configuration and
`docs/SUBMISSION_READINESS.md` for unresolved human and submission-asset work.

## Safety and data governance

Bearer-token scope determines learner identity; request bodies cannot select a
different learner. Immutable event IDs and device sequences protect sync
integrity. External text is treated as data, never as a system instruction.
Only review-approved records may be selected by the PWA, and deletion propagates
through a transactional outbox to rebuildable enhanced indexes.

Historical SQLite plans and migrations are retained only as superseded design
history. The current runtime authority is PostgreSQL.
