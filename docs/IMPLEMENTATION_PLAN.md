# BridgeSAT Competition Implementation Plan

Normative implementation contracts are defined in:

- `PEDAGOGY_SPEC.md`;
- `MEMORY_CONSISTENCY.md`;
- `SYNC_PROTOCOL.md`;
- `THREAT_MODEL.md`;
- `API_AND_OPERATIONS.md`;
- `EVALUATION_SPEC.md`.

When this plan conflicts with one of those specifications, the narrower normative specification takes precedence.

## 1. Status and objective

- Plan version: 0.3.0
- Prepared: 2026-08-06
- Target: AceSAT Education AI-Agent competition submission
- Architecture style: offline-first modular monolith
- Primary product proof: a later session recalls an earlier learning episode and changes the teaching action for an explainable reason

This plan converts `ARCHITECTURE.md` into an implementation sequence with frozen technology choices, module contracts, acceptance gates, and a competition delivery schedule.

---

## 2. Final product proposition

BridgeSAT is an adaptive SAT learning agent for students with limited tutoring access, older devices, short study windows, or unstable internet.

It performs a closed learning loop:

```text
diagnose
  -> plan
  -> teach
  -> observe an attempt
  -> identify the misconception
  -> recall relevant learner memory
  -> retrieve approved teaching content
  -> select and explain the next action
  -> record the outcome
  -> continue offline when necessary
```

The project is not a chatbot, a generic document-QA application, or a collection of unrelated RAG frameworks.

---

## 3. Frozen technical stack

### 3.1 Required for submission

| Layer | Technology | Reason |
|---|---|---|
| API | FastAPI | Existing project foundation and rapid iteration |
| Authoritative data | SQLite with migrations | Local-first, reproducible, restart-safe |
| Student client | Mobile-first PWA | Weak-network and offline requirement |
| Offline data | IndexedDB | Session, content pack, memory snapshot, pending events |
| Core knowledge retrieval | metadata + SQLite FTS5 | Fast, deterministic, easy to package offline |
| Knowledge structure | reviewed skill/subskill/prerequisite graph | Education-specific hierarchy and explainability |
| Core memory | event log + episodes + aggregates | Reliable cross-session behavior |
| Advanced memory | Mnemis adapter | Similar and global long-term recall |
| Evaluation | golden trajectories and retrieval/memory ablations | Proves Agent behavior rather than only UI quality |

### 3.2 Conditional enhancements

| Technology | Use only when | Exit condition |
|---|---|---|
| LightRAG | online relationship retrieval improves golden RAG results | remove from demo path if setup, latency, or accuracy is worse than local hybrid retrieval |
| A-RAG-style tools | complex planning benefits from iterative retrieval | disable if it exceeds step/token budget or does not improve plan correctness |
| RAG-Anything | approved multimodal documents are actually available | omit if all competition content is structured JSON/Markdown |
| Embedding reranker | Recall@3 or intervention-content match improves | retain FTS5-only path if gain is negligible |

### 3.3 Explicitly excluded from the MVP

- multi-agent orchestration;
- GraphRAG as a second knowledge platform;
- HippoRAG alongside Mnemis;
- live unrestricted crawling;
- LLM-generated questions published without review;
- cloud-only answer evaluation;
- a full teacher administration dashboard;
- a graph database as the authoritative student record.

---

## 4. Three-plane architecture

```text
┌──────────────────────────────────────────────────────────┐
│ Plane A: Offline deterministic learning core             │
│ session state, answer checking, mastery, hints, policy,  │
│ local FTS5, prerequisite graph, IndexedDB event queue    │
└──────────────────────────┬───────────────────────────────┘
                           │ optional online context
┌──────────────────────────▼───────────────────────────────┐
│ Plane B: Governed educational knowledge                  │
│ reviewed content, citations, license records, local RAG, │
│ optional LightRAG, bounded A-RAG-style retrieval         │
└──────────────────────────┬───────────────────────────────┘
                           │ learner-specific evidence
┌──────────────────────────▼───────────────────────────────┐
│ Plane C: Cross-session learner memory                    │
│ events, episodes, semantic facts, strategy statistics,   │
│ Mnemis System-1/System-2, SQLite fallback                │
└──────────────────────────────────────────────────────────┘
```

No optional online component may prevent Plane A from completing a normal learning session.

---

## 5. Target source tree

