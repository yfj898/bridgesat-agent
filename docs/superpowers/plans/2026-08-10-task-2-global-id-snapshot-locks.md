# Task 2 Global ID and Snapshot Lock Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reject foreign event/attempt ID collisions and foreign dependencies per event while serializing the standalone snapshot route with student sync/deletion operations.

**Architecture:** Use the existing typed `EventValidationError` and per-event savepoints. Ownership probes remain tenant-scoped; same-student duplicate behavior stays unchanged. The snapshot route will mirror `process_batch()` with the shared advisory lock and an outer transaction joining `build_snapshot()`.

**Tech Stack:** Python, FastAPI, psycopg PostgreSQL, pytest, threading.

---

### Task 1: Add red regressions

**Files:** `tests/test_pg_sync.py`, `tests/test_pg_api.py`

- [x] Add mixed-batch foreign learning-event ID and foreign attempt-ID tests, asserting non-retryable `INVALID_SCHEMA`, valid sibling acceptance, and no foreign-event mutation.
- [x] Update the foreign dependency test to expect non-retryable `INVALID_SCHEMA`; retain missing dependency coverage as retryable.
- [x] Add a two-connection snapshot route lock test proving the snapshot query does not run while the student advisory lock is held.
- [x] Run the new tests and verify the current implementation fails.

### Task 2: Implement minimal fixes

**Files:** `app/sync/service.py`, `app/sync/router.py`

- [x] Add a tenant-scoped event owner probe before learning-event insertion.
- [x] Add a tenant-scoped attempt owner probe before answer-attempt insertion.
- [x] Add dependency owner inspection distinguishing foreign IDs from missing IDs.
- [x] Wrap the snapshot route in `student_advisory_lock` and `transaction`, calling `build_snapshot(..., in_transaction=True)`.

### Task 3: Verify

- [x] Run all Task 2 focused suites and schema/auth/connect tests.
- [x] Run `py_compile` and `git diff --check`.
- [x] Confirm no commit and no Task 3/4 changes.
