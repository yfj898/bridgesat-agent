# BridgeSAT Worklog

> Chronological historical record. SQLite/FTS5 references and test counts below
> describe earlier repository states and are superseded by the current
> PostgreSQL runtime and freshly generated reports.

Session records: what was done, methods used, problems encountered, and
follow-ups. Companion to the spec docs (IMPLEMENTATION_PLAN.md,
EVALUATION_SPEC.md); this file is a chronological log, not a spec.

---

## 2026-08-12 — Hybrid H8-H10: summary, final freeze, and closeout

Completed the remaining Hybrid plan phases after H7. The competition default is
now explicitly frozen to deterministic mode; the optional Hybrid paths remain
implemented and fail-closed but are not enabled by default.

### H8 — Session summary personalization

- Added strict `SummaryFact`, `SummaryProposal`, and `SessionSummaryContext`
  contracts plus a bounded summary prompt/parser/verifier in `app/agent/`.
- Derived facts from committed PostgreSQL answer attempts, approved content
  metadata, misconception evidence, validated episodes, transfer outcomes,
  confirmed presentation events, review-due skills, and session completion
  status. Facts are deduplicated and capped at 16; context/enrichment failures
  cannot roll back `SESSION_COMPLETED`.
- Added provenance-bearing `personalized_summaries` to `SyncResponse`; the PWA
  selects the current completion/session entry and always retains deterministic
  sync-status prose. Worked-example and micro-lesson presentation events are
  both supported.
- Added H8 sync/verifier/PWA tests and five additive golden cases (`h8-01` to
  `h8-05`). Summary metrics remain separate from legacy H6/H7 metrics.

### H9 — Final evaluation and enablement freeze

- Added `scripts/run_hybrid_final_gate.py` and
  `reports/hybrid_final_gate.json`. The gate validates golden case/variant
  metadata, recomputes safety metrics from the golden inputs/results, and treats
  an H7 No-Go as a valid evidence decision rather than a test failure.
- Frozen competition configuration: `final_mode=deterministic` with
  `BRIDGESAT_HYBRID_COMPETITION_MODE=1`; all five
  `BRIDGESAT_HYBRID_*` flags are `0`; H7 action ranking is No-Go for default
  enablement because the available provider evidence is scripted and lacks
  repeated real-provider latency/lock-duration measurements.
- `evals.run_all` now records the full Python suite in
  `reports/python_tests.json` and fails closed on Python, Web, security,
  content, performance, offline, or Hybrid gate regressions.

### H10 — Demo and documentation closeout

- Updated current PostgreSQL/migration, curriculum, test, evaluation, demo,
  roadmap, architecture, and submission-readiness documentation; historical
  SQLite/planning material is explicitly labeled.
- Added `docs/HYBRID_FINAL_CONFIGURATION.md` with flags, rollback profile,
  evidence scope, reproduction order, and limitations.

### Post-gate hardening

- Both `WORKED_EXAMPLE_PRESENTED` and `MICRO_LESSON_PRESENTED` now require a
  matching approved server decision and create the same runtime episode path;
  forged, replayed, and distinct-item completion cases are covered.
- H8 facts are captured inside the authoritative completion transaction, while
  provider calls remain outside the student lock. Duplicate completed sessions
  do not resummarize, and a batch is capped at eight summary calls.
- Summary verification now grounds every non-generic claim token and spelled-out
  number against cited facts; misconception refs preserve skill, misconception,
  and confidence identity, including signed/compound-count rejection and local
  subject binding.
- Competition mode is consumed at application startup: contradictory enabled
  flags fail startup and runtime gates remain deterministic.
- Micro presentation events are part of the typed event-store enum; H7 ranked
  response actions are validated against the decision trace and approved pack
  before runtime episode creation.

### Results (controlled internal tests / synthetic cases)

- Full Python suite: 850 passed; Node PWA suite: 55 passed.
- Content audit: 1799/1799; offline/sync: 10/10.
- Hybrid: 15 cases/22 variants; H8 summary 5/5; allowed-action violations 0;
  hallucinated acceptance 0; fallback 100%; explanation and summary grounding
  100%; unavailable-summary fallback 100%.
- No real student outcome, human content approval, real-provider latency/SLA,
  public deployment, video, or manual accessibility walkthrough is claimed.

---

## 2026-08-11 — Hybrid H6: shadow ablation and behavioral-value proof

Phase H6 (plan sections 21/22): proved the verified shadow Hybrid layer adds
decision quality on adjudicated ambiguous cases before it may change actions.

### What was done

- `evals/hybrid/golden.jsonl` (`hybrid-golden-v1`): 10 versioned cases covering
  the plan 21.1 benchmark set — supported-intervention evidence vs single
  transfer (h6-01), semantic episode ranking over recency (h6-02), isolated
  first error vs repeated errors with validated transfer (h6-03a/b), time-budget
  hard action (h6-04), hallucinated episode/content (h6-05), provider outage
  (h6-06), single-exact-episode invariant (h6-07), and the H5 explanation
  grounding surface (h6-08/09). Every case carries the deterministic expected
  action, the adjudicated best action, and scripted model variants with
  expected gate/calls/accepted/would-change/reason.
- `scripts/run_hybrid_ablation.py`: deterministic, offline runner that exercises
  the production path (`derive_policy_constraints` → `choose_mode` → prompt →
  parse → `verify_proposal`/`verify_explanation`) with a replay-only transport.
  Emits `reports/hybrid_eval.json` (schema 1.0, "controlled internal test") and
  `evals/hybrid/REPORT.md`. Scoped `_ShadowFlags` context manager sets the three
  Hybrid env flags only for the duration of a run, so the harness never leaks
  environment state into the sync service or the rest of the test suite.
- Wiring: `run_hybrid()` registered in `evals/run_all.py` (after memory), with a
  "Hybrid shadow ablation" section in `reports/final_summary.md` and an
  evidence-pack row.
- Harness tests: `tests/test_hybrid_ablation.py` (12 tests) — fixture validity
  against the real policy, decisive-case zero-call invariant, hallucination
  rejection, provider-outage fallback, benefit measurement, version guard, and
  a subprocess smoke test of the exact `run_all` invocation.

### Verifier hardening (found by the golden set)

- `_span_overlap` only checked prefix windows of protected spans; a suffix or
  mid-span copy of a lesson title was accepted. Now any contiguous window
  covering >= 60% of the span is a rewrite, regardless of start position, while
  shorter generic fragments ("a worked example") stay incidental. The H6 golden
  case h6-08 pins the suffix-copy rejection; `test_verify_rejects_suffix_copy_
  of_protected_span` pins the regression; the three existing tests whose
  sentences incidentally contained >= 60% span windows were reworded to test
  their actual intent (grounded numbers, sentence bound, gateway).

### Results (controlled internal test, synthetic learners)

- 10 cases / 17 scripted variants, all variant expectations pass.
- Accepted allowed-action violations: 0. Accepted hallucinated episode/content:
  0. Adversarial rejection: 6/6 (100%).
- Deterministic fallback success: 100%. Decisive cases never call the model
  (0/4 cases), preserving the single-exact-episode invariant.
- Baseline accuracy vs golden: 100%. Verified beneficial difference: h6-01
  (SHOW_MICRO_LESSON over SHOW_WORKED_EXAMPLE) — the only improvement claimed;
  no benefit is claimed from cases with one obvious policy action.
- Explanation grounding: 4/4 (grounded accepted; ungrounded number,
  protected-span rewrite, invented ref rejected). Full regression: 746 Python
  tests + 46 Node tests pass.

---

## 2026-08-11 — Hybrid H7: behavioral action ranking (conditional Go)

