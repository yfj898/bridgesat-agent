# BridgeSAT Complete Architecture

## 1. Document status

- Project: BridgeSAT Agent
- Architecture version: 0.2.0-draft
- Target: AceSAT Education AI-Agent competition MVP
- Primary client: mobile browser / installable PWA
- Primary deployment: single-node FastAPI application with SQLite
- Core principle: the learning loop must continue when the network, LLM provider, vector service, graph database, or Mnemis integration is unavailable

This document is the architecture baseline for implementation, testing, deployment, and the competition demonstration. Any major feature should map to a component, event, state transition, evaluation scenario, and failure fallback defined here.

Normative companion specifications:

- `PEDAGOGY_SPEC.md`: curriculum, item, mastery, misconception, intervention, and fairness contracts;
- `MEMORY_CONSISTENCY.md`: SQLite authority, episode formation, outbox, Mnemis consistency, correction, and deletion;
- `SYNC_PROTOCOL.md`: offline event envelope, idempotency, ordering, conflicts, snapshots, and retries;
- `THREAT_MODEL.md`: privacy, prompt injection, memory poisoning, web security, crawler, and model-provider threats;
- `API_AND_OPERATIONS.md`: endpoint conventions, authentication, migration, deployment, performance, and accessibility;
- `EVALUATION_SPEC.md`: policy, educational, RAG, memory, offline, fairness, security, and performance evaluation.
- `DATA_SOURCE_REGISTRY.md`: source rights decisions, approved uses, review workflow, and withdrawal policy;
- `../config/sources.yaml`: machine-readable fail-closed source and acquisition registry.

---

## 2. Product definition

BridgeSAT is an offline-first adaptive SAT learning agent for students who have limited tutoring access, older devices, short study windows, or unstable internet connections.

The product is not a general-purpose chatbot. It is a bounded learning agent that repeatedly performs the following loop:

```text
observe the learner
  -> update the learner model
  -> recall relevant learning memory
  -> retrieve approved educational content
  -> select the next teaching action
  -> execute the action
  -> observe the outcome
  -> explain and record the decision
  -> consolidate useful long-term memory
```

The minimum competition story is:

```text
first session
  -> diagnose a repeated misconception
  -> apply a targeted intervention
  -> observe improvement
  -> store the learning episode

second session
  -> recall the previous misconception and intervention outcome
  -> choose the previously effective intervention earlier
  -> explain why the plan changed

network interruption
  -> continue practice locally
  -> queue events
  -> reconnect and synchronize without duplicate scoring
```

---

## 3. Architecture goals

### 3.1 Functional goals

BridgeSAT must be able to:

1. create a lightweight learner profile;
2. run an adaptive diagnostic;
3. estimate skill mastery and confidence;
4. identify misconceptions, not only incorrect answers;
5. generate a time-bounded study plan;
6. select the next question and difficulty;
7. provide three levels of hints;
8. retrieve a relevant micro-lesson or worked example;
9. adapt the session after each meaningful event;
10. preserve and recall cross-session learning memory;
11. explain every consequential agent decision;
12. continue the core session offline;
13. synchronize offline events idempotently;
14. show measurable learning and system outcomes.

### 3.2 Non-functional goals

- Mobile-first and usable on older devices.
- Core local interaction should feel immediate.
- No mandatory external model call for answer checking, mastery updates, question selection, persistence, or offline practice.
- All persistent state changes must be traceable to an event.
- All retrieved content must retain source and license metadata.
- All critical decisions must be deterministic or reproducibly policy-driven.
- The system must degrade gracefully when optional services fail.
- The competition demo must run from a clean environment with a short setup path.

### 3.3 Explicit non-goals for the competition MVP

- Full SAT curriculum coverage.
- Automatic ingestion of restricted College Board or Khan Academy content.
- An unbounded web-crawling platform.
- A multi-agent microservice architecture.
- A school administration suite.
- Automated high-stakes score prediction.
- Fully autonomous generation and publication of unreviewed questions.
- Requiring a graph database for the basic learning workflow.

### 3.4 Frozen technology decisions

The competition implementation uses a layered design rather than placing every new RAG framework in the request path.

