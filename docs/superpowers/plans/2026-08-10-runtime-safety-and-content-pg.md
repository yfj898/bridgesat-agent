# Runtime Safety And Content PostgreSQL Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make deletion, sync, memory outbox, and optional Mnemis paths safe across PostgreSQL processes, then finish the PostgreSQL content-registry migration so the complete test suite is runnable.

**Architecture:** PostgreSQL remains authoritative. Student status and a shared PostgreSQL advisory lock coordinate all student-scoped writes; outbox claim tokens fence stale workers; Mnemis remains derived and deletion completion is conservative when the adapter cannot verify emptiness. Content publishing uses an admin connection, while runtime/content reads use the non-privileged app connection.

**Tech Stack:** Python 3.11+, FastAPI, psycopg 3, PostgreSQL RLS/advisory locks, pytest, deterministic in-memory Mnemis stub.

---

## Task 1: Schema And Role Contract

**Files:**
- Create: `app/infrastructure/migrations_pg/0014_runtime_safety_contract.py`
- Modify: `app/infrastructure/migration_runner.py`
- Modify: `app/infrastructure/pg.py`
- Modify: `scripts/verify_memory_parity.py`
- Test: `tests/test_pg_migration_runner.py`
- Test: `tests/test_pg_rls.py`
- Test: `tests/test_pg_connect.py`

- [x] **Step 1: Add failing migration assertions**

Add tests that migrate an isolated database and assert:

```python
assert "claim_token" in columns("memory_outbox")
assert "tenant_id" in columns("legacy_mastery_imports")
assert pg.database_version(connection) == 14
assert rls_enabled("legacy_mastery_imports")
assert has_policy("legacy_mastery_imports", "tenant_isolation")
```

Add a test proving an existing `processing` outbox row is normalized to
`pending` with a null claim token by migration 0014.

- [x] **Step 2: Run the migration tests and verify the intended failures**

Run:

```bash
.venv/bin/pytest tests/test_pg_migration_runner.py tests/test_pg_rls.py tests/test_pg_connect.py -q
```

Expected: failures for schema version, missing columns, missing legacy RLS,
and missing processing-row normalization.

- [x] **Step 3: Implement migration 0014**

Implement an idempotent migration with these statements:

```sql
ALTER TABLE memory_outbox ADD COLUMN IF NOT EXISTS claim_token TEXT;
UPDATE memory_outbox
SET status = 'pending', claim_token = NULL
WHERE status = 'processing';

ALTER TABLE legacy_mastery_imports
  ADD COLUMN IF NOT EXISTS tenant_id TEXT;
UPDATE legacy_mastery_imports legacy
SET tenant_id = students.tenant_id
FROM students
WHERE students.id = legacy.student_id
  AND legacy.tenant_id IS NULL;
UPDATE legacy_mastery_imports
SET tenant_id = 'tenant_demo'
WHERE tenant_id IS NULL;
ALTER TABLE legacy_mastery_imports
  ALTER COLUMN tenant_id SET DEFAULT 'tenant_demo',
  ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE legacy_mastery_imports ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON legacy_mastery_imports;
CREATE POLICY tenant_isolation ON legacy_mastery_imports
  USING (tenant_id = current_setting('app.tenant_id', true));
CREATE INDEX IF NOT EXISTS idx_legacy_mastery_imports_tenant
  ON legacy_mastery_imports (tenant_id);
```

Include `legacy_mastery_imports` in the runtime tenant-table list and grant
only the same tenant-scoped privileges as other learner tables. Update
`SCHEMA_VERSION` to `14`.

- [x] **Step 4: Harden effective-role checks**

Make `pg.assert_safe_app_role()` reject roles that are superusers,
`rolbypassrls`, direct owners, or effective members of a tenant-table owner
role. Use `pg_has_role(current_user, c.relowner, 'USAGE')` in the owner query.
`pg.connect()` must rollback the role-validation query before returning and
must close a rejected connection.

Update parity’s role check to use the same helper/logic. Add a fake role
membership regression and an assertion that a returned `pg.connect()` starts
with no open transaction.

- [x] **Step 5: Verify Task 1**

Run:

```bash
.venv/bin/pytest tests/test_pg_migration_runner.py tests/test_pg_rls.py tests/test_pg_connect.py -q
.venv/bin/python -m py_compile app/infrastructure/migrations_pg/0014_runtime_safety_contract.py app/infrastructure/pg.py
git diff --check
```

Expected: all tests pass and schema version is `14`.

## Task 2: Active Student Write Gate And Cross-Process Sync

