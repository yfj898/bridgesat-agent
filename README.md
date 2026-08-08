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
- a published content pack `bridgesat-math-0.1.0`: 55 original math items
  across four skills (`linear_equations`, `systems_equations`,
  `ratios_percentages`, `functions_models`), plus 8 micro-lessons and 8
  worked examples, all with reviewer ledgers, licenses, and content hashes;
- scoped bearer-token authentication: `POST /v1/students` returns a
  one-time token (only its hash is stored), and every student-scoped
  endpoint derives the learner identity from the token, never from the
  request body;
- an immutable SQLite event log with sync protocol: offline queuing,
  idempotent batches, refresh recovery, version-bound scoring;
- episodic/strategy long-term memory (SQLite) with an optional Mnemis gateway
  that degrades to SQLite fallback on timeout or unavailability;
- optional LLM enhancements that never become dependencies of the main loop:
  a dual-mode next-action decision (`BRIDGESAT_LLM_API_KEY`) that prefers the
  LLM's structured decision and falls back to the deterministic policy on any
  failure, and an LLM-backed local memory index that distills episode
  summaries and reranks recall, degrading to the authoritative SQLite recall
  when the endpoint is unreachable;
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
  dependencies of the main loop. The LLM layer (`app/agent/llm_client.py`,
  `app/memory/nvidia_backend.py`) is the same kind of conditional enhancement:
  unset `BRIDGESAT_LLM_API_KEY` and every code path is byte-identical to the
  deterministic engine.

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

Requirements: Python >= 3.11, Node >= 18 (for the web tests).

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
pytest                                  # full test suite (278 tests)
python -m evals.run_all                 # regenerate every eval report
node --test web/tests/*.test.js         # offline/weak-network/accessibility core paths
```

The PWA issues its own bearer token on first profile creation, so no manual
login is needed for the demo. Manual API use requires the token returned by
`POST /v1/students` (`Authorization: Bearer <token>`).

### Optional LLM enhancements

Set `BRIDGESAT_LLM_API_KEY` (plus optional `BRIDGESAT_LLM_BASE_URL`,
`BRIDGESAT_LLM_MODEL`, `BRIDGESAT_LLM_TIMEOUT_MS`) to enable the dual-mode
decision and the LLM-backed memory index. Without the key every path is
unchanged. With `BRIDGESAT_MODE=enhanced`, the index is used by the memory
outbox worker:

```bash
export BRIDGESAT_LLM_API_KEY=nvapi-...   # never committed; env-only
export BRIDGESAT_LLM_TIMEOUT_MS=8000     # default; NIM queues can exceed 1s
export BRIDGESAT_MODE=enhanced
```

The default model is `deepseek-ai/deepseek-v4-flash-0731` on
`https://integrate.api.nvidia.com/v1` (OpenAI-compatible). LLM failures are
never fatal: decisions fall back to the deterministic policy and recall falls
back to SQLite, exactly as before.

The route layer (`/v1/adapt`) is wired to the LLM: with a key configured the
next action is decided inside the `AdaptResponse` action domain, while the
mastery update stays deterministic. A slow or timed-out call falls back to
the deterministic policy mid-request, so a cold model never stalls a
session.

Verified NVIDIA NIM availability and behavior (free tier, 2026-08-08).
Measured with the decision task (JSON-only action selection):

- `deepseek-ai/deepseek-v4-flash-0731` — **default**; correct JSON at
  `max_tokens=120`, math ~0.7s, decision ~1-9s (occasional queue). Same
  model family as the opencode assistant runtime.
- `openai/gpt-oss-120b` — strong quality but it is a reasoning model: it
  returns `content=None` until `max_tokens` ~400 (the reasoning pass fills
  the budget), and intermittently returns empty content. Usable via
  `BRIDGESAT_LLM_MODEL=openai/gpt-oss-120b` + larger max-token headroom;
  our client treats empty content as unavailable, so a missed call degrades,
  it never crashes.
- `nvidia/nemotron-3-super-120b-a12b` — correct JSON, ~3s;
- `nvidia/nemotron-3-nano-30b-a3b` — correct JSON, ~1-4s;
- `nvidia/llama-3.3-nemotron-super-49b-v1.5` — correct but very slow (16-46s);
- `thinkingmachines/inkling` — reasoning-only output (content always None),
  not usable for the structured decision;
- `meta/llama-3.1-8b-instruct` — fine but weaker than the default;
- `meta/llama-3.1-70b-instruct` — works; cold ~51s, warm ~18-31s. Use
  `BRIDGESAT_LLM_TIMEOUT_MS=90000` and expect occasional timeouts on the
  first call (which degrade to the deterministic policy);
- `meta/llama-3.3-70b-instruct` — exceeds 90s on this key; not usable;
- `nvidia/llama-3.1-nemotron-70b-instruct`, `moonshotai/kimi-k2.6`,
  `mistralai/mistral-large*`, `z-ai/glm-5.2` — 404/unauthorized or timeout.

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
| security + sync suites | controlled internal test | 74 passed |
| web core-flow tests | controlled internal test | 21 passed, 0 failed |
| educational improvement over control | synthetic simulation | +5.7pp correctness |

Not yet measured or not yet done (each labeled honestly):

- **real educational outcome** — requires a human usability study;
- **accessibility manual walkthrough** — items marked "manual check required"
  in `reports/accessibility_eval.md`;
- **submission assets** — screenshots, one-page description, and the
  3-minute demo video from `docs/IMPLEMENTATION_PLAN.md` section 14 do not
  exist yet;
- **human content review** — the review ledger is a simulated pass (see
  "What is delivered"); a real human review of the 55 items is outstanding;
- **newer API surface** — the full session/memory API contract
  (`/v1/sessions`, `/v1/memory/*`) is not yet built; only the legacy
  endpoints plus sync/content/knowledge routers exist.

## Status

Competition MVP implemented end to end; every pre-submission checklist item
in `docs/COMPETITION_MVP_EXECUTION_PLAN.md` section 12 is closed at the code
level except the human items above (usability study, accessibility
walkthrough, submission assets, real content review) and the demo recording
itself.
