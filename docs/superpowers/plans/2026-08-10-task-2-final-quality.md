# Task 2 Final Quality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix fact serialization, authoritative event/skill scoping, typed rejection metadata, and primary-exception preservation without touching Task 3/4.

**Architecture:** Keep the existing outer transaction/savepoint design. Add only explicit scope predicates and typed metadata at existing boundaries; cleanup handlers will attempt all required cleanup while preserving the original exception.

**Tech Stack:** Python, psycopg PostgreSQL, pytest, asyncio.

---

### Task 1: Add failing regressions

**Files:** `tests/test_pg_sync.py`, `tests/test_pg_event_store.py`, `tests/test_pg_deletion.py`

- [x] Add a real `student_memory_facts` row and assert snapshot fact serialization works during a successful batch.
- [x] Add cross-student dependency/duplicate scope coverage and assert ownership rejection metadata is `INVALID_SCHEMA`, non-retryable.
- [x] Add skill-state tenant isolation coverage.
- [x] Add savepoint rollback/release cleanup tests preserving the primary error.
- [x] Add deletion finalization rollback-failure coverage preserving the primary error.
- [x] Run all new regressions and confirm red.

### Task 2: Implement minimal fixes

**Files:** `app/sync/service.py`, `app/infrastructure/event_store.py`, `app/memory/deletion.py`

- [x] Serialize actual `MemoryFact` fields.
- [x] Add tenant/student scope to `learning_event_exists()` and SyncService duplicate/dependency calls.
- [x] Add tenant predicates to skill SELECT/UPDATE while retaining the schema-supported conflict target.
- [x] Add `code` and `retryable` to `EventValidationError`; map ownership errors to `INVALID_SCHEMA`/false.
- [x] Preserve primary exceptions in savepoint and finalization cleanup.

### Task 3: Verify

- [x] Run Task 2 focused suites.
- [x] Run schema/auth/connect tests.
- [x] Run `py_compile` and `git diff --check`.
- [x] Confirm no commit and no Task 3/4 changes.