| Capability | Required implementation | Conditional enhancement | Not in the core path |
|---|---|---|---|
| Operational state | SQLite event store | PostgreSQL after the competition | Graph database as source of truth |
| Offline retrieval | metadata filters + SQLite FTS5 + local skill graph | compact local embedding index if measured useful | cloud-only retrieval |
| Educational hierarchy | reviewed SAT skill/subskill/prerequisite graph inspired by HiRAG | LightRAG adapter for richer online graph retrieval | a second independent HiRAG service |
| Long-term learner memory | validated learning episodes + SQLite aggregates | Mnemis dual-route retrieval | raw chat-history vector search as the only memory |
| Complex retrieval control | deterministic query router | A-RAG-style keyword/semantic/chunk-read tools for planning queries | autonomous iterative retrieval for every question |
| Multimodal ingestion | reviewed JSON/Markdown content import | RAG-Anything batch parsing for licensed PDFs, tables, and formulas | runtime PDF parsing on student devices |
| Answer evaluation | deterministic answer keys and rubric logic | bounded model assistance for free-form explanations | LLM-only grading of objective questions |

The selected architecture therefore has three required planes:

```text
offline deterministic learning core
  + governed educational knowledge retrieval
  + cross-session learner memory
```

New frameworks are accepted only when they improve a named evaluation metric without breaking offline operation or the clean-demo setup path.

---

## 4. System context

```text
┌────────────────────────────────────────────────────────────┐
│                    Student Mobile PWA                      │
│                                                            │
│ Profile | Diagnostic | Plan | Session | Progress | Memory  │
│                                                            │
│ IndexedDB                                                  │
│ - content packs                                            │
│ - active session                                           │
│ - learner snapshot                                         │
│ - pending events                                           │
│ - local policy data                                        │
└─────────────────────────────┬──────────────────────────────┘
                              │ REST / event synchronization
                              ▼
┌────────────────────────────────────────────────────────────┐
│                FastAPI Modular Monolith                    │
│                                                            │
│ Session Orchestrator                                       │
│ Learner Model                                              │
│ Adaptive Policy                                            │
│ Question Selector                                          │
│ Planner                                                    │
│ Misconception Classifier                                   │
│ Layered Memory Router                                      │
│ Content RAG                                                │
│ Synchronization Service                                    │
│ Reporting and Evaluation                                   │
│ Optional LLM Adapter                                       │
└───────────────┬─────────────────────┬──────────────────────┘
                │                     │
                ▼                     ▼
┌────────────────────────┐   ┌──────────────────────────────┐
│ SQLite Operational DB  │   │ Optional Mnemis Backend      │
│                        │   │                              │
│ students               │   │ episodic graph memory       │
│ sessions               │   │ hierarchical memory         │
│ attempts               │   │ similarity recall           │
│ events                 │   │ global memory selection      │
│ memory facts           │   │                              │
│ intervention stats     │   │ Graphiti / graph store       │
│ FTS knowledge index    │   └──────────────────────────────┘
└───────────────┬────────┘
                ▼
┌────────────────────────────────────────────────────────────┐
│             Governed Content Ingestion Pipeline            │
│                                                            │
│ source registry -> license gate -> safe fetch/import       │
│ -> parse -> normalize -> deduplicate -> skill-map           │
│ -> chunk -> review -> index -> versioned offline pack       │
└────────────────────────────────────────────────────────────┘
```

---

## 5. Core architectural decision: modular monolith

BridgeSAT should remain one deployable FastAPI application during the competition.

Reasons:

- the project has a short implementation window;
- a single-node application is easier to demonstrate and recover;
- SQLite is sufficient for the expected demo workload;
- offline capability matters more than horizontal scale;
- module boundaries can be preserved without network boundaries;
- optional Mnemis and embedding services can still be accessed behind adapters.

The architecture should be modular in code, but not split into independently deployed services unless a later production requirement justifies it.

Recommended package structure:

```text
bridgesat-agent/
├── app/
│   ├── api/
│   ├── agent/
│   ├── memory/
│   ├── knowledge/
│   ├── ingestion/
│   ├── domain/
│   ├── infrastructure/
│   ├── schemas/
│   └── main.py
├── web/
│   ├── pages/
│   ├── components/
│   ├── offline/
│   ├── index.html
│   ├── app.js
│   ├── styles.css
│   └── sw.js
├── content/
│   ├── sources/
│   ├── raw/
│   ├── reviewed/
│   ├── packs/
│   ├── lessons/
│   ├── questions/
│   └── schemas/
├── evals/
├── tests/
├── scripts/
├── docs/
└── data/
```

---

## 6. Agent control loop

The Session Orchestrator owns the learning trajectory. It receives events, reads state and memory, calls deterministic domain services, chooses an action, records the reason, and returns the next student-facing step.

```text
student event
  -> validate event and session version
  -> append immutable event
  -> update working state
  -> update learner model
  -> decide whether memory recall is necessary
  -> retrieve approved content if necessary
  -> execute adaptive policy
  -> persist agent decision
  -> return next action
  -> mark episode candidates for consolidation
```

