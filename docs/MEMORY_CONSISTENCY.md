# BridgeSAT Memory Consistency Specification

## 1. Core decision

PostgreSQL is the authoritative system of record. Mnemis is a derived, rebuildable long-term-memory index.

```text
PostgreSQL facts and events
  -> validated learning episodes
  -> transactional outbox
  -> Mnemis indexing
  -> memory retrieval
```

No request may require a successful synchronous write to both PostgreSQL and Mnemis.

---

## 2. Memory layers

| Layer | Authority | Storage | Rebuildable |
|---|---|---|---|
| working memory | current event stream | PostgreSQL + IndexedDB | yes |
| episodic memory | validated learning episodes | PostgreSQL | yes |
| semantic learner facts | derived evidence records | PostgreSQL | yes |
| intervention statistics | aggregate outcomes | PostgreSQL | yes |
| hierarchical recall index | derived graph/index | Mnemis backend | yes |

Mnemis output is retrieval evidence, not an authoritative fact by itself.

---

## 3. Required tables

### 3.1 Learning events

```sql
CREATE TABLE learning_events (
    event_id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    content_version TEXT,
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    device_id TEXT,
    device_sequence INTEGER,
    origin TEXT NOT NULL,
    integrity_hash TEXT NOT NULL
);
```

### 3.2 Learning episodes