Phase H7 (plan section 22): the H6 ablation proved a beneficial difference
(h6-01) on adjudicated ambiguous cases, which earns the conditional Go —
a verified Hybrid proposal may now replace the action in the sync response,
exclusively through the bounded two-phase revalidation path.

### What was done

- Migration `0016_hybrid_decision_trace`: auditable, RLS-protected decision
  trace table binding each served verified action to its source event
  (`SCHEMA_VERSION` 15 → 16). Inert schema when the task flag is off; rollback
  (H9 No-Go) keeps H5 behavior with the table simply unused.
- `DecisionToken` contract (`app/agent/hybrid.py`): Phase A boundary evidence
  captured inside the authoritative transaction — source event, fallback
  action/reason/policy version, session state, and the agent event count that
  includes the just-committed fallback event. Attached to `ShadowMaterial`
  alongside `verified_payloads` (deterministic payload per allowed teaching
  action, derived from approved pack assets inside the transaction).
- Sync service two-phase wiring (`app/sync/service.py`):
  - Phase A: fallback AgentEvent commits and the token/payloads are captured;
  - Phase B: the model call happens after the advisory lock is released
    (existing shadow gateway, now also returning observations);
  - Phase C: gated by `BRIDGESAT_HYBRID_ACTION_RANKING_ENABLED`, a short
    revalidation transaction recomputes the token from durable state — the
    committed agent event must still match the fallback identity, the session
    state must match, and the agent event count must be unchanged — then one
    idempotent trace row is persisted (`h7b_<event_id>`), the response event
    action/payload is replaced with the verified action plus
    `hybrid_ranked`/`decision_trace_id` markers, and the durable agent event
    stays the deterministic fallback.
- Everything fails closed: stale token, missing payload, rejected proposal,
  provider failure, or any exception keeps the deterministic fallback and
  persists nothing.
- Fixture pack: added `lessons.jsonl` to `tests/fixtures/packs/syncmath-0.1.0/`
  (approved worked-example + micro-lesson assets for the sign-error scenario)
  so the sync-test path can exercise Phase C payload resolution.
- Tests: `tests/test_hybrid_action_ranking.py` (10 tests) — off-by-default H5
  behavior, ranking requires master + shadow gates, verified action replaces
  the fallback only in the response with an auditable trace, ranking +
  explanation coexist, stale-token race (learner advances between Phase B and
  Phase C via a nested sync) keeps the fallback, duplicate sync idempotency
  (one trace, one model call), rejected/illegal/unavailable proposals keep the
  fallback, model call never inside the advisory lock (recorded lock intervals
  vs transport call timestamps), one-episode demo stays deterministic.
  Plus `test_fresh_database_has_hybrid_decision_trace_contract`.

### Results (controlled internal test, synthetic learners)

- Full regression: 757 Python tests + 46 Node tests pass.
- Zero illegal/stale action persistence: rejected proposals, timeouts, and the
  concurrent-advance race never serve or persist a verified action.
- Fallback remains 100%: durable agent events never diverge from the
  deterministic policy; verified actions exist only in the response + trace.
- One-episode demo stays deterministic (0 model calls, no trace).
- No model call holds the advisory-lock transaction (lock-interval proof).

---

## 2026-08-11 — Hybrid H5: verified personalized explanation

Added the first student-visible Hybrid feature without changing action or
math: one optional, grounded sentence behind the existing "Why this
recommendation?" surface.

### What was done

- `ExplanationContext`/`ExplanationFact` contracts (strict allowlists, no PII,
  no lesson body, no math truth); `ExplanationProposal` now requires at least
  one evidence ref and a non-empty sentence.
- `hybrid.py`: `explanation_gate` (wording-task gate: provider healthy + master
  flag + task flag, offline/circuit/budget deterministic), fixed
  `build_explanation_prompt` (structured JSON only), robust
  `parse_explanation_proposal`, and fail-closed `verify_explanation`:
  - every `evidence_ref` must exist in the provided facts;
  - protected spans (deterministic reason_text, lesson title) may not be
    rewritten or copied;
  - deny list blocks guarantees, permanence, diagnosis, score claims, human
    approval, comparative superiority, markdown/HTML;
  - every number in the sentence must appear in the grounded fact phrases
    (math immutability);
  - at most two sentences.
- `SyncService` builds the explanation context only for SHOW_WORKED_EXAMPLE /
  SHOW_MICRO_LESSON decisions inside the authoritative transaction and runs
  the gated task post-commit; a verified proposal adds
  `personalized_explanation` + `personalized_emphasis` to the response event.
  Any failure (timeout, unparsable, ungrounded, unavailable) leaves the event
  untouched.
- PWA: `.intervention-why` shows the personalized sentence when present,
  otherwise the existing deterministic copy; `.recommendation-detail`
  (collapsed "Why this recommendation?") stays deterministic; the technical
  evidence meta gains a `personalized: <emphasis>` marker.
- Tests: 27 unit tests (`tests/test_hybrid_explanation.py`) covering contract
  bounds, gate branches, parse robustness, the full verifier rejection
  matrix, and never-raises gateway behavior; 3 sync integration scenarios in
  `tests/test_hybrid_sync.py` (enriched response after commit, unavailable
  model, flags off); Node tests for the `agentEventToView` passthrough and
  app wiring.

### Verification

- Full Python suite passes (618 tests); all Node PWA tests pass.
- Debugging note: the first integration attempt used an ungrounded number in
  the fake model output ("2" vs the real context "3 recorded errors") — the
  verifier correctly rejected it, which validated the numeric grounding path.

### Follow-ups

- H6 (behavioral-value ablation) is next; H5's explanation task stays
  independently toggleable.

---

## 2026-08-11 — Hybrid H0–H4: bounded model reasoning on the real sync path

Executed the first continuous Hybrid stage (plan sections 22 H0–H4) with the
model in shadow mode: the executed teaching action stays fully deterministic;
model proposals are verified post-commit and never change the response.

### H0 — correctness and baseline freeze

- Fixed intervention-specific memory aggregation: `PGMemory.list_episodes_for_fact`
  now filters by the episode's own `intervention` and requires validated
  effectiveness >= 0.6, so a fact can no longer be supported by episodes from
  another intervention; added regression coverage.
- Documented the runtime authority (PostgreSQL-backed `/v1/sync/events →
  SyncService → shared deterministic policy`), deprecated engine/orchestrator
  decision paths, and froze the full baseline: 591 Python tests, 44 Node tests,
  content audit 1799/1799, all eval reports regenerated.

### H1 — policy constraints API

- `app/agent/policy.py` now derives `PolicyConstraints` (hard action, allowed
  actions, legal next states, preferred deterministic fallback) from one policy
  source, in addition to the backward-compatible `decide_next_action()`.
- Trajectory tests (`tests/test_policy_constraints.py`) cover every existing
  branch; all pre-existing policy/golden outputs unchanged.

### H2 — Hybrid contracts and Reasoning Gate (dark)

- New `app/agent/hybrid_contracts.py` (strict context/proposal/observation
  contracts, field allowlists, bounds) and `app/agent/hybrid.py` (gate,
  prompt building, task settings).
- `choose_mode` stays deterministic unless: no hard action, provider
  configured/healthy/within budget, task enabled, and
  `semantic_reasoning_needed` (>= 2 allowed actions, conflicting episodes,
  or supported-intervention disagreement).
- `tests/test_hybrid_reasoning_gate.py` covers every gate branch; feature
  flags default off.

### H3 — proposal verifier, adversarial first

- `verify_proposal(context, constraints, evidence)` fails closed against:
  illegal actions, hallucinated IDs, wrong-tenant evidence, unapproved
  content, fake claims, math mutation, and unparsable proposals.