**Files:**
- Modify: `app/infrastructure/migrations_pg/0014_runtime_safety_contract.py`
- Modify: `app/auth.py`
- Modify: `app/repository.py`
- Modify: `app/main.py`
- Modify: `app/sync/service.py`
- Modify: `app/memory/episode_builder.py`
- Modify: `app/memory/pg_memory.py`
- Modify: `app/memory/outbox.py`
- Modify: `app/memory/deletion.py`
- Test: `tests/test_pg_api.py`
- Test: `tests/test_pg_sync.py`
- Test: `tests/test_pg_memory.py`
- Test: `tests/test_pg_deletion.py`
- Test: `tests/security/test_deletion_propagation.py`

- [x] **Step 1: Write deletion-pending rejection tests**

Create a PG test that requests deletion, then proves all of these fail without
new rows or state changes:

```python
with pytest.raises(ValueError):
    SyncService(connection).register_device(student_id, "blocked")
with pytest.raises(ValueError):
    EpisodeBuilder(connection).build_candidate(**candidate_args)
with pytest.raises(ValueError):
    PGMemory(connection).record_intervention_outcome(**stat_args)
response = client.post("/v1/adapt", headers=auth, json=adapt_payload)
assert response.status_code in {401, 409}
```

Add a concurrent two-connection test: connection A holds the student row lock
while connection B attempts deletion or sync, then assert B observes the
serialized final status rather than writing after `deletion_pending`.

- [x] **Step 2: Run the new tests to confirm the current gaps**

Run:

```bash
.venv/bin/pytest tests/test_pg_api.py tests/test_pg_sync.py tests/test_pg_memory.py tests/test_pg_deletion.py tests/security/test_deletion_propagation.py -q
```

Expected: at least one authenticated/sync write currently succeeds after the
student enters deletion state.

- [x] **Step 3: Update the token resolver and API write paths**

In migration 0014, replace the security-definer resolver body with a join that
requires `students.status = 'active'`. In `StudentRepository.update_mastery`,
update only a tenant-scoped active row and raise a domain error when
`rowcount == 0`. The route dependency should normally reject a
deletion-pending token before this method is reached, while the repository
check protects direct callers.

The `/v1/diagnostics` and `/v1/adapt` routes must preserve their existing
response models while returning the existing unauthorized/conflict response
for an inactive student.

- [x] **Step 4: Serialize sync and device operations with the PG student lock**

Wrap `SyncService.process_batch`, `register_device`, and `revoke_device` in
the shared student advisory lock. Within the lock, use a tenant-scoped
`SELECT status FROM students WHERE id = %s FOR UPDATE`, require `active`, and
keep sequence validation, projection, and device-sequence advancement under
one logical operation. Remove the class-level lock registry as an authority;
if retained, it may only be a bounded optimization.

Use the authoritative request student/device IDs for all queries. Do not allow
`_student_exists()` or `_verify_device()` to accept a deletion-pending row.

- [x] **Step 5: Extend the same guard to memory writes and deletion**

Use `student_advisory_lock()` plus `ensure_active_student()` before:

- `EpisodeBuilder._insert_episode()` and `validate()`;
- `PGMemory.upsert_fact_for_episode()` and `record_intervention_outcome()`;
- outbox enqueue paths that originate from learner writes.

`request_deletion()` must lock and verify an existing active student before
creating `requested`, update the student row, and revoke tokens in one
transaction. `complete_index_deletion()` must accept only
`sqlite_deleted` or `index_deletion_pending`, and must commit the verified
state plus `students.status = 'deleted'` atomically.

- [x] **Step 6: Verify Task 2**

Run:

```bash
.venv/bin/pytest tests/test_pg_api.py tests/test_pg_sync.py tests/test_pg_memory.py tests/test_pg_deletion.py tests/security/test_deletion_propagation.py -q
git diff --check
```

Expected: deletion-pending HTTP, sync, episode, fact, and intervention writes
are rejected; active flows remain green.

## Task 3: Claim-Owned Memory Outbox

**Files:**
- Modify: `app/memory/outbox.py`
- Modify: `app/memory/worker.py`
- Modify: `app/memory/deletion.py`
- Modify: `scripts/rebuild_memory_index.py`
- Modify: `scripts/replay_dead_letter.py`
- Modify: `app/memory/tenant_dispatcher.py`
- Test: `tests/test_pg_outbox.py`
- Test: `tests/test_memory_worker.py`
- Test: `tests/test_scripts.py`
- Test: `tests/test_pg_tenant_dispatcher.py`
- Test: `tests/test_pg_deletion.py`

- [x] **Step 1: Add a stale-worker regression**

Create two connections for one tenant and one pending outbox row. Claim it on
connection A, force the lease due, reclaim it on connection B, then assert:

```python
assert repo_a.complete(row_a.outbox_id, row_a.claim_token) is False
assert repo_b.complete(row_b.outbox_id, row_b.claim_token) is True
assert repo.get(row_b.outbox_id).status == "indexed"
```