```text
app/
├── api/
│   ├── students.py
│   ├── diagnostics.py
│   ├── sessions.py
│   ├── content.py
│   ├── memory.py
│   ├── sync.py
│   └── reports.py
├── agent/
│   ├── orchestrator.py
│   ├── policy.py
│   ├── planner.py
│   ├── question_selector.py
│   ├── misconception.py
│   └── explanations.py
├── domain/
│   ├── events.py
│   ├── sessions.py
│   ├── learner.py
│   ├── content.py
│   ├── memory.py
│   └── sync.py
├── knowledge/
│   ├── interfaces.py
│   ├── local_backend.py
│   ├── lightrag_backend.py
│   ├── router.py
│   ├── hierarchy.py
│   └── citations.py
├── memory/
│   ├── interfaces.py
│   ├── sqlite_backend.py
│   ├── mnemis_backend.py
│   ├── fallback_backend.py
│   ├── episode_builder.py
│   └── consolidation.py
├── ingestion/
│   ├── sources.py
│   ├── license_gate.py
│   ├── crawler.py
│   ├── importers.py
│   ├── validators.py
│   └── pack_builder.py
├── infrastructure/
│   ├── database.py
│   ├── migrations.py
│   ├── settings.py
│   └── telemetry.py
└── main.py

web/
├── app.js
├── offline/
│   ├── db.js
│   ├── policy.js
│   ├── evaluator.js
│   └── sync.js
└── sw.js

content/
├── taxonomy/
├── questions/
├── lessons/
├── sources/
└── packs/

evals/
├── policy/
├── memory/
├── retrieval/
├── offline/
└── reports/
```

---

## 6. Authoritative event model

Every state-changing interaction is written as an immutable event before derived state is updated.

Minimum event types:

```text
STUDENT_CREATED
DIAGNOSTIC_STARTED
ANSWER_SUBMITTED
ANSWER_EVALUATED
MISCONCEPTION_IDENTIFIED
HINT_REQUESTED
INTERVENTION_SELECTED
CONTENT_PRESENTED
PLAN_ADAPTED
SESSION_COMPLETED
EPISODE_CREATED
MEMORY_INDEXED
OFFLINE_EVENT_QUEUED
OFFLINE_EVENT_SYNCED
```

Required event fields:

```json
{
  "event_id": "evt_...",
  "student_id": "stu_...",
  "session_id": "ses_...",
  "event_type": "INTERVENTION_SELECTED",
  "payload": {},
  "occurred_at": "ISO-8601",
  "device_id": "device_...",
  "origin": "online|offline",
  "policy_version": "policy-0.3.0",
  "content_version": "sat-pack-0.1.0"
}
```

Idempotency is enforced by `event_id`.

---

## 7. Session orchestrator contract

### Input

```text
current session state
latest event
learner skill state
working memory
approved content availability
compact long-term memory evidence
remaining time
network state
```

### Output

```text
bounded next action
target skill and difficulty
selected content ID
reason code
student-facing explanation
evidence references
updated session state
```

### Bounded actions

```text
ASK_QUESTION
GIVE_HINT
SHOW_MICRO_LESSON
SHOW_WORKED_EXAMPLE
RETRY_SAME_SKILL
LOWER_DIFFICULTY
RAISE_DIFFICULTY
SWITCH_TO_PREREQUISITE
SCHEDULE_REVIEW
END_SESSION
```

The language model may phrase the explanation but cannot invent new executable actions.

---

## 8. Knowledge retrieval plan

### 8.1 Local baseline

The baseline query is structured:

```json
{
  "skill": "linear_equations",
  "subskill": "isolating_variables",
  "misconception": "sign_error",
  "difficulty": 1,
  "content_types": ["worked_example", "micro_lesson"],
  "offline_required": true
}
```

Retrieval sequence:

```text
license and audience filter
  -> metadata match
  -> FTS5 query
  -> one-hop prerequisite expansion
  -> weighted rank
  -> citation validator
```

Suggested ranking features:

```text
skill match                 4.0
misconception match         3.0
difficulty match            2.0
content-type match          2.0
prerequisite relevance      1.5
offline availability        1.0
recently shown penalty     -2.0
```

Weights are versioned and tuned using the golden retrieval set.

### 8.2 Hierarchy design

The reviewed graph contains:

```text
domain
  -> skill
    -> subskill
      -> misconception

skill REQUIRES prerequisite_skill
lesson REMEDIATES misconception
question TESTS subskill
worked_example DEMONSTRATES subskill
```

This applies the useful hierarchy-aware principle from HiRAG without introducing a separate runtime service.

### 8.3 LightRAG adapter

`LightRAGKnowledgeBackend` receives only approved content and returns IDs from the authoritative content registry.

It is called when:

- local retrieval confidence is below threshold;
- the query spans more than one skill or prerequisite;
- an online worked-example relationship query is requested.

