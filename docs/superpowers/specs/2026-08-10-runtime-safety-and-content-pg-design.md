# Runtime Safety And Content PostgreSQL Design

## Status

Approved direction: database-authoritative coordination, conservative Mnemis
deletion completion, and a separate PostgreSQL content-registry migration.

## Context

BridgeSAT now uses PostgreSQL for runtime state, tenant RLS, request-scoped
connections, memory outbox delivery, and optional Mnemis indexing. Focused PG,
API, security, and golden suites pass. The remaining review findings are
cross-process correctness issues rather than isolated test-fixture defects:

- authenticated writes can continue after a student's deletion starts;
- sync sequence validation is protected only by a process-local Python lock;
- outbox leases have no claim ownership, so a stale worker can overwrite a
  newer delivery result;
- the optional Mnemis adapter has no verification API, so deletion must not
  claim `verified` without an observable empty-index proof;
- the content registry still has a SQLite-shaped importer and stale PG schema,
  leaving the full suite red on `PosixPath` connection errors.

## Goals

- Make `active` student status the authoritative write gate for authenticated,
  sync, projection, episode, fact, intervention, and memory-outbox writes.
- Serialize student-scoped operations across processes, not only threads.
- Make outbox completion and failure transitions claim-owner safe.
- Never report Mnemis deletion as verified without an explicit verification
  capability; preserve `index_deletion_pending` when verification is absent.
- Migrate content registry import and tests to PostgreSQL with approved-content
  lineage and review fields intact.
- Preserve local-mode learning behavior and PostgreSQL-authoritative fallback.

## Non-Goals

- Do not add a new external Mnemis HTTP endpoint in this change.
- Do not make live Mnemis availability a prerequisite for the learning loop.
- Do not make parity checks depend on a live Mnemis service; parity remains a
  read-only authoritative-data rebuildability check using a fresh deterministic
  stub.
- Do not change the student-facing API shape except for rejecting writes for
  inactive/deletion-pending students.

## Design

### 1. Student Lifecycle Write Gate

`request_deletion(student_id)` acquires the shared student advisory lock,
locks the student row with `SELECT ... FOR UPDATE`, verifies tenant ownership
and `status = 'active'`, then atomically:

1. inserts or transitions `student_deletions` to `requested`;
2. changes `students.status` to `deletion_pending`;
3. revokes all bearer tokens for the student.

The token resolver must join the student row and return no token for a status
other than `active`. Route-level repositories and `SyncService` retain an
active-row check as defense in depth. Device registration, device validation,
batch processing, mastery updates, projection writes, episode creation and
validation, fact promotion, intervention statistics, and outbox enqueueing
must use the same active-student check while holding the student lock.
The runtime-role guard rejects superusers, `BYPASSRLS` roles, direct or
inherited ownership of tenant tables, and unsafe injected connection
factories.

Deletion states remain ordered:

`requested -> sqlite_deleted -> index_deletion_pending -> verified | failed`.

The final `verified` transition and `students.status = 'deleted'` update occur
in one transaction and require the prior state to be
`sqlite_deleted` or `index_deletion_pending`.

### 2. Cross-Process Sync Serialization

`SyncService.process_batch`, device registration, and device revocation use the
same PostgreSQL student advisory lock as memory and deletion operations. The
lock is session-scoped and never commits or rolls back caller work.

Within the lock, the service checks that the student is active, validates the
device and sequence, applies events, advances the device sequence, and commits
the request transaction. The process-local lock registry is removed or reduced
to a non-authoritative optimization; correctness must not depend on it.

### 3. Outbox Claim Ownership

Migration `0014` adds nullable `claim_token TEXT` to `memory_outbox`. Existing
`processing` rows are normalized to `pending` during the migration so every
live claim has an owner. A worker creates a fresh token when it claims each row
and writes it with `status = 'processing'` and the lease deadline. The
in-memory `OutboxRecord` always carries the token.