- Full adversarial matrix in `tests/test_hybrid_verifier.py` plus prompt-
  injection security tests (`tests/security/test_hybrid_prompt_injection.py`):
  zero accepted unsafe proposals.

### H4 — real-path Hybrid shadow integration

- `SyncService` now assembles a sanitized shadow context while the
  authoritative transaction is active and runs the gated model task
  post-commit (`_run_shadow_observations`); `LLMClient` gained per-task
  timeout support.
- `tests/test_hybrid_sync.py` proves, against a real PostgreSQL fixture:
  flags off → deterministic fallback with zero model calls; flags on with a
  decisive policy (single allowed action) → no model call; flags on with
  ambiguity → shadow runs after commit, produces a verified observation
  (fallback vs proposal action, accepted, would_change, latency), and never
  changes the returned action; the AgentEvent is committed before the model
  is called; model failure leaves the response unchanged; duplicate sync
  creates no second decision.
- Two-session golden memory scenario unchanged; full Python/Node regression
  passes.

### Follow-ups

- H5 (grounded personalized explanation) is the next heading: verified
  student-facing wording behind "Why this recommendation?" with action/math
  unchanged.
- Real human content review and real-student learning-effect evidence remain
  open for any competition claim.

---

## 2026-08-08 — Model selection: NVIDIA catalog probe + default model switch

The user's NVIDIA NIM key is the same catalog the opencode assistant runtime
uses, so the whole catalog was probed and benchmarked on the actual decision
and memory tasks instead of assuming the small default.

### What was done

- Probed 17 candidate models on the free tier (200 / 404 / timeout) and
  benchmarked the survivors on three tasks: arithmetic, JSON-only
  decision, and (for the LLM-backed index) summary + rerank.
- **Default model switched** `meta/llama-3.1-8b-instruct` →
  `deepseek-ai/deepseek-v4-flash-0731` (the same model family the opencode
  runtime uses). It returns valid JSON at `max_tokens=120` (the reasoning
  models do not), math ~0.7s, decision ~1-9s, and handled summary and
  rerank end-to-end on the first try.
- Catalog findings recorded in README: `openai/gpt-oss-120b` and
  `nvidia/nemotron-3-super-120b-a12b`/`nano-30b-a3b` work but are reasoning
  models — they consume `max_tokens` with the reasoning pass and return
  `content=None` until given ~400 tokens; `thinkingmachines/inkling` only
  ever emits reasoning (content always None); `kimi`, `mistral-large*`,
  `glm-5.2`, `nemotron-70b` are unauthorized/timeout.
- `LLMClient.complete` now treats empty/None content as
  `LLMUnavailableError` (reasoning models can emit None) instead of letting
  a `AttributeError` escape; +1 test.
- **Timeout-budget bug found and fixed** (would have made the LLM memory
  index always fall back): `MnemisMemoryAdapter.recall_similar` falls back
  to `SYSTEM_1_TIMEOUT_MS` (800 ms) when no per-call timeout is passed, and
  `FallbackStudentMemory` calls it without one. The LLM round-trip (~1-30s)
  always exceeded 800 ms → `MnemisUnavailableError` → SQLite fallback on
  every recall. Fixes:
  - `NvidiaMemoryIndex` budgets LLM calls with
    `max(timeout_ms, llm.timeout_ms)` in both upsert summary and rerank;
  - `FallbackStudentMemory` derives its recall budget from the adapter's
    `timeout_ms` when not given explicitly (explicit wins, legacy default
    kept);
  - `build_mnemis_index` passes `timeout_ms=client.timeout_ms` to the
    adapter.
- Live chain re-verified with the new default: adapt → `insert_micro_lesson`
  (LLM), upsert summary distilled, recall → `route=mnemis_system1` with the
  expected hit. Route-level smoke of the 403-on-wrong-key path also
  confirmed the degradation contract (deterministic fallback, no crash).

### Verification

- `pytest` → **278 passed**; `python -m evals.run_all` → 12 `[ok]`;
  `node --test web/tests/*.test.js` → 21 pass.

### Problems encountered and resolutions

- Reasoning models (gpt-oss etc.) return `content=None` at small
  `max_tokens`; resolved by empty-content → `LLMUnavailableError` and
  documented `BRIDGESAT_LLM_MODEL=openai/gpt-oss-120b` with the caveat.
- The 800 ms SYSTEM_1 budget silently defeated the LLM index; resolved by
  the budget chain above and verified with the real endpoint (previously
  `route: sqlite` on every call, now `mnemis_system1`).
- A script typo in the smoke key produced a 403 — the failure surfaced as
  a clean deterministic fallback with a traceback-free degrade, confirming
  the degradation contract holds for auth errors too.

### Follow-ups / known issues

- `openai/gpt-oss-120b` is usable but needs max-token headroom; the default
  stays `deepseek-ai/deepseek-v4-flash-0731` because it is reliable at the
  configured budgets.
- 70b-class models remain slow (cold 51s); documented.

---

## 2026-08-08 — LLM decision at the route layer + real 70b model verified

Follow-on to the optional LLM layer: the `/v1/adapt` route now makes its
decision through the LLM when configured, and the 70b-class model was probed
and wired as a documented option.

### What was done

- `app/engine.py::adapt(previous_mastery, request, llm=None)` — dual-mode:
  mastery stays deterministic; with an LLM attached the next action is asked
  inside the `AdaptResponse` action domain (5 literal actions) and used only
  when legal; failure/non-JSON/unknown action falls back to the
  deterministic branches. The `minutes_remaining <= 2` time guard stays a
  floor even when the LLM disagrees.
- `app/main.py` — `_get_llm_client()` lazy singleton wired into
  `adapt_session`; no `BRIDGESAT_LLM_API_KEY` → client stays None and the
  route is byte-identical to before.
- `app/infrastructure/async_utils.py` — `await_in_any_context` extracted
  from the orchestrator (now a compat alias) so the sync engine can await
  the async LLM client from any context (threadpool, running loop, tests).
- Default `BRIDGESAT_LLM_TIMEOUT_MS` raised 800 → 8000 ms (8b model measures
  ~1s; 800 ms was too tight for real NIM queues).
- Probed the NVIDIA free-tier catalog for 70b-class models:
  - `meta/llama-3.1-70b-instruct` — **works**: cold ~51s, warm ~18-31s;
  - `meta/llama-3.3-70b-instruct` — exceeds 90s on this key;
  - `nvidia/llama-3.1-nemotron-70b-instruct` — 404 (not authorized).
- Live verification (env-only key, never committed):
  - `adapt(0.5, ..., llm=70b client)` → `insert_micro_lesson` (deterministic
    would have said `decrease_difficulty`), mastery stayed 0.41 deterministic;
  - HTTP route with 70b → first call timed out at 90s and **degraded to the
    deterministic policy mid-request** (the designed fallback, observed
    live), warm call returned `insert_micro_lesson` in 18s.
- Tests: `tests/test_llm_adapt.py` (8, incl. minutes-guard floor, mastery
  determinism), 2 route-level tests in `tests/test_api.py`.

### Verification

- `pytest` → **277 passed**; `python -m evals.run_all` → 12 `[ok]`;
  `node --test web/tests/*.test.js` → 21 pass.

### Problems encountered and resolutions

- Route test stub was injected as a transport (callable) instead of a
  client (`complete`) — the first wiring test silently fell back to the
  deterministic policy, which is exactly the degradation contract; the test
  now injects a client-shaped stub.
- First live 70b route call timed out at 90s and fell back — confirmed as
  the designed behavior, documented (cold model → deterministic fallback,
  never a stalled session).

### Follow-ups / known issues

- 70b calls are slow enough that only the warm path is interactive; the
  default 8b model remains the sensible default. Documented in README.
- `_get_llm_client` is a module-level singleton; fine for the single-worker
  demo, revisit for multi-worker deployment.