It is skipped when:

- offline;
- a direct lesson/question link exists;
- the local top result exceeds the confidence threshold;
- the latency budget is nearly exhausted.

### 8.4 Complex retrieval tools

For complex planning only, expose bounded tools:

```text
keyword_search(query, filters, top_k)
semantic_search(query, filters, top_k)
chunk_read(content_id, section_id)
```

Budgets:

- maximum four retrieval actions;
- maximum two semantic searches;
- maximum six returned chunks before reranking;
- mandatory source/license validation;
- deterministic fallback to the local planner.

---

## 9. Mnemis memory plan

### 9.1 What is indexed

Do not index every click. Index validated learning episodes containing:

```text
context
skill and subskill
misconception
intervention
immediate outcome
later retention outcome when available
supporting event IDs
```

### 9.2 System-1 route

Use for:

- repeated misconception;
- similar past episode;
- intervention evidence lookup.

The result must include episode IDs and relevance scores.

### 9.3 System-2 route

Use for:

- session-start planning when several skills are weak;
- weekly plan generation;
- persistent multi-skill stagnation;
- prerequisite root-cause analysis.

System-2 is never required for answering or submitting an objective question.

### 9.4 Fallback

```text
Mnemis call
  -> strict timeout or error
  -> SQLite episode search and strategy aggregates
  -> local client snapshot if offline
```

### 9.5 Memory effect proof

The demo must contain two sessions:

1. A repeated sign error leads to a worked example and the next attempt improves.
2. In a later session, memory recalls the successful intervention and selects it earlier.

The UI shows the recalled episode and the reason the action changed.

---

## 10. Data and ingestion plan

### 10.1 Content target

- 8–10 skills;
- 2–4 subskills per skill;
- 80–120 reviewed questions;
- 15–25 micro-lessons and worked examples;
- three hints per question;
- explicit distractor-to-misconception mapping where possible.

### 10.2 Import policy

Each source record must state:

```text
license
allowed access method
RAG-ingestion permission
redistribution permission
attribution
review status
content hash
```

No source with unclear rights enters the knowledge index.

### 10.3 Crawler scope

The crawler is only a controlled fetcher for pre-approved sources. It must enforce robots rules, rate limits, redirect validation, private-network blocking, response limits, hashing, and provenance retention.

Live crawling is excluded from the student request path.

---

## 11. Offline implementation

IndexedDB stores:

```text
student snapshot
active session
installed content pack
recent strategy snapshot
pending events
sync cursor
```

Offline supports:

- rendering cached questions;
- checking objective answers;
- updating temporary mastery;
- three-level hints;
- precomputed local content lookup;
- bounded local adaptation policy;
- event creation and queueing.

Reconnect supports:

- event-batch upload;
- event-ID deduplication;
- server-derived state rebuild;
- new learner/memory snapshot download;
- visible synchronization status.

---

## 12. API milestone surface

```text
POST /v1/students
POST /v1/diagnostics/start
POST /v1/diagnostics/{id}/answers
POST /v1/sessions
GET  /v1/sessions/{id}
POST /v1/sessions/{id}/attempts
POST /v1/sessions/{id}/hints
POST /v1/sessions/{id}/complete
POST /v1/knowledge/retrieve
GET  /v1/students/{id}/memory-summary
POST /v1/sync/events
GET  /v1/content-packs/{version}
GET  /v1/reports/{student_id}
```

Internal adapters are not exposed as unrestricted public endpoints.

---

## 13. Evaluation plan

### 13.1 Policy evaluation

At least 20 golden trajectories covering:

- difficulty increase and decrease;
- repeated misconception intervention;
- hint dependency;
- prerequisite switching;
- time-budget closure;
- content unavailable fallback;
- offline continuation;
- memory-aware action change.

### 13.2 Retrieval evaluation

Compare:

```text
metadata only
FTS5
FTS5 + hierarchy
FTS5 + hierarchy + embeddings
optional LightRAG
```

Metrics:

- Recall@1 and Recall@3;
- misconception-content match;
- prerequisite-root match;
- citation and license coverage;
- latency;
- retrieved context size.

### 13.3 Memory evaluation

Compare:

```text
no memory
recent episodes only
vector/similarity route
Mnemis dual route
```

Metrics:

- relevant episode Recall@5;
- cross-session fact coverage;
- root-cause identification;
- intervention-selection accuracy;
- next-action accuracy;
- latency and fallback success.

### 13.4 Offline and recovery evaluation

- complete one full session with network disabled;
- refresh and resume the active session;
- upload duplicate events without duplicate scoring;
- restart the API and recover state;
- synchronize after content-pack version change.