Add equivalent failure-transition coverage and a test that a delete claim ends
in `deleted`, not `indexed`.

- [x] **Step 2: Run the regression tests and verify they fail before fencing**

Run:

```bash
.venv/bin/pytest tests/test_pg_outbox.py tests/test_memory_worker.py tests/test_pg_deletion.py -q
```

Expected: the stale worker can currently update the newer row because the
transition predicates do not include claim ownership.

- [x] **Step 3: Carry claim tokens through the repository**

Extend `OutboxRecord` with `claim_token: str | None`. In `claim_due`, generate
one UUID per row and update the row with:

```sql
UPDATE memory_outbox
SET status = 'processing', next_attempt_at = %s, claim_token = %s
WHERE outbox_id = %s
  AND tenant_id = current_setting('app.tenant_id')
```

Change the signatures to:

```python
complete(outbox_id: str, claim_token: str, *, now: str | None = None) -> bool
mark_deleted(outbox_id: str, claim_token: str, *, now: str | None = None) -> bool
mark_failed(outbox_id: str, claim_token: str, error: str, *, now: str | None = None) -> str | None
```

Each transition must require tenant scope, `status = 'processing'`, and the
matching token. Successful terminal transitions clear `claim_token` and
`last_error`; `mark_failed` clears the token and sets `retrying` or
`dead_letter`. Stale transitions return `False`/`None` without changing state.

- [x] **Step 4: Update worker, dispatcher, deletion, and scripts**

Pass each record’s token to the matching repository transition. Use
`row.student_id` for delete operations and reject payload student IDs that do
not match the authoritative row. Treat a stale transition as a bounded
`last_errors` diagnostic, not as a fresh retry. Keep replay filtering to the
IDs reset by that invocation and skip inactive/deleting students.

- [x] **Step 5: Verify Task 3**

Run:

```bash
.venv/bin/pytest tests/test_pg_outbox.py tests/test_memory_worker.py tests/test_scripts.py tests/test_pg_tenant_dispatcher.py tests/test_pg_deletion.py -q
.venv/bin/python -m py_compile app/memory/outbox.py app/memory/worker.py app/memory/deletion.py
git diff --check
```

Expected: stale workers cannot overwrite current claims; all existing worker,
dispatcher, rebuild, replay, and deletion tests pass.

## Task 4: Conservative Mnemis Scope And Deletion

**Files:**
- Modify: `app/memory/deletion.py`
- Modify: `app/memory/mnemis_backend.py`
- Modify: `app/memory/fallback_backend.py`
- Modify: `app/memory/pg_memory.py`
- Modify: `scripts/verify_memory_parity.py`
- Modify: `scripts/replay_dead_letter.py`
- Test: `tests/test_mnemis_backend.py`
- Test: `tests/test_fallback_memory.py`
- Test: `tests/test_pg_deletion.py`
- Test: `tests/security/test_deletion_propagation.py`
- Test: `tests/security/test_memory_poisoning.py`

- [x] **Step 1: Add conservative verification tests**

Add an adapter without `count_episodes`, `count_facts`, or
`verify_student_deleted` and assert:

```python
assert await service.verify_not_retrievable(student_id) is False
assert await service.complete_index_deletion(student_id) is False
assert service.deletion_status(student_id) == "index_deletion_pending"
```

Add a Mnemis response containing a foreign supporting episode ID and assert no
foreign result reaches policy. Add replay coverage for a deleting student.

- [x] **Step 2: Implement conservative verification and scope filtering**

Keep count-based verification for the deterministic stub. For an optional
adapter, call `verify_student_deleted` only when the adapter explicitly exposes
that method; otherwise return `False`. Do not add an HTTP endpoint or invent a
remote response shape.

Before accepting Mnemis results, query tenant-scoped validated episode IDs from
PostgreSQL and retain only results whose supporting IDs are a non-empty subset
of that set for the requested student.

- [x] **Step 3: Verify Task 4**

Run:

```bash
.venv/bin/pytest tests/test_mnemis_backend.py tests/test_fallback_memory.py tests/test_pg_deletion.py tests/security/test_deletion_propagation.py tests/security/test_memory_poisoning.py -q
git diff --check
```

Expected: unsupported remote verification remains pending, and foreign Mnemis
evidence cannot affect policy.

## Task 5: PostgreSQL Content Registry

**Files:**
- Create: `app/infrastructure/migrations_pg/0015_content_registry_contract.py`
- Modify: `app/infrastructure/migration_runner.py`
- Modify: `app/content_pipeline/importing.py`
- Modify: `scripts/import_content_pack.py`
- Modify: `app/knowledge/local_backend.py`
- Modify: `tests/test_content_pipeline.py`
- Modify: `tests/test_pg_migration_runner.py`
- Test: `tests/test_pg_content_pipeline.py`