### 6.1 Bounded action space

```text
START_DIAGNOSTIC
ASK_QUESTION
GIVE_HINT_LEVEL_1
GIVE_HINT_LEVEL_2
GIVE_HINT_LEVEL_3
SHOW_MICRO_LESSON
SHOW_WORKED_EXAMPLE
RETRY_SAME_SKILL
LOWER_DIFFICULTY
RAISE_DIFFICULTY
SWITCH_SKILL
SCHEDULE_REVIEW
PAUSE_SESSION
END_WITH_REVIEW
END_SESSION
SYNC_PROGRESS
```

An LLM may phrase an explanation, but it may not invent new executable actions.

### 6.2 Decision priority

1. data integrity and session validity;
2. safety and content availability;
3. remaining time;
4. repeated misconceptions;
5. review deadlines;
6. long-term memory evidence;
7. mastery and confidence;
8. current difficulty;
9. content diversity and repetition avoidance.

---

## 7. Session state machine

```text
NEW
  -> PROFILE_READY
  -> DIAGNOSTIC_ACTIVE
  -> DIAGNOSTIC_COMPLETE
  -> PLAN_READY
  -> SESSION_ACTIVE
       -> QUESTION_ACTIVE
       -> ANSWER_EVALUATED
       -> HINT_ACTIVE
       -> MICRO_LESSON_ACTIVE
       -> WORKED_EXAMPLE_ACTIVE
       -> PRACTICE_ADAPTED
       -> REVIEW_ACTIVE
  -> SESSION_SUMMARY
  -> SESSION_COMPLETED
```

Recovery and degraded states:

```text
SESSION_PAUSED
OFFLINE_ACTIVE
OFFLINE_PENDING_SYNC
SYNC_CONFLICT
CONTENT_PACK_MISSING
MEMORY_BACKEND_DEGRADED
MODEL_BACKEND_DEGRADED
SESSION_EXPIRED
```

Every transition must create an agent event containing the event ID, student ID, session ID, previous and next state, action, reason code, human-readable explanation, policy version, content version, memory evidence references, timestamp, and online/offline origin.

---

## 8. Layered memory architecture

Memory is a first-class subsystem. It is not equivalent to chat history and it must directly influence future teaching actions.

### 8.1 Working memory

Scope: the current learning session.

Stores:

- current goal;
- active skill and subskill;
- current difficulty;
- remaining time;
- recent questions;
- consecutive correct and incorrect counts;
- recent misconceptions;
- hint use;
- current plan position;
- offline status;
- local content availability.

Primary storage:

- server: SQLite session state;
- client: IndexedDB active-session snapshot.

Working memory must remain fully usable without Mnemis.

### 8.2 Episodic memory

Scope: concrete learning experiences across sessions.

An episode connects context, observation, misconception, intervention, immediate outcome, and later retention when available.

```json
{
  "episode_id": "episode_024",
  "student_id": "student_001",
  "skill": "linear_equations",
  "misconception": "sign_error",
  "observations": ["two_consecutive_errors", "hint_level_2_used"],
  "intervention": "worked_example",
  "outcome": {
    "next_question_correct": true,
    "next_hint_level": 0,
    "mastery_delta": 0.05
  }
}
```

Episodic memory answers:

- Has this learner experienced a similar error before?
- What intervention was used?
- What happened afterward?
- Did the improvement persist?

### 8.3 Semantic learner memory

Scope: stable facts inferred from multiple episodes.

Examples:

- recurring weakness in sign changes;
- worked examples outperform text-only explanations;
- performance declines after long sessions;
- reading inference is improving but confidence remains low;
- a prerequisite skill is blocking progress in another skill.

Every fact must include category, normalized fact representation, confidence, supporting episode IDs, evidence count, contradiction count, timestamps, and active/uncertain/archived status.

### 8.4 Teaching-strategy memory

Scope: evidence about which interventions work for this learner in a given context.

Aggregate by student, skill, misconception, intervention, and difficulty band.

Effectiveness should consider:

- next-attempt correctness;
- hint reduction;
- mastery change;
- retention in a later session;
- response time;
- number of observations.

### 8.5 Memory router

| Decision | Required memory |
|---|---|
| Check current streak | Working memory |
| Resume a session | Working memory + event log |
| Detect a repeated historical error | Episodic memory |
| Generate a weekly plan | Semantic learner memory |
| Select the best intervention | Strategy memory + similar episodes |
| Analyze a multi-skill bottleneck | Mnemis global selection |
| Continue offline | Local working snapshot + compact strategy snapshot |