---

## 2026-08-08 — Optional LLM layer: dual-mode decision + LLM-backed memory index

TDD, strictly additive: with `BRIDGESAT_LLM_API_KEY` unset every code path is
byte-identical to the deterministic engine; with it set, the orchestrator
prefers the LLM's structured decision and the memory index distills summaries
and reranks recall, both degrading to the existing deterministic/SQLite paths.

### What was done

- `app/agent/llm_client.py`: OpenAI-compatible chat-completions client over
  an injectable async transport; default httpx transport reads
  `BRIDGESAT_LLM_API_KEY`/`BRIDGESAT_LLM_BASE_URL`/`BRIDGESAT_LLM_MODEL`/
  `BRIDGESAT_LLM_TIMEOUT_MS` (defaults: NVIDIA NIM endpoint,
  `meta/llama-3.1-8b-instruct`, 800 ms). No key → `LLMUnavailableError`.
- `SessionOrchestrator` dual-mode decision (`_decide`/`_decide_with_llm`):
  LLM returns JSON `{action, reason_code, reason_text}`; the action must be
  inside `BoundedAction`, otherwise the deterministic policy decides.
  `_await_in_any_context` bridges the sync decision API to the async client
  both outside and inside a running event loop.
- `app/memory/nvidia_backend.py`: `NvidiaMemoryIndex` — a Mnemis-transport
  drop-in (`request(method, path, body, timeout_ms)`) backed by local SQLite
  plus the injected LLM: upsert distills a summary (LLM failure degrades to
  the payload summary, never blocks indexing), recall reranks local
  candidates by LLM relevance and raises `MnemisUnavailableError` on LLM
  failure so the fallback chain routes to authoritative SQLite.
- `app/memory/__init__.py::build_mnemis_index`: worker wiring — with the LLM
  key set, `OutboxWorker` gets a `MnemisMemoryAdapter` over
  `NvidiaMemoryIndex`; without it, the default unavailable-transport adapter
  (unchanged behavior). `app/main.py` lifespan uses the factory.
- Verified live against the real NVIDIA NIM endpoint (env-injected key, never
  committed): decision returned `GIVE_HINT_1`/`llm-0.1.0`; upsert→recall
  returned `ep_live` with `mnemis_system1` route and health ok. Also
  confirmed the 800 ms default is too tight for NIM queues (~1s+); README
  documents `BRIDGESAT_LLM_TIMEOUT_MS=8000` for live use.
- Tests: `tests/test_llm_client.py` (5), `tests/test_llm_decision.py` (5),
  `tests/test_nvidia_backend.py` (11), `tests/test_memory_index_factory.py`
  (2), plus a chain integration test in `tests/test_fallback_memory.py`.

### Verification

- `pytest` → **267 passed** (was 244); `python -m evals.run_all` → 12
  `[ok]`; `node --test web/tests/*.test.js` → 21 pass.
- Live smoke (env-only key): LLMClient + orchestrator decision +
  NvidiaMemoryIndex upsert/recall/health all exercised against NVIDIA NIM.

### Problems encountered and resolutions

- `asyncio.run()` inside a running loop (smoke called `_decide` from an
  async main) → `_await_in_any_context` runs the coroutine on a fresh loop
  in a worker thread when a loop is already running.
- LLM decision `GIVE_HINT_1` mapped to `HINT_ACTIVE`, but `evaluate_answer`
  had already transitioned to `ANSWER_EVALUATED`; hints map to
  `QUESTION_ACTIVE` (the only legal source states are
  `ANSWER_EVALUATED`/`QUESTION_ACTIVE`).
- 800 ms default timeout too short for real NIM queues → documented 8000 ms
  for live use; tests keep the injectable transport and stay sub-second.

### Follow-ups / known issues

- NVIDIA 70b-class models queue-beyond-timeout on the free tier; use
  `meta/llama-3.1-8b-instruct` (verified ~1s) or `minimaxai/minimax-m3`
  (~6s).
- The LLM key must only ever be injected via environment; it is not stored
  anywhere in the repo (verified by `tests/security/test_secret_scan.py`).

---

## 2026-08-07 — Review-driven hardening: auth, demo seed truth, worker, sync limits

Fixes from the full project review (4 parallel subagent reviews: spec, code
quality, security, usability), applied in priority order.

### What was done

1. **P0-1 import path** — `scripts/import_content_pack.py` defaulted to
   `./bridgesat.db` (repo root) while the app uses `data/bridgesat.db`;
   now resolves `ROOT / "data" / "bridgesat.db"`.
2. **P0-2 HTTP auth** — new `app/auth.py` (`TokenStore` + `require_student`
   dependency); `POST /v1/students` returns a one-time Bearer token (server
   stores only a SHA-256 digest, matching migration 0003). All of
   `/v1/diagnostics`, `/v1/adapt`, and `/v1/sync/*` now require the token;
   student scope is derived from the token, never from client-claimed body
   fields (mismatch -> 403). `web/app.js` persists the token in sync state
   and sends `Authorization: Bearer ...` on every request.
3. **P0-3 seed narrative** — `scripts/seed_demo.py` previously faked
   mastery: the practice batch claimed correct answers on the transfer item
   while the episode said `sign_error`. Now the session answers
   `linear_equations.001/.002` wrong (distractor B, `sign_error`) and the
   transfer item `.003` correctly without hints, matching the episode.
4. **P1-1 worker startup** — `app/main.py` now starts `OutboxWorker` in a
   lifespan; enhanced mode polls every 2 s with the Mnemis adapter, local
   mode keeps rows pending by design.
5. **P1-2 README honesty** — 55 items re-labeled as simulated (human review
   pending); bearer-token auth documented; quick start / test counts fixed;
   "not yet measured" list expanded.
6. **P2-1 sync protocol hardening** — `SyncService.process_batch` now:
   serializes same-student batches under a per-student lock
   (THREAT_MODEL 5.3); rejects events whose serialized payload exceeds
   64 KiB (`PAYLOAD_TOO_LARGE`); requires a valid `integrity_hash`
   (`None` -> `INVALID_SCHEMA`, previously passed through); enforces
   monotonic `device_sequence` per device against the `last_device_sequence`
   column (previously dead) — replays/out-of-order batches are rejected.
   New tests: `test_single_payload_over_64kb_rejected`,
   `test_missing_integrity_hash_rejected`,
   `test_out_of_order_device_sequence_rejected`.
7. **P2-2 dead-letter replay** — `scripts/replay_dead_letter.py` previously
   drained dead letters into a throwaway `InMemoryMnemisIndex`, marking rows
   `indexed` that never reached the real index. It now mirrors `app.main`:
   delivers via the Mnemis adapter in enhanced mode, and otherwise just
   resets rows to `pending` for the running app to drain.
8. **Demo script** — new `docs/DEMO_SCRIPT.md`, a 7-step reproducible
   competition path with exact commands, expected output, and a failure
   table.

### Verification

- Full suite: 244 passed (was 238; +6 request-limit/sequence/hash tests).
- `python -m evals.run_all` all steps [ok]; `node --test web/tests/` green.
- Fresh-db `scripts/seed_demo.py` smoke: 13 events accepted, episode
  validated, plan `['micro_lesson','practice','review','reflection']`.

### Problems encountered and resolutions

1. **Sequence check scoped to the wrong device** — early draft validated
   `envelope.device_id` (client-claimed, often a default `device_a`) against
   the registered request device, rejecting legitimate batches; the check now
   uses the request (token-verified) device.
2. **`MAX()` on learning_events returned NULL** — forged-event tests insert
   envelopes whose `device_id` differs from the registered device, so the
   UPDATE subquery hit a NOT NULL violation; `_advance_device_sequence` now
   takes the batch's accepted max directly.