### 13.5 Minimum acceptance targets

These are engineering targets, not claimed results until measured:

| Gate | Target |
|---|---:|
| Golden policy trajectories | at least 90% pass |
| Critical memory scenarios | 100% pass |
| Offline core-flow completion | 100% |
| Duplicate-sync protection | 100% |
| Decision explanation coverage | 100% |
| Citation/license coverage for retrieved content | 100% |
| Local action p95 | below 300 ms on development machine |
| Clean-start demo | one documented command sequence |

---

## 14. Delivery schedule

### August 6 — freeze and foundations

- freeze this plan and taxonomy draft;
- introduce database migrations;
- create event, session, episode, and content-registry models;
- create backend interfaces.

### August 7 — complete online learning loop

- session orchestrator;
- attempt processing;
- misconception mapping;
- question selection;
- hints and intervention actions.

### August 8 — reliable memory baseline

- SQLite episodic memory;
- episode builder;
- strategy-effectiveness aggregate;
- second-session memory-aware policy;
- two-session golden test.

### August 9 — knowledge retrieval

- content registry;
- skill hierarchy;
- FTS5 retrieval;
- citation and license validator;
- retrieval golden set.

### August 10 — Mnemis prototype

- pin dependencies;
- map learning episodes to base/hierarchical graph objects;
- implement System-1 and Global Selection adapter;
- add timeout, cache, and SQLite fallback.

### August 11 — offline proof

- IndexedDB schema;
- local answer evaluator and policy;
- event queue and sync API;
- service-worker content pack caching.

### August 12 — content and optional enhancement gate

- expand reviewed content;
- run FTS5/hierarchy baseline;
- test LightRAG only if the baseline is stable;
- use RAG-Anything only for approved multimodal sources.

### August 13 — evaluation and accessibility

- policy, memory, retrieval, offline, and recovery evaluations;
- keyboard, contrast, labels, touch-target, and page-weight checks;
- weak-network test.

### August 14 — submission assets

- final README;
- architecture diagram;
- screenshots;
- one-page description;
- three-minute video recording;
- public deployment verification.

### August 15 — freeze and submit

- clean-environment install;
- rerun all critical tests;
- verify demo/video/repository links;
- no architecture expansion;
- submit with buffer before the official deadline.

---

## 15. Go/no-go gates for optional technology

### Mnemis

Go when:

- two-session SQLite memory is already working;
- official code can run in the available environment;
- evidence IDs can be returned;
- fallback is verified.

No-go for the live demo path when:

- setup is not reproducible;
- global selection latency breaks the demo;
- results cannot be traced to episodes;
- it does not improve controlled memory cases.

### LightRAG

Go when:

- approved corpus and local baseline are complete;
- it improves hierarchical retrieval on the golden set;
- a stable pinned release runs reliably.

No-go when:

- the corpus is too small to show benefit;
- indexing or deployment consumes submission-critical time;
- local FTS5 + hierarchy is equally accurate.

### A-RAG-style retrieval

Go only for complex planning queries that improve with iterative retrieval. Keep it disabled for ordinary practice.

### RAG-Anything

Go only when a licensed multimodal source is included and the parsed output can be reviewed before indexing.

---

## 16. Main risks and controls

| Risk | Control |
|---|---|
| Technology stacking hides the product | demo one learner story and one clear decision loop |
| Mnemis research code is incomplete | authoritative SQLite memory and adapter boundary |
| LightRAG setup consumes remaining time | local retrieval is the submission baseline |
| Incorrect or restricted educational content | source registry, license gate, human review |
| Student memory creates fixed labels | confidence, contradiction, decay, neutral language |
| Offline sync duplicates mastery updates | immutable event IDs and idempotent projection |
| LLM output changes state incorrectly | schema validation and deterministic policy ownership |
| Evaluation appears synthetic | disclose fixture construction and report real measured outputs only |

---

## 17. Definition of competition-ready

BridgeSAT is ready when all of the following are visible in one reliable flow:

1. The learner completes a short diagnostic.
2. The agent creates a time-bounded plan.
3. A repeated misconception changes the teaching strategy.
4. Approved knowledge retrieval supplies a targeted intervention with provenance.
5. The intervention and outcome form a learning episode.
6. A later session recalls the episode and changes the next action.
7. The UI explains the memory-based decision.
8. The session continues with the network disabled.
9. Reconnection synchronizes each event exactly once.
10. Golden evaluations and an honest result report are available.

The next implementation task is the event-driven session and two-session SQLite memory loop. Mnemis integration begins only after that proof passes.