### 8.6 Memory backend abstraction

```python
class StudentMemoryBackend:
    async def add_episode(self, episode): ...
    async def recall_similar(self, query): ...
    async def get_profile(self, student_id): ...
    async def get_intervention_evidence(self, query): ...
    async def consolidate(self, student_id): ...
```

Required implementations:

```text
SQLiteStudentMemory
MnemisStudentMemory
FallbackStudentMemory
```

Fallback order:

```text
Mnemis within a strict timeout
  -> SQLite episodic and aggregate queries
  -> local client snapshot when offline
```

### 8.7 Mnemis role

Mnemis is the selected advanced long-term-memory retrieval method for the competition enhancement path. It is used for similar episode recall, hierarchical learner memory, global selection for complex planning, and discovery of recurring cross-session patterns.

The verified public state in August 2026 is a formal ACL 2026 paper and an official Microsoft research repository that exposes the Global Selection implementation, evaluation artifacts, prompts, and Graphiti-based reproduction guidance. This is sufficient for a competition prototype, but it is not treated as a complete production memory service.

BridgeSAT therefore owns the full memory lifecycle:

```text
raw learning events
  -> validated learning episode
  -> SQLite authoritative storage
  -> asynchronous Mnemis indexing
  -> System-1 similar recall or System-2 global selection
  -> evidence-bounded policy input
```

Route policy:

| Trigger | Memory route |
|---|---|
| Current streak or session resume | SQLite working memory |
| Repeated known misconception | Mnemis System-1 with SQLite fallback |
| Intervention selection | strategy aggregate + similar episodes |
| New session plan | semantic profile + optional System-1 |
| Multi-skill bottleneck or weekly plan | Mnemis System-2 Global Selection |
| Offline operation | local memory snapshot only |

Mnemis calls must have a strict timeout, return evidence IDs, and never block answer submission. Indexing may be asynchronous because SQLite remains the authoritative store.

It must not be used for answer validation, mastery updates, direct state mutation, offline operation, mandatory session startup, or unrestricted generation of persistent facts.

Any Mnemis-derived fact must retain supporting episode references and confidence.

### 8.8 Memory consolidation

```text
answer attempt
  -> agent intervention
  -> observed outcome
  -> candidate learning episode
  -> validation
  -> episodic memory
  -> periodic consolidation
  -> semantic and strategy memory
```

Consolidation triggers include session completion, repeated misconceptions, known intervention outcomes, periodic summaries, and evaluation runs.

### 8.9 Memory decay and correction

- supporting evidence increases confidence;
- contradictory evidence decreases confidence;
- stale memories decay gradually;
- low-confidence facts become uncertain or archived;
- archived facts remain auditable but do not normally drive decisions;
- recent repeated evidence can reactivate an archived fact.

Student-facing language must describe observed behavior, not fixed ability.

---

## 9. Learner model

For each skill, store:

```text
mastery estimate
confidence
evidence count
last practiced time
correct streak
incorrect streak
hint dependency
average response time
review due time
recent misconception distribution
```

The model must distinguish low mastery from low confidence. A skill with one correct answer should not be considered mastered.

The competition MVP can use a deterministic weighted update based on correctness, difficulty, hint level, response time, repeated attempts, recency, prerequisite status, and whether the answer followed an intervention. Parameters must be versioned and covered by golden tests.

The diagnostic should use a coverage stage followed by an adaptive confirmation stage for weak or uncertain skills.

---

## 10. Adaptive policy and planning

### 10.1 Policy input

```text
current session state
learner model
current attempt result
misconception evidence
remaining time
study-plan goals
content availability
working memory
long-term memory evidence
network state
```

### 10.2 Policy output

```text
next action
target skill
target difficulty
selected content ID
reason code
student-facing explanation
memory evidence IDs
policy version
```

### 10.3 Time-bounded planner

A plan must contain objectives and a time budget, not only a list of activities.

```json
{
  "session_minutes": 20,
  "goals": [
    {
      "skill": "linear_equations",
      "target_questions": 4,
      "target_mastery": 0.60
    }
  ],
  "reserved_review_minutes": 3,
  "adaptation_budget_minutes": 5
}
```

The agent may modify the plan when new evidence appears, but it must preserve the total session limit, minimum review time, bounded introduction of new concepts, and an explicit reason for each change.

---

## 11. Question selection

Questions should primarily be selected from reviewed content, not generated live.

Selection features:

- target skill and subskill;
- target difficulty;
- misconception alignment;
- prerequisite alignment;
- recent-question exclusion;
- estimated completion time;
- offline availability;
- content version;
- accessibility metadata.