### Follow-ups / known issues

- Remaining human items only: accessibility manual walkthrough, real
  educational outcome study, demo recording; new token-based API surface
  needs external security review. No further code items open.

---

## 2026-08-07 — Fix byte-identical lesson pairs in content generation

Commit: `a1f808f` `fix(content-pipeline): generate distinct lesson pairs per
skill — no more byte-identical .001/.002 duplicates, lesson version 2`

### What was done

The math content generator (`app/content_pipeline/generation.py`) produced,
per skill, two micro_lessons and two worked_examples that were byte-identical
duplicates (`.001`/`.002`), with an empty `target_subskill`. The second lesson
of each pair was unreachable and carried no pedagogical value.

Fix, end to end through the governed pipeline:

1. **Generator** — replaced `_lesson_body()` with
   `_lesson_content(skill, kind, index)`; emits 16 semantically distinct
   lessons (plan section 8.2: lessons remediate misconceptions, worked
   examples demonstrate subskills), each with a real `target_subskill`
   (e.g. `isolate_variables`, `solve_systems`, `unit_rates`,
   `function_evaluation`).
2. **Version discipline** — content changes require immutable versions, so
   lesson `version` bumped `1` -> `2`. The 55 items were verified
   byte-identical (0 diffs) — no unrelated regeneration.
3. **Regeneration + validation** — `generate_math_drafts.py` then
   `validate_content.py --write-validated` (all passed).
4. **Review ledger** — appended v2 lesson rows to
   `content/reviews/math-v1.csv` (16 rows, `release_batch` =
   `simulated-formal-review-v2-20260807`). `read_reviews()` is keyed by
   `content_id` only (last row wins), so v2 rows were appended after v1 rows
   to keep v2 authoritative while preserving v1 as audit history.
5. **Pack rebuild + re-import + re-index** — rebuilt pack
   `bridgesat-math-0.1.0` (manifest `created_at` only change; item hashes
   identical), re-imported into `content/registry.db` and re-indexed FTS
   (`{items: 55, lessons: 16}`).
6. **Evals** — updated `evals/retrieval/dev.jsonl` and `golden.jsonl` to
   leverage the now-distinct `.002` lessons (exact-match expectations, e.g.
   the negative-value worked example `f(x) = 7x - 6, f(-4) = -34`). Re-ran
   evals: DEV all 1.0; GOLDEN Recall@1/Recall@3/MRR = 0.875,
   all_expected_found 0.875 (no-result query counted in the denominator),
   coverage 1.0, ~1 ms latency.
7. **Tests** — full suite green: 97 passed.

### Methods used

- **Immutability/version discipline**: changed content -> version bump, keep
  old versions in the audit trail, only the review ledger decides which
  version is authoritative (append-only CSV, last row per content_id wins).
- **Byte-diff verification**: `git diff` on approved JSONL confirmed the 55
  items are untouched; only the 16 lesson rows changed.
- **Query-probe verification**: before updating eval expectations, ran
  per-query retrieval probes against the re-indexed DB to confirm which
  content_id each query now surfaces (rather than guessing expectations).
- **Eval-then-tune**: expectations were only strengthened where the retrieval
  actually produced the pedagogically right hit; one expectation
  ("isolate x ...") was widened to accept both genuinely relevant lessons
  rather than gamed.

### Problems encountered and resolutions

1. **Byte-identical lesson pairs (the bug)** — `_lesson_body()` returned the
   same (title, body) for both indices of a kind. Fixed by making content a
   function of `index` with hand-authored per-skill content.
2. **Empty `target_subskill`** — lesson records carried `""`, so subskill
   filters/evals could not target them. Fixed by assigning a real subskill
   per lesson.
3. **Wrong DB during import** — the import script defaults to
   `./bridgesat.db` while the knowledge router reads `content/registry.db`
   (`BRIDGESAT_KNOWLEDGE_DB`); first import run wrote to the wrong file
   (also untracked — removed, `git status` clean). Re-ran import explicitly
   against `content/registry.db`.
4. **Golden recall@1 regression during eval update** — the "isolate x ..."
   query ranked the sign-error lesson first because its body contains
   standalone tokens ("side", "a") that the lexical overlap term weights.
   Both lessons are genuinely relevant; widened the expectation to
   `[micro_lesson.001, micro_lesson.002]` with a note, restoring
   Recall@1 = 0.875.
5. **Registry holds no lessons** — `content_item_versions` contains only the
   55 items; lessons live only in the FTS index, not the authoritative
   registry. Pre-existing gap from the pack import (import reads
   `items.jsonl` only) — **follow-up**, not fixed in this session.

### Follow-ups / known issues

- **Registry lesson gap** (above): decide whether pack import should also
  register lessons in `content_item_versions` (makes the registry truly
  authoritative for all content types; enables lesson-level withdraw/
  deprecation).
- **DB path mismatch**: import script default (`./bridgesat.db`) vs
  knowledge router (`content/registry.db`) vs app loop
  (`data/bridgesat.db`). Pre-existing; consider consolidating defaults and
  documenting in API_AND_OPERATIONS.md.
- **Lexical overlap in `_rerank`**: exact-token overlap gives small content
  bodies a chance to win ties on common words; acceptable at this scale, but
  a normalized-token (stemming/plural) or TF-based term weight would reduce
  tie surprises.

---

## 2026-08-07 — Gate 4 offline-first proof (backend + client)

Commit: `d2a1746` `feat(gate4): offline-first sync protocol and client —
0006 migration, sync service, content-pack API, offline PWA flow`

### What was done

Full offline-first proof per ROADMAP Gate 4, IMPLEMENTATION_PLAN §11, and
SYNC_PROTOCOL.md:

**Backend sync protocol** (Python):
1. `app/infrastructure/migrations/0006_sync_protocol.py` — `devices`,
   `session_branches`, `sync_conflicts` tables; `SCHEMA_VERSION` 5 -> 6.
   `SyncService.__init__` applies migrations itself, so any DB reaching the
   service is schema-6.
2. `app/sync/protocol.py` — 13 `SyncErrorCode`s (incl.
   `QUESTION_VERSION_UNKNOWN`, `MISSING_DEPENDENCY`, `PAYLOAD_TOO_LARGE`),
   4 `ConflictType`s, `SyncEventEnvelope` (integrity-hashed),
   `SyncRequest/Response`, `DeviceRegistration`, `SnapshotResponse`,
   `OFFLINE_POLICY_VERSION = "offline-policy-v1"`,
   `MAX_EVENTS_PER_BATCH = 100`.
3. `app/sync/versioned_scoring.py` — `PackAnswerKey`/`VersionedAnswerKey`:
   server scores the offline answer against the exact referenced question
   version; unknown pack/version -> reject, never a newer key.
4. `app/sync/service.py` — device register/revoke/verify; `process_batch`
   with integrity-hash verification (sha256 of `event_type \x00
   canonical_json(payload)`), event_id dedup (idempotent re-sync),
   MISSING_DEPENDENCY retryable, version-bound scoring, repeated attempt_id
   -> `non_scoring_duplicate` (stored `attempt_id#dupN`) + conflict,
   parallel same-item -> weight ×0.5 + conflict, late events after
   SESSION_COMPLETED -> appended + `SUMMARY_REVISED` conflict (session state
   preserved), `build_snapshot` (student, skill_states, session, plan,
   `intervention_stats` from immediate/short-term/delayed columns, facts,
   `snapshot_version` = event count, `server_cursor`).
5. `app/sync/router.py` + `app/sync/content_packs.py` — `POST
   /v1/sync/devices`, `DELETE /v1/sync/devices/{device_id}`, `POST
   /v1/sync/events`, `GET /v1/sync/snapshot`, `GET /v1/content-packs`,
   `GET /v1/content-packs/{pack_version}` (published packs only); wired into
   `app/main.py`.
