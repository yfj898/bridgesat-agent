# BridgeSAT API and Operations Contract

## 1. Runtime modes

```text
BRIDGESAT_MODE=local
  FastAPI + SQLite + FTS5 + reviewed local content
  no external model, vector service, graph database, or Mnemis required

BRIDGESAT_MODE=enhanced
  local mode capabilities
  + optional embeddings
  + optional LightRAG adapter
  + optional Mnemis adapter
  + optional LLM explanation adapter
```

The default startup path is `local`.

---

## 2. Reproducibility

Freeze before submission:

```text
Python 3.12
locked Python dependencies
Node-free static PWA build or locked frontend dependencies
SQLite schema version
content-pack version
policy versions
environment variable reference
seed demo command
database migration command
evaluation command
```

Required commands:

```bash
python -m bridgesat.migrate
python -m bridgesat.seed_demo
python -m bridgesat.verify
pytest
python -m evals.run_all
uvicorn app.main:app
```

Exact module names may change during implementation, but equivalent commands must exist.

---

## 3. API conventions

- base path: `/v1`;
- JSON request and response bodies;
- UTC ISO-8601 timestamps on the server;
- request ID returned in `X-Request-Id`;
- state-changing replay-sensitive endpoints accept `Idempotency-Key`;
- error responses follow one schema;
- maximum body size is configured;
- pagination uses opaque cursors;
- breaking changes require `/v2`.

Error schema:

```json
{
  "error": {
    "code": "STUDENT_NOT_FOUND",
    "message": "The learner profile was not found.",
    "retryable": false,
    "request_id": "req_01J...",
    "details": {}
  }
}
```

---

## 4. Authentication model

Competition MVP:

- create a pseudonymous learner profile;
- issue a scoped learner session token;
- token grants access only to that learner;
- device IDs are registered under the learner token;
- administrative ingestion and review endpoints use a separate operator key;
- no endpoint trusts a student ID without verifying token scope.

---

## 5. Planned endpoint contracts

### 5.1 Learners

```text
POST   /v1/students
GET    /v1/students/me
PATCH  /v1/students/me/preferences
DELETE /v1/students/me
```

### 5.2 Diagnostics

```text
POST /v1/diagnostics
GET  /v1/diagnostics/{diagnostic_id}
POST /v1/diagnostics/{diagnostic_id}/answers
POST /v1/diagnostics/{diagnostic_id}/complete
```

### 5.3 Sessions

```text
POST /v1/sessions
GET  /v1/sessions/{session_id}
POST /v1/sessions/{session_id}/events
POST /v1/sessions/{session_id}/pause
POST /v1/sessions/{session_id}/resume
POST /v1/sessions/{session_id}/complete
```

### 5.4 Content

```text
GET  /v1/content/packs
GET  /v1/content/packs/{pack_id}
POST /v1/content/retrieve
POST /v1/content/report
```

### 5.5 Memory

```text
GET    /v1/memory/profile
GET    /v1/memory/decisions/{event_id}
PATCH  /v1/memory/facts/{fact_id}
DELETE /v1/memory/facts/{fact_id}
DELETE /v1/memory
```

### 5.6 Sync

```text
POST /v1/sync/events
GET  /v1/sync/snapshot
POST /v1/sync/devices
DELETE /v1/sync/devices/{device_id}
```

### 5.7 Reports

```text
GET /v1/reports/progress
GET /v1/reports/agent-decisions
GET /v1/reports/export
```

---

## 6. Concurrency and idempotency

- learner projection rows carry a version number;
- write operations use optimistic concurrency where appropriate;
- event append and projection update occur in one SQLite transaction;
- duplicate idempotency keys return the previous result;
- session completion is idempotent;
- content reports and memory corrections are append-only events.

---

## 7. Database migration

Migration rules:

- every schema change has an ordered migration ID;
- migrations are transactional when SQLite permits;
- application refuses startup if schema is newer than supported;
- backup is created before destructive migration;
- migration tests run from the earliest supported schema;
- derived indexes can be rebuilt rather than migrated in place.

Required recovery capabilities:

```text
restore SQLite backup
rebuild learner projections from events
rebuild FTS5 index from approved content
rebuild Mnemis from validated episodes and facts
verify content-pack checksums
```

---

## 8. Backup and retention

Competition deployment baseline:

- daily SQLite backup;
- pre-migration backup;
- retention of seven daily backups during the competition window;
- backups stored outside the active database path;
- restore test completed before submission;
- demo learner data can be reset independently.

Long-term retention policy must be documented before real deployment. Competition data uses fictional or consented test profiles.

---

## 9. Optional-service circuit breakers

For Mnemis, embeddings, LightRAG, and LLM providers:

```text
closed
  -> failures reach threshold
open
  -> skip calls and use fallback
half_open
  -> probe recovery
closed
```

Suggested thresholds:

- open after three consecutive failures or `>= 50%` failures in the last ten calls;
- probe after 30 seconds;
- strict per-call timeout;
- report fallback use in metrics and Agent evidence.

---

## 10. Performance budgets

| Operation | Target |
|---|---:|
| health endpoint | < 100 ms |
| local question selection | < 100 ms |
| local policy decision | < 150 ms |
| FTS5 retrieval | < 200 ms |
| session restore | < 500 ms |
| Mnemis System-1 | < 800 ms timeout |
| Mnemis System-2 | < 3 s timeout |
| PWA initial compressed shell | < 250 KB target |
| default offline pack | < 5 MB target |

These are competition targets, not universal production guarantees.

---

## 11. Accessibility acceptance criteria

- WCAG 2.1 AA color contrast target;
- all core flows keyboard-operable;
- visible focus indicator;
- touch targets at least 44 by 44 CSS pixels;
- form controls have accessible names;
- progress and error states are announced to assistive technology;
- no required information is conveyed by color alone;
- 200% text zoom does not block the core flow;
- reduced-motion preference is respected;
- mathematical content has accessible text representation;
- offline and synchronization state has text labels.

---

## 12. Content-pack operations

Every pack includes:

```text
pack ID
semantic version
creation timestamp
schema version
content manifest
source and license manifest
checksums
minimum application version
withdrawn-content list
```

Pack activation is atomic. The client verifies checksums before use.

---

## 13. LLM operational contract

Every task configuration defines:

```json
{
  "task": "rewrite_explanation",
  "model": "configured-model-id",
  "prompt_version": "rewrite-v1",
  "allowed_input_fields": ["approved_explanation", "language"],
  "timeout_ms": 3000,
  "max_output_tokens": 300,
  "fallback": "deterministic_template",
  "output_schema": "ExplanationResponseV1"
}
```

Costs and tokens are logged without storing unnecessary learner text.

---

## 14. Health and readiness

```text
GET /health
  application process alive

GET /ready
  SQLite writable
  schema compatible
  reviewed content pack loaded
```

Optional-service failures do not make local mode unready. They appear in a degraded-services field.

---

## 15. Release checklist

```text
clean install succeeds
all migrations pass
all tests pass
all golden evaluations pass thresholds
source and license audit passes
no secret scan findings
offline flow passes
restore test passes
Mnemis fallback passes
accessibility checklist passes
demo seed works
public links work
```