The selected question and ranking factors should be traceable in agent events.

---

## 12. Content RAG architecture

The RAG knowledge base is separate from student memory.

### 12.1 RAG content types

- micro-lessons;
- worked examples;
- hint blocks;
- misconception explanations;
- prerequisite summaries;
- reviewed SAT-style questions;
- public-domain or explicitly licensed reading passages;
- source and license records.

### 12.2 Retrieval pipeline

```text
structured teaching need
  -> license, audience, language, and offline-availability filter
  -> skill/subskill/misconception metadata filter
  -> local FTS5 lexical retrieval
  -> local prerequisite-graph expansion
  -> optional online LightRAG retrieval
  -> reciprocal-rank or weighted fusion
  -> bounded reranking
  -> citation, version, and license validation
  -> reviewed content block
```

The MVP must work with metadata filtering, FTS5, and the reviewed prerequisite graph alone. Embeddings and LightRAG are enhancements, not hard dependencies.

### 12.3 Retrieval levels

#### Level 0 — direct deterministic lookup

Used for answer keys, a known hint ID, an explicitly linked micro-lesson, or an offline content-pack object. No model or vector retrieval is involved.

#### Level 1 — local hierarchy-aware retrieval

Used for normal student practice. It combines metadata filtering, FTS5, misconception alignment, and one- or two-hop expansion over the reviewed skill prerequisite graph. This level must remain available offline when the relevant content pack is installed.

#### Level 2 — LightRAG online enhancement

Used when the local result is weak or when a query needs relationships across several approved lessons. LightRAG is accessed through a `KnowledgeBackend` interface and never becomes the source of truth for questions, answers, licenses, or content versions.

The implementation should pin a stable release rather than a release candidate and must retain a local backend for clean-demo reliability.

#### Level 3 — A-RAG-style complex retrieval

Used only for weekly planning, root-cause analysis, or a complex student question. The agent receives bounded tools equivalent to keyword search, semantic search, and chunk read. It has a maximum step count, retrieval-token budget, latency budget, and mandatory citation validator.

It is not invoked for every objective question or ordinary difficulty adjustment.

### 12.4 HiRAG and RAG-Anything boundaries

- HiRAG contributes the hierarchy-aware indexing and retrieval design. BridgeSAT implements a domain-specific SAT hierarchy instead of deploying a second full framework.
- RAG-Anything is an optional offline ingestion utility for licensed documents containing tables, diagrams, or formulas. Parsed output must still pass the source registry, license gate, content validator, and human review.
- Live student requests never trigger crawling or document parsing.

### 12.5 RAG safety boundary

- No content may be retrieved unless its source is approved.
- Restricted sources may be linked or used as taxonomy references only when permitted.
- Attribution and license metadata must be retained.
- Generated explanations must not silently replace source records.
- Retrieved content must be suitable for the student-facing context.

---

## 13. Governed ingestion and crawler architecture

The ingestion system is a controlled import pipeline, not an unrestricted scraper.

```text
source registry
  -> terms and license gate
  -> robots check when crawling is allowed
  -> safe fetch or dataset import
  -> parse
  -> normalize
  -> content hash and deduplication
  -> skill classification
  -> chunking
  -> quality and safety review
  -> approved knowledge index
  -> versioned offline content pack
```

### 13.1 Source registry

Every source must declare its type, URL, license, allowed import method, RAG-ingestion permission, redistribution permission, attribution requirement, human-review requirement, and approval status.

### 13.2 License gate

The license gate must block ingestion when intended use is not allowed, redistribution rights are unclear, AI/RAG ingestion is prohibited, automated access is prohibited, item-level rights are unverified, or the source is not approved.

### 13.3 Safe crawler requirements

- obey robots rules;
- use an identifiable User-Agent;
- block private and local network destinations;
- validate redirect destinations;
- apply per-host rate limits;
- cap response size and timeout;
- support ETag and Last-Modified;
- retain canonical URL and fetch time;
- calculate content hashes;
- prevent duplicate ingestion;
- retain source and license metadata;
- fail closed for unapproved sources.

### 13.4 Human review

External content must pass review for correctness, age suitability, skill mapping, answer correctness, hint quality, misconception mapping, license evidence, accessibility, and final approval.

---

## 14. Offline-first architecture

Offline capability must include the learning loop, not only the page shell.

### 14.1 IndexedDB data

```text
student_snapshot
active_session
skill_state_snapshot
strategy_memory_snapshot
content_pack_manifest
cached_questions
cached_lessons
pending_learning_events
pending_memory_candidates
sync_cursor
```