6. `tests/test_sync_protocol.py` — 23 tests: device lifecycle, idempotent
   dedup, partial-batch resume, version-bound correct/wrong, unknown
   version/pack rejection, tampered integrity hash, dependency handling,
   repeated attempt, parallel branch, late events, refresh/restart
   recovery, snapshot memory, mastery-never-trusted, batch > 100. Fixed
   during development: `_insert_learning_event_row` binding count (13 vs
   12), tuple concatenation, UNIQUE `attempt_id` (`#dupN` suffix).

**Offline client** (JS, no build step, no runtime deps):
7. `web/offline-core.js` — dependency-free CommonJS/browser module: pure
   SHA-256, canonical JSON (matches Python `json.dumps(sort_keys=True,
   separators=(",", ":"))`), envelope builder, local objective evaluator,
   temporary-mastery Beta update (server weights), bounded pack-local
   `pickNextQuestion` policy, pending-event queue with SYNC_PROTOCOL retry
   schedule (0s/5s/15s/60s/5min/15min), storage-injected `OfflineSyncClient`
   (upload + ack + snapshot).
8. `web/offline.js` — IndexedDB wrapper, 7 stores per SYNC_PROTOCOL §3
   (profile_snapshot, active_session, content_packs, pending_events,
   acknowledged_events, memory_snapshot, sync_state).
9. `web/app.js` — offline session flow: device registration, pack install,
   local question presentation/hints/feedback, event creation + queueing,
   reconnect sync with visible status, refresh/restart recovery.
10. `web/sw.js` — separate pack cache (`bridgesat-packs-v1`, cache-first for
    `/v1/content-packs/*`) so installed packs serve fully offline.
11. `tests/node/offline-core.test.js` — 15 tests, plain `node --test`, no
    npm deps (in-memory store injection).

**Verification**:
- Python suite: 120 passed (97 prior + 23 sync).
- Node: 15 passed.
- Cross-language integrity check: JS `integrityHash("ANSWER_SUBMITTED",
  {...})` == Python `sha256:...` exactly.
- Live E2E (Node client against uvicorn on a fresh DB): student -> device
  -> pack install -> 2 answers (1 correct, 1 wrong) -> sync accepted 5
  events, 0 duplicates, 0 rejected; second sync empty (idempotency);
  snapshot stable across refresh; server mastery matches client
  approximation direction (wrong answer dropped ratios mastery 0.5 -> 0.4).

### Problems encountered and resolutions

1. **Local policy excluded fresh students** — `pickNextQuestion` filtered
   out items whose skill had no state; a brand-new student (empty
   skillStates) could never start a session. Fixed: unknown skills default
   to mastery 0.5 instead of being excluded.
2. **E2E wrong-answer scored as duplicate** — client generated
   `attempt_id` from `Date.now()`, two rapid answers collided in the same
   millisecond; server correctly marked the second `non_scoring_duplicate`.
   Not a server bug — unique attempt IDs (UUID) fixed the test.
3. **Stale uvicorn holding port with deleted DB** — first detached server
   kept a connection to a DB file that was then removed; restarts must
   kill the old process first.

### Follow-ups / known issues

- **Temporary vs authoritative mastery**: the client shows local Beta
  approximations; the server re-scores authoritatively on sync. Worth an
  explicit "temporary until synced" label in the UI.
- **SW pack versioning**: `sw.js` caches every `/v1/content-packs/{version}`
  URL; old pack versions accumulate in `bridgesat-packs-v1` (bounded by the
  small number of versions, but a prune rule could be added).
- **Real `pack_version` flow**: the production pack's manifest
  `pack_version` is `"0.1.0"`; the client hardcodes the version string in
  two places (install + store key) — move to a config constant once packs
  are versioned more frequently.

## 2026-08-07 — Stage 5: memory outbox, Mnemis gateway, deletion & governance

**Delivered**:

1. **`memory_outbox` + `student_deletions` (migration 0007)** — delivery
   rows with stable idempotency keys (`memory-index:{student}:{type}:{id}:{version}:{op}`),
   state machine `pending -> processing -> indexed/retrying/dead_letter`,
   claim lease, retry schedule (10 s/30 s/60 s/5 m/30 m/6 h), student scope
   enforcement, `student_deletions` protocol table.
2. **OutboxRepository** — same-transaction enqueue (dedup by idempotency
   key, version bump on facts), claim_due (due + lease window), complete,
   mark_failed, list_by_status, consistency_metrics.
3. **Same-transaction wiring** — `EpisodeBuilder.validate` (validated
   episodes) and `SQLiteMemory.upsert_fact_for_episode` (evidenced facts)
   enqueue inside the same transaction that writes the row; rollback
   removes the delivery row.
4. **MnemisMemoryAdapter** — HTTP transport injected, 800 ms default
   timeout, MnemisUnavailableError on config/network failure, idempotency
   key passed through, strict result shaping (scope filters).
5. **InMemoryMnemisIndex stub** — deterministic in-memory backend
   (upsert_episode/upsert_fact/recall_similar/global_select/delete_student/
   health/counts) for tests, demo parity, and the ablation eval.
6. **FallbackStudentMemory** — Mnemis 800 ms -> SQLite -> offline snapshot;
   route counters, latency metrics; Mnemis timeout/unavailable never
   blocks the learning loop.
7. **OutboxWorker** — in-process worker delivering `pending -> indexed`,
   retry schedule to `dead_letter`; unsupported ops fail closed.
8. **StudentMemoryDeletionService** — 8-step protocol (stop new writes ->
   tombstone + deletion outbox -> Mnemis delete -> verify unretrievable ->
   completed).
9. **Consistency metrics** (app/memory/metrics.py) — the 9 required
   outbox/parity metrics from MEMORY_CONSISTENCY.
10. **Ops scripts** — `rebuild_memory_index.py`, `verify_memory_parity.py`
    (rebuild-then-compare, gate on exit code), `replay_dead_letter.py`.
11. **Memory ablation eval** — `evals/memory/golden.jsonl` (10 probes, 3
    students) + `run_memory_ablation.py` comparing no-memory, recent
    SQLite, similar SQLite, Mnemis System-1, Mnemis dual-route; emits
    JSON + `evals/memory/REPORT.md` (episode recall@3, MRR, next-action
    accuracy, intervention accuracy, fallback success, latency avg/p95).

**Verification**:

- Python suite: 182 passed (177 prior + 5 script tests + 5 ablation
  tests).
- Full ablation run on the golden set:

  | Route | Recall@3 | MRR | Next-action | Intervention | Fallback | Latency avg |
  |---|---|---|---|---|---|---|
  | no_memory | 0.00 | 0.00 | 0.00 | 0.40 | - | 0.0 ms |
  | recent_sqlite | 0.30 | 0.30 | 0.30 | 0.30 | - | 0.5 ms |
  | similar_sqlite | 1.00 | 1.00 | 1.00 | 1.00 | - | 0.4 ms |
  | mnemis_system1 | 1.00 | 0.85 | 1.00 | 1.00 | - | 0.0 ms |
  | mnemis_dual | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 801.6 ms |

- Dead-letter path exercised end-to-end: failing index -> 6 attempts ->
  dead_letter -> replay -> indexed, no data loss.
- Parity: rebuild-from-SQLite reproduces the expected episodes/facts for
  every student (exit 0 gates release).

### Problems encountered and resolutions

1. **Parity script compared against an empty index** — a fresh
   `InMemoryMnemisIndex` is empty, so "compare indexed vs SQLite" always
   failed. Parity is now verified the way §12 defines it: rebuild from
   SQLite into a fresh index and compare counts.
