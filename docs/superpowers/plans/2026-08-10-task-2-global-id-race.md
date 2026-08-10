# Task 2 Global ID Race Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Classify concurrent foreign event/attempt ID collisions after savepoint cleanup without masking same-student or unrelated unique violations.

**Architecture:** Keep pre-insert owner probes and the existing savepoint boundary. Add a narrow `UniqueViolation` handler after the savepoint context exits; re-probe tenant-scoped owners and convert only confirmed foreign global-ID races to the existing typed event rejection.

**Tech Stack:** Python, psycopg PostgreSQL, pytest, threading.

---

### Task 1: Add the barrier regression

**Files:** `tests/test_pg_sync.py`

- [x] Create two same-tenant students and connections, synchronize both batches on a barrier immediately before their shared event insert, and use distinct valid sibling IDs.
- [x] Assert one shared event wins, the other is rejected as non-retryable `INVALID_SCHEMA`, both sibling events commit, and both device sequences advance.
- [x] Run the race test and verify the current implementation fails with `UniqueViolation` in the losing worker.

### Task 2: Implement post-savepoint race classification

**Files:** `app/sync/service.py`

- [x] Import `UniqueViolation` and catch it only after `_event_savepoint` has rolled back/released.
- [x] Re-probe tenant-scoped event and attempt owners for the envelope.
- [x] Convert only a different-student owner to `EventValidationError(INVALID_SCHEMA, retryable=False)` and reuse event-list rollback/rejection handling.
- [x] Re-raise same-student/no-collision/unrelated unique violations unchanged.

### Task 3: Verify

- [x] Run the race regression and all Task 2 focused suites.
- [x] Run schema/auth/connect tests.
- [x] Run `py_compile` and `git diff --check`.
- [x] Confirm no commit and no Task 3/4 changes.
