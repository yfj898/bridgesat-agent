# Task 2 Quality Blockers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the remaining PostgreSQL sync ownership, exception-classification, snapshot-authority, and deletion-lock cleanup gaps without changing Task 3 or Task 4.

**Architecture:** Keep the existing outer PostgreSQL batch transaction and per-event savepoints. Add an explicit sync-domain validation exception, centralize session ownership validation under the authoritative request student and tenant, let authoritative snapshot errors escape the batch, and rollback the deletion connection for every exceptional completion path before the advisory-lock context exits.

**Tech Stack:** Python 3.11+, FastAPI, psycopg PostgreSQL, pytest, asyncio, PostgreSQL advisory/row locks.

---

### Task 1: Add red sync ownership and exception-classification regressions

**Files:**
- Modify: `tests/test_pg_sync.py`
- Modify: `app/sync/service.py` only after the tests fail

- [x] **Step 1: Add a same-tenant cross-student session test.**

Create two active students and devices in the existing tenant, create a session for student B, then submit a valid answer for student A using B's `session_id`. Assert the response rejects the event, no A event/attempt is committed, and B's session state and attempts are unchanged.

- [x] **Step 2: Change the existing partial-savepoint test to raise `EventValidationError`.**

The test must continue proving that partial accepted/conflict/server-agent response state is truncated before the typed rejection is appended.

- [x] **Step 3: Add a generic `ValueError` fatal-batch regression.**

Raise a plain `ValueError` from event application after a projection write and assert it propagates, with no event, projection, or device-sequence commit.

- [x] **Step 4: Run the sync regressions before production changes.**

Run:

```bash
./.venv/bin/pytest tests/test_pg_sync.py -k \
  'cross_student_session or rejectable_event_failure or generic_value_error' -q
```

Expected result: the new ownership and generic-error assertions fail against the current implementation; the existing typed-savepoint test documents the intended response cleanup.

### Task 2: Add the snapshot-authority regression

**Files:**
- Modify: `tests/test_pg_sync.py`

- [x] **Step 1: Monkeypatch authoritative fact retrieval to raise `psycopg.OperationalError`.**

Process a valid answer event, fail `service.memory.get_facts`, and assert the exception propagates. Query PostgreSQL afterward to prove no learning event, answer attempt, skill projection, session mutation, or device sequence was committed.

- [x] **Step 2: Run the new snapshot regression.**

Run:

```bash
./.venv/bin/pytest tests/test_pg_sync.py -k snapshot_pg_failure -q
```

Expected result: it fails because `_facts_summary()` currently converts the PostgreSQL exception into an empty fact list.

### Task 3: Add deletion lock-release regressions

**Files:**
- Modify: `tests/test_pg_deletion.py`

- [x] **Step 1: Add a two-connection invalid-state probe.**

Leave a deletion row in `requested`, invoke `complete_index_deletion()` and capture its `ValueError`, then use a second tenant connection and thread to acquire the same advisory lock and execute `SELECT ... FOR UPDATE` on the deletion row. Assert the probe completes promptly and the original error is preserved.

- [x] **Step 2: Add a verification-failure probe.**

Use an index whose verification method raises, then run the same second-connection probe and assert the original verification exception is preserved.

- [x] **Step 3: Run the deletion regressions before production changes.**

Run:

```bash
./.venv/bin/pytest tests/test_pg_deletion.py -k \
  'complete_index_deletion and (lock or verification)' -q
```

Expected result: the row-lock probe blocks or times out against the current implementation.

### Task 4: Implement the minimal production fixes

**Files:**
- Modify: `app/sync/service.py`
- Modify: `app/memory/deletion.py`

- [x] **Step 1: Define explicit sync event validation.**

Add `EventValidationError` and make savepoint rejection catch only that type. Remove the generic built-in exception tuple from `_is_rejectable_event_error`; unexpected application/data exceptions propagate to the outer transaction.

- [x] **Step 2: Enforce session ownership.**

Add a tenant-scoped, row-locking helper that checks the existing session owner against the authoritative envelope student. Raise `EventValidationError` on mismatch. Add authoritative `student_id` and tenant predicates to session/attempt reads and updates, pass `student_id` into `_transition_session`, and scope the snapshot session query.

- [x] **Step 3: Preserve separate versioned-scoring handling.**

Leave `QuestionVersionError` mapping isolated in `_apply_answer_submitted`; it must not become a generic event-domain rejection.

- [x] **Step 4: Make snapshot facts authoritative.**

Remove the broad `try/except Exception` from `_facts_summary()` so PostgreSQL failures propagate through `process_batch()` and rollback the outer transaction.

- [x] **Step 5: Rollback all exceptional deletion completion paths.**

Wrap the entire `complete_index_deletion()` body in a `BaseException` handler that attempts `connection.rollback()` and preserves the primary exception if rollback itself fails. Keep successful commits unchanged.

### Task 5: Verify all requested gates

- [x] **Step 1: Run all Task 2 focused suites.**
- [x] **Step 2: Run resolver/schema, auth, and connection tests.**
- [x] **Step 3: Run `py_compile` for changed modules.**
- [x] **Step 4: Run `git diff --check`.**
- [x] **Step 5: Inspect status/diff and confirm no commit and no Task 3/4 changes.**