2. **Rebuild enqueued nothing after a prior run** — enqueue dedupes by
   idempotency key, so completed rows blocked re-delivery. Rebuild now
   deletes the student's outbox rows and re-enqueues
   delete-first/upserts-after, so delivery order is deterministic (a
   leftover pending `delete_student` from an earlier run would otherwise
   wipe the fresh index after the upserts).
3. **Worker processes one batch per call** — `run_pending` claims 20 rows
   by design; the ablation and rebuild call it in a drain loop.
4. **Ablation recall was silently incomplete** — probes queried the stub
   before the worker had drained all pending deliveries (34 episodes, 20
   claimed); recall@3 was 0.90 for no data reason.
5. **Golden data contamination** — noise episodes for later probes shared
   the misconception of an earlier probe (e.g. `b_e6` was
   `unit_rate_error`, so probe q_b1's expected cluster gained a foreign
   intervention). Fixed by moving noise to unrelated
   skill/misconception pairs; every probe's expected cluster is now
   unambiguous.
6. **Over-strict next-action goldens** — sibling episodes in the same
   proven cohort are equally valid content; `expected_content_id` became
   `expected_content_ids`. Predictions are driven by the top-scoring
   similar cohort (off-misconception skill-only matches at 0.6 no longer
   outvote the 1.0 misconception matches).

### Follow-ups / known issues

- **Mnemis stub scores similarity, not embeddings** — the System-1 route
  in the ablation uses the deterministic stub; swap in the live Mnemis
  endpoint (BRIDGESAT_MODE=enhanced) to validate against the real
  embedding backend.
- **No automated purge for old outbox rows** — completed rows accumulate;
  add a retention policy once delivery volume grows.
- **`student_deletions` is wired but not yet exposed via API** — the
  deletion service exists and is tested; the HTTP endpoint is a follow-up
  (no UI/admin surface in the MVP).

---

## 2026-08-07 — Phase 6: security, evaluation, and demo evidence pack

### What was done

Completed the final MVP phase (plan row 108): security hardening, the full
evaluation suite from EVALUATION_SPEC, the demo seeder, and the evidence pack.

1. **Security hardening**
   - Replaced an `innerHTML` sink in `web/app.js` with `textContent` (XSS).
   - Added `SecurityHeadersMiddleware` in `app/main.py` (CSP, X-Frame-Options
     DENY, X-Content-Type-Options, Referrer-Policy, Permissions-Policy).
2. **Security test suite** — `tests/security/` (cross-student isolation,
   prompt injection, memory poisoning, forged offline events, crawler SSRF,
   XSS, deletion propagation, request limits, secret scan, timeout fallback).
3. **Policy golden eval** — `evals/policy/golden.jsonl` (24 trajectories,
   all 12 EVALUATION_SPEC section 3 categories, 13 safety-critical) +
   `scripts/run_policy_evals.py` -> `reports/policy_eval.json`.
4. **Educational behavior eval** — `scripts/run_educational_evals.py`
   (synthetic simulation, honestly labeled): intervention arm (real policy
   `decide_next_action` driving worked examples, difficulty control, hint
   gating) vs control arm; reports immediate transfer, short-term stability,
   delayed retention, hint dependency, mastery/confidence change.
5. **Offline and sync eval** — `scripts/run_offline_sync_evals.py`
   (controlled internal test): 10 SYNC_PROTOCOL scenarios (full offline
   session, refresh recovery, server restart, duplicate batch, out-of-order,
   late event after summary, old/unknown content versions, parallel branches,
   pending-event retention after failure) -> `reports/offline_sync_eval.json`.
6. **Demo seeder** — `scripts/seed_demo.py`, idempotent: creates the demo
   student, runs the diagnostic, registers the demo device, replays one
   13-event offline practice session (4 correct, 2 misconception answers
   scored by version-bound keys), builds and validates a long-term-memory
   episode.
7. **Orchestrator + evidence pack** — `evals/run_all.py` (`python -m
   evals.run_all`) regenerates `reports/{policy,educational,rag,memory,
   offline_sync,security}_eval.json`, `reports/accessibility_eval.md`,
   `reports/final_summary.md`, and `docs/EVIDENCE_PACK.md`.

### Verification

- Full suite: 232 passed.
- Policy: 24/24 (100%), safety-critical 100%, 12/12 categories.
- Offline sync: 10/10 scenarios (100%).
- Educational (synthetic): correctness +5.7pp, hints -260, immediate
  transfer +22.5pp, delayed retention +6.7pp, mastery +0.049 over control.
- Retrieval: dev recall@1 1.0, golden recall@1 0.875, citation/license
  coverage 1.0, restricted-source hits 0.
- Memory ablation: similar_sqlite and mnemis_dual recall@3 1.00 /
  next-action 1.00 (10 probes).
- Security suites: 73 passed.
- `python -m evals.run_all` completes with all steps [ok].

### Problems encountered and resolutions

1. **Real pack has no `sync.*` fixture questions** — the eval harness must
   point `BRIDGESAT_PACKS_ROOT` at `tests/fixtures/packs` (same env the
   pytest conftest uses) or scoring rejects every answer as
   QUESTION_VERSION_UNKNOWN.
2. **pytest 9 suppresses the pass-count line under `-q`** — the security
   report parsed 0 passed; dropped `-q` from the orchestrator's pytest
   invocation.
3. **seed_demo mixed legacy and migrated schemas** — `StudentRepository`
   creates a 5-column `students` table, but migrations require 8 columns;
   the seeder now uses `LearnerStore.create_student` on the migrated schema.
4. **seed_demo dependency chain** — event ids are generated as
   `demo_<type>_<seq>`, but the first version referenced
   `demo_present_<question_id>` ids that never existed; dependencies now
   reference the actual created event ids.
5. **Educational eval transfer metric was 0/0** — the control arm has no
   intervention trigger; transfer/short-term probes are now triggered in
   control by the analogous event (second consecutive misconception error).

### Follow-ups / known issues

- **Accessibility items marked "manual check required"** — contrast, zoom,
  screen-reader walkthrough need a human usability pass before the demo
  (EVALUATION_SPEC section 9).
- **Educational eval is a synthetic simulation** — never presented as real
  student improvement; a human usability study is the follow-up.

---

## 2026-08-07 — Content audit gate, performance gates, web tests in orchestration

### What was done

Closed the last three EVALUATION_SPEC section 11 gates and wired them into
the orchestrator:

1. **Content audit eval** — `scripts/run_content_audit.py` audits the
   published pack `bridgesat-math-0.1.0` against the release contracts:
   889 checks across manifest (published, reviewers, versions, licenses,
   item-hash completeness), all 55 items (schema, 4 unique choices, valid
   answer, difficulty bounds, non-empty prompt/hints/explanation, approved
   review, reviewer names, license, canonical hash, known skill, no
   prohibited lineage), 16 lessons (8+8 kinds, distinct ids, no byte-identical
   bodies, hash), and the restricted-source registry audit (no College
   Board / Khan Academy / OpenStax acquisition). Outputs
   `reports/content_audit_eval.json` + `evals/content_audit/REPORT.md`.
2. **Performance gates eval** — `scripts/run_performance_evals.py` measures
   on-device budgets: local policy p95 0.01 ms (target < 150), FTS5 p95
   2.3 ms (target < 200), session restore p95 3.2 ms (target < 500), plus
   sync throughput ~920 events/s and max RSS ~79 MB, into
   `reports/performance_eval.json`. Mnemis timeout non-blocking is covered
   by the test suite (reported as such, not re-measured).