- [x] **Step 1: Add failing PG importer tests**

Create an isolated PG database, build a minimal approved pack, and call the
importer with an admin connection. Assert item/version/pack/source counts and
repeat-import idempotence. Add a subprocess test that passes a DSN and asserts
stderr contains none of:

```text
PosixPath
object has no attribute 'encode'
INSERT OR REPLACE
```

- [x] **Step 2: Run the content tests to capture the current failure**

Run:

```bash
.venv/bin/pytest tests/test_content_pipeline.py tests/test_pg_content_pipeline.py -q
```

Expected: the current importer passes a `Path` into `pg.connect_admin()` and
fails in psycopg connection parsing.

- [x] **Step 3: Align PG content schema in migration 0015**

Add missing approved-registry fields with `ADD COLUMN IF NOT EXISTS`, retaining
existing rows and deterministic defaults. The migration must cover:

- `content_sources`: source name/type, redistribution and RAG flags, access,
  attribution, maintenance status, verification timestamp;
- `content_items`: schema/domain, stable version, status, license snapshot,
  source lineage, canonical body hash, created timestamp, withdrawal fields;
- `content_item_versions`: JSON body, content hash, created timestamp;
- `content_reviews`: version, reviewer role/id, conclusion, notes, release batch;
- `content_packs`: manifest JSON;
- `content_pack_items`: version.

Revoke `INSERT/UPDATE/DELETE` on shared content tables and `knowledge_fts` from
`bridgesat_app`; retain SELECT for runtime retrieval. The importer will use the
admin connection for publishing and indexing.

- [x] **Step 4: Rewrite importer connection and SQL paths**

Change the public API to:

```python
def import_pack(connection: psycopg.Connection, pack_dir: Path, ...) -> int: ...
```

Use `%s` placeholders, `ON CONFLICT ... DO UPDATE`, `json.dumps`, and one
`pg.transaction(connection)` for the complete pack. The CLI accepts `--db` and
`--admin-db`, validates target identity, migrates with admin, publishes with
admin, and verifies with a clean app connection.

- [x] **Step 5: Verify Task 5**

Run:

```bash
.venv/bin/pytest tests/test_content_pipeline.py tests/test_pg_content_pipeline.py tests/test_pg_migration_runner.py -q
.venv/bin/python -m py_compile app/infrastructure/migrations_pg/0015_content_registry_contract.py app/content_pipeline/importing.py scripts/import_content_pack.py
git diff --check
```

Expected: content import is PG-only, idempotent, transactional, and no longer
accepts a database `Path`.

## Task 6: Full Integration And Regression

**Files:**
- Modify only tests or docs when a verified failure requires it.
- Review: `app/main.py`, `app/request_context.py`, `app/auth.py`, `app/sync`, `app/memory`, `scripts`, `app/content_pipeline`

- [x] **Step 1: Run focused suites sequentially**

Run:

```bash
.venv/bin/pytest tests/test_pg_api.py tests/test_api.py tests/security/ tests/golden/test_two_session_memory.py -q
.venv/bin/pytest tests/test_pg_sync.py tests/test_pg_outbox.py tests/test_memory_worker.py tests/test_pg_deletion.py tests/test_pg_tenant_dispatcher.py -q
.venv/bin/pytest tests/test_content_pipeline.py tests/test_pg_content_pipeline.py tests/test_pg_retrieval.py tests/test_retrieval.py -q
```

Expected: all pass under the project `.venv`; run database-mutating suites
sequentially to avoid shared maintenance-database races.

- [x] **Step 2: Run the complete Python suite**

Run:

```bash
.venv/bin/pytest tests/ -x -q
```

Expected: collection succeeds and there are zero failures. Any failure must be
fixed at its owning fixture/runtime boundary rather than skipped.

- [x] **Step 3: Run static and repository checks**

Run:

```bash
.venv/bin/python -m py_compile app/main.py app/auth.py app/repository.py app/sync/service.py app/memory/outbox.py app/memory/worker.py app/memory/deletion.py app/content_pipeline/importing.py scripts/import_content_pack.py
git diff --check
```

Confirm no runtime PG constructor receives a `Path`, no request path mutates
`app.main` globals, no app-role path publishes shared content, no secrets or
DSNs are printed, and all new migrations are idempotent.

- [x] **Step 4: Review final diff and report scope**

Review all changed files against
`docs/superpowers/specs/2026-08-10-runtime-safety-and-content-pg-design.md`.
Record focused/full test commands and any intentionally deferred external
Mnemis verification capability. Do not commit unless explicitly requested.