### 14.2 Offline capabilities

- display the active plan;
- present cached questions;
- evaluate multiple-choice answers;
- track hints and response time;
- update temporary mastery estimates;
- execute a compact local policy;
- recall a compact recent strategy snapshot;
- show cached micro-lessons and worked examples;
- append events to a local queue;
- recover after a browser refresh.

### 14.3 Synchronization

```text
local events
  -> batch upload
  -> validate schema
  -> deduplicate by event ID
  -> verify student and session version
  -> append server event log
  -> rebuild materialized state
  -> create episode candidates
  -> update memory backends
  -> return acknowledgments and a new snapshot
```

The same event may be submitted repeatedly, but it must affect mastery, progress, and memory only once.

Prefer event merging over whole-profile replacement. Incompatible session branches should be preserved for review rather than silently overwritten.

---

## 15. Data model

Minimum SQLite tables:

```text
students
skills
student_skill_states
questions
micro_lessons
content_sources
content_packs
study_plans
study_sessions
session_items
answer_attempts
misconception_events
agent_events
learning_events
learning_episodes
student_memory_facts
intervention_stats
memory_sync_jobs
device_sync_events
```

Key responsibilities:

- `student_skill_states`: mastery, confidence, evidence count, streaks, review date, and recent misconceptions.
- `study_sessions`: lifecycle, plan version, policy version, content version, and recovery state.
- `answer_attempts`: answer, correctness, hint level, response time, misconception, and origin.
- `agent_events`: every consequential decision and its evidence.
- `learning_episodes`: validated context-intervention-outcome units.
- `student_memory_facts`: consolidated facts with confidence and supporting evidence.
- `intervention_stats`: aggregate intervention effectiveness.
- `device_sync_events`: synchronization identity, acknowledgment, retry, and conflict information.

---

## 16. Event model

The event log is the shared foundation for session recovery, memory creation, offline synchronization, explanation, evaluation, debugging, and reproducibility.

```json
{
  "event_id": "evt_01JXYZ",
  "student_id": "stu_001",
  "session_id": "ses_010",
  "event_type": "MICRO_LESSON_INSERTED",
  "skill": "linear_equations",
  "reason_code": "REPEATED_KNOWN_MISCONCEPTION",
  "reason_text": "The same sign error appeared twice and a worked example was previously effective.",
  "evidence_refs": ["attempt_108", "attempt_109", "episode_024"],
  "policy_version": "policy-0.2.0",
  "content_version": "sat-pack-0.2.0",
  "origin": "online",
  "occurred_at": "2026-08-06T15:00:00+08:00"
}
```

---

## 17. Optional LLM integration

Allowed uses:

- rewrite structured explanations clearly;
- classify short free-form student explanations;
- translate reviewed content;
- generate summaries from verified events;
- propose draft questions for human review;
- assist memory consolidation under a strict schema.

Forbidden direct responsibilities:

- determine multiple-choice correctness;
- directly change mastery;
- directly transition session state;
- write persistent learner facts without validation;
- bypass source and license controls;
- generate unreviewed assessment content;
- become mandatory for offline learning.

Control flow:

```text
deterministic structured decision
  -> optional LLM phrasing or classification
  -> schema validation
  -> policy and safety validation
  -> student-facing response
```

---

## 18. Planned API surface

```text
POST /v1/students
GET  /v1/students/{student_id}

POST /v1/diagnostics/start
POST /v1/diagnostics/{diagnostic_id}/answer
POST /v1/diagnostics/{diagnostic_id}/complete

POST /v1/sessions
GET  /v1/sessions/{session_id}
POST /v1/sessions/{session_id}/events
POST /v1/sessions/{session_id}/hint
POST /v1/sessions/{session_id}/pause
POST /v1/sessions/{session_id}/resume
POST /v1/sessions/{session_id}/complete

GET  /v1/content/packs
GET  /v1/content/packs/{version}
POST /v1/content/retrieve

GET  /v1/memory/{student_id}/profile
GET  /v1/memory/{student_id}/decisions/{event_id}
DELETE /v1/memory/{student_id}

POST /v1/sync/events
GET  /v1/sync/snapshot/{student_id}

GET  /v1/reports/{student_id}/progress
GET  /v1/reports/{student_id}/agent-decisions
```

---

## 19. Privacy, safety, and learner dignity