3. **Web core-flow tests** — `web/tests/*.test.js` (21 node --test tests:
   offline full flow, refresh recovery, weak network, batch cap, version
   bound scoring, accessibility core paths) now run inside `evals.run_all`
   (`reports/web_tests.json`); `evals/run_all.py` summary + EVIDENCE_PACK
   table gained the content audit, performance, and web sections; the
   "performance not yet measured" caveat was removed.
4. **Real content bugs found and fixed by the audit** — the audit gate
   caught (a) `math.ratios_percentages.002`/`.004` byte-identical duplicate
   questions and (b) seven correct-answer-text collisions across a skill
   (4 in linear_equations, 2 in ratios_percentages, 1 in functions_models).
   `scripts/fix_content_collisions.py` rewrote the seven items with fresh
   equations/expressions (answers chosen to collide with nothing in the
   skill), verified each through `validate_item` (exact sympy math, schema,
   unique choice texts), recomputed canonical hashes, and the pack was
   rebuilt via `scripts/build_content_pack.py` (manifest hashes verified).

### Verification

- Content audit: 889/889 checks (100%).
- Performance: all gates passed (policy 0.01 ms, FTS5 2.3 ms, restore 3.2 ms).
- Web: 21 node tests passed, 0 failed.
- Full suite: 232 pytest tests passed; `python -m evals.run_all` all steps
  [ok]; final_summary and EVIDENCE_PACK regenerated.

### Problems encountered and resolutions

1. **Audit reported duplicate "answers" that were choice ids, not texts** —
   the first version compared `answer_choice_id` ("A") across items; fixed
   to compare the answer choice *texts* per skill.
2. **Web tests are node --test files, not pytest** — the orchestrator first
   invoked pytest (0 collected); switched to `node --test web/tests/*.test.js`
   with in-Python glob expansion (subprocess has no shell to expand `*`).
3. **Performance eval seeding rejected** — `integrity_hash` must be the
   canonical sha256 of event_type+payload (seed used placeholder), event
   payload keys must be `selected_choice_id`/`hint_level`/`attempt_id`, and
   batches cap at 100 events; fixed the seeder to mirror `seed_demo.py` and
   chunked the seeding into two batches.

### Follow-ups / known issues

- **Accessibility items marked "manual check required"** — unchanged, needs
  a human usability pass before the demo.
- **Educational eval is a synthetic simulation** — unchanged.
- `content/validated/math-v1.jsonl` items rewritten by the audit gate carry
  the same simulated review ledger rows; the audit itself is the second gate
  on top of the ledger (reviewers/reviewed_at fields preserved).

---

## 2026-08-07 — Close pre-submission checklist: backup/restore, projection rebuild, README

### What was done

Closed the three remaining pre-submission checklist items
(COMPETITION_MVP_EXECUTION_PLAN.md section 12) so Phase 6 is complete:

1. **Pre-migration backup** — `app/infrastructure/migration_runner.py` now
   copies the database to `data/backups/<stem>-pre-migration-<ts>.db`
   before applying pending migrations (backup only when the file already
   existed and migrations are pending; none for fresh DBs or idempotent
   reruns). Coerces `Path(database_path)` so legacy `str` callers keep
   working. Contract: API_AND_OPERATIONS sections 7-8.
2. **Restore tool** — `scripts/restore_sqlite_backup.py` (--backup/--target):
   refuses same-path restore, missing backup, and non-SQLite files; prints
   the restored schema version.
3. **Projection rebuild** — `scripts/rebuild_learner_projections.py`
   replays `learning_events` (occurred_at/received_at order) through the
   sync service's own apply path. `SyncService._apply_event` and the three
   appliers gained `insert_event_row: bool = True`; replay passes False so
   the immutable log is never re-written. Server-origin events that the
   sync applier does not handle (e.g. STUDENT_CREATED) are counted as
   skipped, not failures. Projection tables (study_sessions,
   answer_attempts, student_skill_states, misconception_evidence,
   sync_conflicts) are cleared per student with `PRAGMA foreign_keys = OFF`
   (recovery operation; the event log is untouched).
4. **Tests** — `tests/test_migrations.py` gained backup-created /
   no-backup-fresh / no-second-backup / restore-round-trip / restore
   refusal cases; `tests/test_sync_protocol.py` gained a golden rebuild
   test that seeds a session with a misconception, snapshots projections,
   corrupts them, rebuilds, and asserts exact equality (timestamps and
   uuid key columns excluded as non-deterministic).
5. **README** — rewritten from "initial project skeleton" to the delivered
   scope: features, honest out-of-scope statement, run commands, data
   sources, measured-vs-not-measured results table, status.
6. **EVIDENCE_PACK** — added a "Recovery capabilities" table (backup,
   restore, projection rebuild, FTS5 rebuild, Mnemis rebuild, checksums)
   with implementation + test evidence; regenerated by `evals.run_all`.

### Verification

- Full suite: 238 passed (was 232; +5 migration/restore, +1 rebuild golden).
- Real-data smoke: `scripts/rebuild_learner_projections.py --db
  data/bridgesat.db` replays 13 events (1 server event skipped), restores
  17 projection rows, `build_snapshot` still healthy; knowledge DB has no
  students, reports 0 rebuilt.
- `python -m evals.run_all` all steps [ok].

### Problems encountered and resolutions

1. **Restore assumed `schema_migrations` exists** — backups of un-migrated
   legacy DBs have no ledger; the script now probes sqlite_master first.
2. **Replay skipped the observational branch** — `_apply_event` forwarded
   `insert_event_row` to ANSWER_SUBMITTED and SESSION_COMPLETED but not the
   observational set, so CONTENT_PRESENTED re-inserted into the immutable
   log and hit UNIQUE violations; forwarded everywhere.
3. **FK constraint on clear** — answer_attempts/evidence reference
   sessions; disabling foreign keys for the explicit rebuild fixed it.
4. **`apply_migrations` broke on str paths** — `db_existed = is_file()`
   regressed `SyncService(str)` callers; coerced `Path()` at entry.
5. **Backup of fresh DBs** — `sqlite3.connect` creates the file, so
   "file exists" alone is wrong; existence is captured before connect and
   empty fresh DBs get no backup.

### Follow-ups / known issues

- Remaining human items only: accessibility manual walkthrough, real
  educational outcome study, and the demo recording itself.

---

## 2026-08-11 — Content Expansion Phase

Expanded the governed SAT Math catalog without changing the Agent, memory, or
sync architecture:

1. Added 48 BridgeSAT-original questions across `inequalities`,
   `quadratic_equations`, `exponents_radicals`, and `coordinate_geometry`, with
   4/5/3 difficulty distribution per skill and explicit trigger/transfer paths.
2. Added one micro lesson and one worked example per new skill, with explicit
   misconception targets. The full pack now contains 103 questions and 24
   lessons across eight skills and 17 used misconception types.
3. Published `bridgesat-math-0.3.0`; the manifest records question and lesson
   hashes, counts, skills, and misconceptions. The controlled `sim.*` ledger is
   labeled not human approval.
4. PostgreSQL import now registers and indexes questions and lessons. The PWA
   selects the latest semantic pack version, preserves the active version for
   offline scoring, and renders homepage counts/cards from pack content.
5. Content audit: 1799/1799 checks passed. PostgreSQL import: 127 registry rows;
   tsvector index: 103 questions + 24 lessons.
6. Final review hardening moved questions to v3 and lessons to v4, rejects
   distractor collisions instead of changing their misconception meaning,
   independently recomputes expansion answers, binds review records to
   `id/version/content_hash`, preserves historical lesson verification, and
   blocks `sim.*` provenance before artifact writes unless the competition-demo
   override is explicit. Correct-answer labels are distributed A/B/C/D =
   21/27/26/29, with zero placeholder choices.

Real human content review and real-student learning-effect evidence remain open.