Terminal and retry transitions require all of:

- matching `outbox_id`;
- `status = 'processing'`;
- matching `claim_token`;
- matching tenant scope.

`complete`/`mark_deleted` clear `claim_token`, `last_error`, and complete the
row. `mark_failed` increments attempts only for the current claim, clears the
token, and sets `retrying` or `dead_letter`. A stale worker whose conditional
update affects zero rows must not change the newer row state and must record a
bounded diagnostic instead of retrying another student's work.

Student advisory locks remain around claim and external delivery for now so
rebuild/deletion ordering is deterministic. Claim ownership is the database
fence; the lock is the student-level serialization boundary.

### 4. Conservative Mnemis Deletion

`StudentMemoryDeletionService.verify_not_retrievable()` uses an explicit
verification capability only:

- indexes exposing `count_episodes` and `count_facts` may be counted;
- an index may expose a future `verify_student_deleted` capability;
- an index with neither capability is unverifiable and returns `False`.

The current `MnemisMemoryAdapter` gains no invented remote endpoint. Enhanced
deletion therefore remains `index_deletion_pending` until the adapter contract
provides verification. Local mode, with no derived index, may complete after
authoritative deletion.

Mnemis recall results are also filtered against tenant-scoped validated episode
IDs before they can influence policy decisions. Unknown, foreign, or
unvalidated supporting IDs are discarded.

Dead-letter replay skips rows whose student is inactive or has any deletion
state, and reports the skipped rows without re-indexing deleted learners.

### 5. Legacy Table Isolation

Migration `0014` adds `tenant_id` to `legacy_mastery_imports`, backfills it from
the owning student where possible, defaults remaining legacy rows to the
existing demo tenant, and enforces non-null tenant scope. It enables RLS with
the canonical tenant policy and includes the table in runtime tenant guards.
Deletion SQL includes the tenant predicate. This keeps the legacy table
available to the governed runtime without exposing cross-tenant rows.

### 6. Content Registry PostgreSQL Migration

The content importer is split into explicit connection responsibilities:

- migration and writes use an admin PostgreSQL connection;
- verification uses an application PostgreSQL connection with read-only
  queries;
- the importer accepts a connection/DSN, never a database `Path`;
- SQL uses psycopg `%s` parameters and PostgreSQL `ON CONFLICT` clauses;
- pack files remain filesystem inputs and are not stored as database paths.

Migration `0015` aligns the historical PG content-registry tables with the
approved SQLite-era contract: source lineage/license fields, content item
version/body hash fields, review metadata, pack manifest JSON, and pack-item
versions. Existing rows are preserved with deterministic defaults. Import is
transactional and idempotent; a failed pack leaves no partial registry state.

The content pipeline tests use isolated PostgreSQL databases and verify item,
version, pack membership, source lineage, review status, and repeat-import
idempotence.

## Error Handling

- Inactive or deletion-pending writes fail with a domain error mapped to the
  existing unauthorized/conflict behavior at API boundaries.
- A stale outbox claim is a no-op state transition, never a new delivery.
- Missing Mnemis verification keeps deletion pending rather than reporting
  success.
- Any migration or content import failure rolls back its transaction and closes
  connections without masking the primary exception.
- Cleanup failures are logged/suppressed only after preserving the primary
  operation error.

## Verification

Each batch must add focused regression tests before implementation and verify:

- deletion-pending HTTP, sync, projection, and memory writes are rejected;
- two PostgreSQL connections cannot accept conflicting device sequences;
- expired-worker completion cannot overwrite a newer outbox claim;
- replay cannot touch unrelated or deleting students;
- Mnemis adapters without verification remain pending;
- content import has no `Path`/`.encode()` failure and is idempotent;
- focused PG/API/security/golden suites pass;
- `pytest tests/ -x -q`, `py_compile`, and `git diff --check` pass, with only
  explicitly documented non-Task-15 failures allowed during intermediate
  batches.