- use pseudonymous student IDs;
- make real names optional;
- do not collect school, phone, address, or precise location;
- separate learner identity from learning events;
- do not send unnecessary raw student text to third-party models;
- provide deletion of learner data and memory;
- avoid logging sensitive free-form content;
- whitelist synchronized fields;
- keep secrets outside the repository;
- do not present memory summaries as permanent traits;
- show uncertainty when evidence is limited;
- explain why a plan changed;
- distinguish educational support from guaranteed score improvement.

---

## 20. Observability and explainability

The MVP can use structured logs and generated reports instead of a full monitoring stack.

Track:

- session completion;
- policy action counts;
- memory backend use and fallback;
- retrieval latency;
- offline event counts;
- duplicate synchronization events;
- content-pack versions;
- failed content retrieval;
- Mnemis timeout and error rate;
- decision-explanation coverage.

Student-visible explanation example:

```text
BridgeSAT changed your plan because:

1. the same sign error appeared twice;
2. a worked example helped in a previous session;
3. nine minutes remain, so the lesson fits the current session.
```

---

## 21. Evaluation architecture

```text
evals/
├── golden_policy_scenarios.json
├── golden_memory_scenarios.json
├── golden_rag_queries.json
├── offline_scenarios.json
├── sync_scenarios.json
├── content_license_cases.json
├── run_policy_eval.py
├── run_memory_eval.py
├── run_rag_eval.py
├── run_offline_eval.py
├── run_sync_eval.py
├── run_content_audit.py
└── reports/
```

### 21.1 Policy evaluation

- repeated misconception inserts the appropriate intervention;
- correct unassisted responses raise difficulty only when confidence is sufficient;
- low remaining time ends with review;
- overdue review takes priority over new content;
- stale memory does not override strong recent evidence.

### 21.2 Memory evaluation

- the second session recalls a relevant first-session episode;
- irrelevant episodes are not surfaced;
- effective interventions rank above ineffective ones when evidence is sufficient;
- contradictory evidence reduces confidence;
- archived memories do not normally control decisions;
- SQLite fallback works when Mnemis is unavailable.

### 21.3 RAG evaluation

- retrieval relevance by skill and misconception;
- citation coverage;
- license metadata coverage;
- restricted-source exclusion;
- offline-pack availability;
- no-answer behavior when approved content is unavailable.

### 21.4 Offline and synchronization evaluation

- cached session completes without network;
- refresh restores the active session;
- repeated event upload is idempotent;
- out-of-order events are handled consistently;
- content-version mismatch is explicit;
- pending events synchronize after reconnect.

Suggested metrics:

```text
policy golden-scenario pass rate
memory recall precision on controlled cases
intervention-selection accuracy
RAG citation coverage
approved-license coverage
offline core-flow completion rate
restart recovery rate
duplicate-sync protection rate
local interaction latency
decision-explanation coverage
```

---

## 22. Failure and fallback design

| Failure | Required behavior |
|---|---|
| No network | Continue with local session, content pack, and compact policy |
| Mnemis unavailable | Use SQLite episodic and aggregate memory |
| Graph database unavailable | Disable global selection; preserve core memory queries |
| Embedding model unavailable | Use metadata and FTS5 retrieval |
| LLM unavailable | Use deterministic templates and classifiers |
| Missing content | Do not fabricate; choose another approved action or explain unavailable content |
| Duplicate sync | Acknowledge but do not reapply |
| Browser refresh | Restore active session from IndexedDB |
| Server restart | Rebuild current state from SQLite state and events |
| Stale content pack | Continue a compatible session or require an explicit update |
| Corrupt event | Reject it and preserve the remaining queue |

---

## 23. Deployment architecture

Competition deployment:

```text
static PWA assets
  + FastAPI application
  + SQLite database
  + local reviewed content packs
  + optional external model API
  + optional Mnemis/Graphiti backend
```

The default startup must not require optional services.

Recommended modes:

```text
BRIDGESAT_MODE=local
  SQLite memory
  FTS5 retrieval
  no external model required

BRIDGESAT_MODE=enhanced
  SQLite + Mnemis
  FTS5 + embeddings
  optional LLM explanation
```

---

## 24. Implementation phases

### Phase 1: event-driven learning loop

- session model and state machine;
- immutable event log;
- answer attempts;
- misconception mapping;
- adaptive question selection;
- three-level hints;
- session summary.

### Phase 2: base memory loop

- SQLite episodic memory;
- learning-episode construction;
- semantic memory facts;
- intervention-effectiveness statistics;
- memory-influenced second-session decisions;
- confidence and decay rules.

### Phase 3: governed RAG

- source registry;
- license gate;
- independent safe crawler and importers;
- content normalization and review;
- FTS5 retrieval;
- citations;
- versioned content packs.

### Phase 4: offline proof