```sql
CREATE TABLE learning_episodes (
    episode_id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    skill TEXT NOT NULL,
    misconception TEXT,
    intervention TEXT,
    outcome_json TEXT NOT NULL,
    effectiveness REAL,
    evidence_event_ids_json TEXT NOT NULL,
    summary TEXT NOT NULL,
    confidence REAL NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

### 3.3 Semantic memory facts

```sql
CREATE TABLE student_memory_facts (
    fact_id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL,
    category TEXT NOT NULL,
    normalized_key TEXT NOT NULL,
    fact_text TEXT NOT NULL,
    confidence REAL NOT NULL,
    supporting_episode_ids_json TEXT NOT NULL,
    contradicting_episode_ids_json TEXT NOT NULL,
    evidence_count INTEGER NOT NULL,
    contradiction_count INTEGER NOT NULL,
    status TEXT NOT NULL,
    first_observed_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL,
    version INTEGER NOT NULL
);
```

### 3.4 Memory outbox

```sql
CREATE TABLE memory_outbox (
    outbox_id TEXT PRIMARY KEY,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    last_error TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
```

---

## 4. Transactional outbox workflow

Within one PostgreSQL transaction:

```text
1. append immutable learning event;
2. update current session projection;
3. create or update validated episode when eligible;
4. append memory_outbox record;
5. commit.
```

After commit, a worker processes the outbox.

Outbox states:

```text
pending
processing
indexed
retrying
dead_letter
deletion_pending
deleted
```

Retry policy:

```text
attempt 1: immediate
attempt 2: +5 seconds
attempt 3: +30 seconds
attempt 4: +5 minutes
attempt 5: +30 minutes
then: dead_letter
```

The competition demo may use an in-process worker, but the outbox contract must remain explicit.

---

## 5. Idempotency

Mnemis indexing uses a stable key:

```text
memory-index:{student_id}:{aggregate_type}:{aggregate_id}:{version}:{operation}
```

Repeated delivery of the same outbox operation must not create duplicate graph nodes or edges.

Index adapters must support:

```text
upsert_episode
upsert_fact
archive_fact
delete_student
delete_episode
rebuild_student
```

---

## 6. Episode formation contract

A raw mistake is not automatically a long-term memory.

An episode is created only when it contains:

1. a valid context event;
2. one or more observations;
3. an Agent intervention or explicit no-intervention baseline;
4. at least one outcome event;
5. content and policy versions;
6. valid supporting event IDs.

Episode statuses:

```text
candidate
validated
insufficient_outcome
contradicted
archived
deleted
```

Only `validated` episodes are sent to Mnemis.

Minimum validation rules:

- attempt was not invalidated;
- question version is available;
- intervention was actually displayed or executed;
- outcome uses a different item from the teaching example;
- no content-error flag exists;
- confidence is at least `0.50`.

---

## 7. Semantic fact formation

Three fact classes are distinguished.

### 7.1 Observation

Directly supported by events:

> Two sign errors occurred in Session 4.

### 7.2 Inference

Tentative cross-event conclusion:

> Sign handling may be a recurring difficulty.

### 7.3 Stable learner fact

Supported across multiple contexts:

> Worked examples have been more effective than text-only explanations for sign-handling errors.

Promotion thresholds:

| Transition | Requirement |
|---|---|
| observation -> inference | two supporting episodes on distinct items |
| inference -> stable | at least three supporting episodes across two sessions and confidence `>= 0.70` |
| stable -> uncertain | contradiction-adjusted confidence `< 0.55` |
| uncertain -> archived | confidence `< 0.35` or no support after configured staleness period |

Confidence calculation for facts:

```text
base = support_weight / (support_weight + contradiction_weight + 2)
recency_factor = max(0.70, 0.98 ^ inactive_weeks_after_4)
confidence = clamp(base × recency_factor, 0, 1)
```

High-level memory generated by an LLM must preserve the supporting episode IDs and cannot bypass these thresholds.

---

## 8. Contradictions and correction

New evidence may support, contradict, or be neutral to an existing fact.

Rules:

- current direct evidence outranks old inferred memory;
- contradictory evidence is never discarded merely because a stable fact exists;
- facts are updated by version, not overwritten without history;
- archived facts remain auditable but do not normally influence actions;
- student corrections create a `USER_MEMORY_CORRECTION` event;
- a correction can mark a fact disputed immediately, but underlying events remain unless deletion is requested.

---

## 9. Memory retrieval routing

### 9.1 PostgreSQL-only queries

Use PostgreSQL for authoritative current streaks, recent attempts, mastery,
intervention statistics, and server-side session recovery. During a network
outage, the PWA uses its last synchronized IndexedDB snapshot and queues new
events; it does not pretend the browser can query PostgreSQL offline.

### 9.2 Mnemis System-1

Use for similar historical episodes when current evidence indicates a repeated or known misconception, the learner has at least three validated episodes, network mode is enhanced, and the latency budget allows it.

Default budget:

```text
timeout: 800 ms
top-k: 5
minimum result confidence: 0.55
```

### 9.3 Mnemis System-2

Use only for weekly planning, multi-skill bottleneck analysis, prerequisite-root-cause analysis, and periodic memory consolidation.

Default budget:

```text
timeout: 3 seconds
maximum selected memory nodes: 12
maximum hierarchy depth: 4
```

### 9.4 Evidence precedence

```text
current validated observations
  > current learner model
  > high-confidence stable memory
  > similar validated episodes
  > low-confidence inferred memory
```

Memory with confidence below `0.55` may inform an explanation but cannot independently control an action.

---

## 10. Mnemis adapter contract

```python
class MnemisMemoryAdapter:
    async def upsert_episode(self, episode, idempotency_key): ...
    async def upsert_fact(self, fact, idempotency_key): ...
    async def recall_similar(self, query, timeout_ms=800): ...
    async def global_select(self, query, timeout_ms=3000): ...
    async def delete_student(self, student_id, idempotency_key): ...
    async def health(self): ...
```

Every result must include:

```text
memory_id
memory_type
supporting_episode_ids
confidence
retrieval_route
retrieval_score
index_version
```

Results without supporting episode references are excluded from consequential decisions.

---

## 11. Deletion protocol

Student deletion is a distributed process.

```text
1. authenticate deletion request;
2. mark learner account deletion_pending;
3. stop new sessions and memory writes;
4. delete or tombstone PostgreSQL personal records according to policy;
5. create deletion outbox event;
6. delete Mnemis nodes and edges;
7. verify no retrievable memory remains;
8. mark deletion complete.
```

Deletion states:

```text
requested
sqlite_deleted
index_deletion_pending
verified
failed
```

The user receives completion only after verification, not after the first local deletion step.

---

## 12. Rebuild and migration

Mnemis must be fully rebuildable from PostgreSQL.

Required commands:

```text
rebuild all memory indexes
rebuild one student
verify index parity
replay dead-letter outbox records
compare PostgreSQL episode count with indexed episode count
```

Index versions are immutable identifiers. A new index version is built alongside the old version, validated, and then activated.

---

## 13. Consistency monitoring

Required metrics:

```text
outbox_pending_count
outbox_oldest_age_seconds
outbox_dead_letter_count
memory_index_success_rate
memory_index_latency_ms
sqlite_episode_count
indexed_episode_count
deletion_pending_count
memory_fallback_rate
```

Competition acceptance:

- duplicate indexing produces zero duplicate memories;
- Mnemis outage does not block the core session;
- restart resumes pending outbox delivery;
- one-student rebuild reproduces expected retrieval results;
- deletion verification removes all retrievable indexed memories;
- PostgreSQL fallback passes all mandatory memory scenarios.
