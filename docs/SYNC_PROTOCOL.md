# BridgeSAT Offline Synchronization Protocol

## 1. Goals

The protocol must support unreliable networks, page refreshes, delayed uploads, duplicate submissions, and a limited multi-device case without corrupting learner state.

Core properties:

```text
immutable events
idempotent application
content-version binding
server-authoritative projections
explicit conflicts
safe offline continuation
```

---

## 2. Event envelope

Every client-generated event uses:

```json
{
  "event_id": "01J...",
  "student_id": "student_01",
  "session_id": "session_01",
  "session_branch_id": "branch_device_a",
  "device_id": "device_a",
  "device_sequence": 17,
  "event_type": "ANSWER_SUBMITTED",
  "payload": {},
  "content_pack_version": "pack-2026.08.1",
  "question_id": "math_linear_001",
  "question_version": 1,
  "policy_version": "offline-policy-v1",
  "device_occurred_at": "2026-08-06T16:00:00+08:00",
  "created_monotonic_ms": 912345,
  "integrity_hash": "sha256:..."
}
```

Rules:

- `event_id` is globally unique and immutable;
- `device_sequence` strictly increases per device, including within one batch;
- device wall-clock time is informative only;
- ordering is not based solely on device timestamps;
- question scoring is bound to the specified question version;
- event payloads are validated against an event-type schema.

`WORKED_EXAMPLE_PRESENTED` is the client confirmation that a server-selected
intervention was actually shown. Its payload binds the source answer event,
worked-example content ID/version, skill, misconception, and intervention. The
server accepts it only when all fields match the same-session
`SHOW_WORKED_EXAMPLE` decision. A runtime Episode then remains a candidate until
a correct, low-hint answer arrives on a question different from the triggering
question.

---

## 3. Local IndexedDB stores

```text
profile_snapshot
active_session
content_packs
pending_events
acknowledged_events
memory_snapshot
sync_state
```

`sync_state` includes:

```json
{
  "device_id": "device_a",
  "last_device_sequence": 17,
  "last_server_cursor": "cursor_122",
  "base_snapshot_version": 12,
  "active_content_pack": "pack-2026.08.1"
}
```

---

## 4. Sync API

### 4.1 Request

```http
POST /v1/sync/events
Idempotency-Key: sync-device_a-18-24
```

```json
{
  "device_id": "device_a",
  "student_id": "student_01",
  "base_snapshot_version": 12,
  "last_server_cursor": "cursor_122",
  "content_pack_versions": ["pack-2026.08.1"],
  "events": []
}
```

Maximum events per request: 100.

### 4.2 Response

```json
{
  "accepted_event_ids": [],
  "duplicate_event_ids": [],
  "rejected_events": [
    {
      "event_id": "...",
      "code": "QUESTION_VERSION_UNKNOWN",
      "retryable": false
    }
  ],
  "conflicts": [],
  "new_snapshot_version": 13,
  "new_server_cursor": "cursor_130",
  "server_events": [],
  "required_content_packs": [],
  "memory_snapshot": {},
  "sync_status": "complete"
}
```

---

## 5. Server processing order

For each batch:

```text
1. authenticate student and device;
2. validate batch size and schemas;
3. verify integrity hashes;
4. identify duplicate event IDs;
5. validate referenced content versions;
6. store valid events append-only;
7. apply events in strict device-sequence order with domain rules;
8. detect semantic conflicts;
9. generate server-side Agent events where applicable;
10. commit transaction;
11. return acknowledgements and updated snapshot metadata.
```

An invalid event does not invalidate unrelated valid events in the same batch.

---

## 6. Conflict semantics

### 6.1 Duplicate event

Same `event_id`:

- acknowledge as duplicate;
- do not reapply mastery or statistics;
- return the prior processing result when available.

### 6.2 Repeated attempt on the same item

Different event IDs but the same `attempt_id`:

- only the first valid submission affects mastery;
- later submissions are retained for audit with `non_scoring_duplicate` status.

### 6.3 Two devices answer the same question

If both are distinct valid attempts:

- preserve both events;
- only one may satisfy a single planned session slot;
- both may inform history, but the second receives reduced repeated-item weight;
- create `PARALLEL_ATTEMPT_DETECTED` for audit.

### 6.4 Two active branches

If two devices continue the same session from the same base snapshot:

- designate the first server-received branch as primary;
- retain the second as a parallel branch;
- do not silently merge plan position;
- merge valid learning evidence with duplicate protections;
- generate a revised session summary when necessary.

Competition UI may warn that the session is active on another device.

### 6.5 Late events after session summary

- append late events normally;
- update learner projections if valid;
- do not overwrite the historical summary;
- emit `SUMMARY_REVISED` with the reason and differences;
- show the latest revision in reports while preserving prior revisions.

### 6.6 Incorrect device clock

Device time is never used as the sole order source. Use:

```text
device_sequence
server_received_at
event dependencies
session state constraints
```

### 6.7 Content pack mismatch

- score using the exact referenced question version;
- if that version is known and not safety-withdrawn, accept;
- if unknown, reject with `QUESTION_VERSION_UNKNOWN`;
- if safety-withdrawn, reject scoring and create a remediation event;
- never score an old attempt using a newer answer key.

---

## 7. Event dependencies

Events may declare:

```json
{
  "depends_on_event_ids": ["question_presented_event_id"]
}
```

If a dependency is missing, the server returns retryable
`MISSING_DEPENDENCY`; the client retains the stable event ID and resubmits it
after the dependency is acknowledged. It is not projected early.

---

## 8. Retry behavior

Client retry schedule:

```text
immediate
5 seconds
15 seconds
60 seconds
5 minutes
then every 15 minutes while the app is active
```

Rules:

- reuse the same event IDs;
- reuse a stable batch idempotency key for the same batch;
- do not delete local events until acknowledged;
- preserve rejected non-retryable events for user-visible diagnostics;
- background sync is an enhancement, not a requirement for correctness.

---

## 9. Snapshot protocol

The server provides a compact snapshot:

```text
student profile
skill states
active or most recent session
current plan
compact strategy memory
content-pack requirements
snapshot version
server cursor
```

Snapshots are projections, not replacements for events.

Client startup:

```text
load local snapshot
  -> resume immediately
  -> contact server when possible
  -> upload pending events
  -> apply server acknowledgements
  -> replace local projection with newer server snapshot
```

---

## 10. Security requirements

- sync endpoint requires a scoped student session token;
- device registration produces a revocable device ID;
- payload fields are whitelisted;
- batch and payload sizes are capped;
- event hashes detect accidental corruption, not malicious forgery by themselves;
- HTTPS is required outside localhost;
- server never trusts client-computed mastery values;
- client submits observations; server recomputes authoritative projections;
- offline Agent actions are marked with the offline policy version and revalidated on sync.

---

## 11. Error codes

```text
INVALID_SCHEMA
UNAUTHORIZED_STUDENT
DEVICE_REVOKED
DUPLICATE_EVENT
ATTEMPT_ALREADY_SCORED
QUESTION_VERSION_UNKNOWN
CONTENT_WITHDRAWN
MISSING_DEPENDENCY
SESSION_EXPIRED
SESSION_BRANCH_CONFLICT
PAYLOAD_TOO_LARGE
RATE_LIMITED
INTERNAL_RETRYABLE
```

Every rejection states whether it is retryable.

---

## 12. Offline performance budgets

```text
local answer evaluation: < 100 ms target
local policy decision: < 150 ms target
session restoration: < 500 ms target
initial compressed app shell: < 250 KB target
default offline content pack: < 5 MB target
pending event capacity: at least 5,000 events
```

---

## 13. Acceptance tests

Required scenarios:

1. complete a learning session with no network;
2. refresh mid-session and recover exactly;
3. upload the same batch three times without duplicate scoring;
4. upload events out of order and reach the same final projection;
5. sync an old known question version correctly;
6. reject an unknown question version safely;
7. handle two parallel device branches explicitly;
8. revise a summary after a valid late event;
9. restart the server with pending local events;
10. preserve unacknowledged client events after a failed sync.