- IndexedDB state;
- offline evaluator;
- compact local policy;
- compact memory snapshot;
- event queue;
- idempotent synchronization;
- throttled-network and fully offline tests.

### Phase 5: Mnemis enhancement

- memory-backend adapter;
- similar episode recall;
- hierarchical memory graph;
- global selection prototype;
- timeout, fallback, and observability;
- controlled evaluation against SQLite-only memory.

### Phase 6: presentation and evidence

- 80 to 120 reviewed questions;
- 8 to 10 skills;
- 15 to 20 policy scenarios;
- memory demonstration across two sessions;
- offline demonstration;
- accessibility checks;
- performance measurements;
- screenshots, architecture diagram, video, and submission document.

---

## 25. Architecture completeness audit

### 25.1 Areas that are sufficiently specified

1. **Product boundary**: an adaptive learning agent rather than a chatbot.
2. **Deployment shape**: a modular monolith appropriate for the competition timeline.
3. **Agent control**: bounded actions, state transitions, event logging, and policy priorities.
4. **Memory model**: working, episodic, semantic, and teaching-strategy memory have separate roles.
5. **Mnemis boundary**: optional advanced memory with required SQLite fallback.
6. **RAG boundary**: educational knowledge is separate from learner memory.
7. **Data governance**: source registry, license gate, review queue, and attribution.
8. **Offline path**: local state, content packs, event queues, and idempotent synchronization.
9. **Explainability**: decisions retain reasons and evidence references.
10. **Evaluation**: policy, memory, RAG, offline, sync, and content tests are identified.
11. **Privacy**: pseudonymous data and memory deletion are included.
12. **Failure handling**: optional components have degraded modes.

### 25.2 Important gaps that still require concrete decisions

The major architecture contracts are now frozen in the companion specifications. Remaining uncertainty is implementation and evidence rather than missing design.

#### A. Actual reviewed content

Create the specified eight-skill content set, review records, license records, misconception mappings, and versioned offline pack.

#### B. Empirical calibration

Validate the frozen mastery weights, promotion thresholds, intervention thresholds, retrieval routing, and memory confidence thresholds against controlled scenarios.

#### C. Mnemis operational verification

Pin the actual dependency versions, select the graph backend, measure local hardware and deployment latency, and verify the adapter against the official implementation available at build time.

#### D. Optional retrieval go/no-go

Run the required ablations before enabling embeddings or LightRAG in the live demo.

#### E. Deployment evidence

Complete clean-install, migration, backup restore, public deployment, accessibility, security, and offline-device tests.

### 25.3 Over-engineering risks

- production-scale graph infrastructure;
- multi-agent orchestration;
- complex distributed synchronization;
- large-scale live web crawling;
- unrestricted LLM-generated questions;
- a separate teacher dashboard;
- a dedicated vector database for a small corpus;
- extensive cloud observability.

The project should prioritize a visible, reliable second-session memory effect and a complete offline learning loop.

### 25.4 Minimum complete competition architecture

```text
1. A student completes a diagnostic.
2. The agent produces a time-bounded plan.
3. The student shows a repeated misconception.
4. The agent retrieves an approved targeted intervention.
5. The intervention and outcome become a learning episode.
6. A later session recalls that episode.
7. The recalled memory changes the next action.
8. The agent explains the memory-based decision.
9. The same core session can continue offline.
10. Offline events synchronize exactly once.
11. The RAG response shows source and license metadata.
12. Golden evaluations verify the above behavior.
```

### 25.5 Overall assessment

**Architecture completeness: approximately 97% at the design-contract level.**

The subsystem boundaries, algorithms, consistency rules, synchronization semantics, security controls, accessibility targets, and evaluation gates are now specified. The remaining work is implementing these contracts and producing evidence:

- reviewed content and source records;
- executable database migrations and event projections;
- measured Mnemis feasibility and benefit;
- measured retrieval ablations;
- security, accessibility, offline, and restore test results;
- calibrated educational thresholds.

The architecture should not expand further until the minimum memory loop and event-driven session flow are implemented and tested.

---

## 26. Immediate next implementation step

The next code milestone should be:

> A second session remembers a first-session misconception and intervention outcome, then chooses a different action for a traceable reason.

Implement in this order:

1. `learning_events` and `agent_events`;
2. session state machine;
3. `learning_episodes` construction;
4. SQLite memory backend;
5. intervention-effectiveness aggregation;
6. memory-aware policy decision;
7. two-session golden test;
8. only then add the Mnemis adapter.

This milestone validates the central product claim before adding crawler, vector, graph, or model complexity.
