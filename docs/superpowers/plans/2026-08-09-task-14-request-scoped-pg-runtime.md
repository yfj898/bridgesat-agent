# Task 14 Request-Scoped PG Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace path-based runtime wiring with request-scoped PostgreSQL connections, tenant-safe API execution, tenant-aware memory dispatch, and PG-backed fallback/scripts without changing public API behavior.

**Architecture:** An application factory owns migration and dependency wiring. A request middleware opens one app-role connection, resolves the bearer token through `resolve_token()`, sets `app.tenant_id`, exposes the connection through `request.state`, then always rolls it back and closes it. A privileged background dispatcher enumerates tenants only, while each tenant batch runs through a separate RLS-scoped app connection.

**Tech Stack:** Python 3.11+, FastAPI, Starlette middleware, psycopg 3, PostgreSQL RLS, pytest, FastAPI TestClient.

---

## File Map

- Create `app/request_context.py`: request connection factory, default tenant, tenant middleware, and request-state accessors.
- Create `app/memory/tenant_dispatcher.py`: bounded per-tenant outbox dispatch using admin tenant discovery and app-role tenant connections.
- Create `tests/test_pg_api.py`: HTTP-level PG authentication, tenant isolation, and sync ownership tests.
- Modify `app/main.py`: app factory, PG migration startup, request middleware, request-scoped route dependencies, and tenant dispatcher lifespan.
- Modify `app/auth.py`: make `require_student` resolve the request-scoped `TokenStore` instead of importing module globals.
- Modify `app/knowledge/router.py`: reuse the request connection rather than opening a second connection.
- Modify `app/sync/router.py`: construct `SyncService` from the request connection rather than caching a path-based service.
- Modify `app/agent/orchestrator.py`: accept a psycopg connection and use `PGMemory`/PG `EpisodeBuilder`.
- Modify `app/memory/fallback_backend.py`: accept a connection and use authoritative `PGMemory`.
- Modify `app/memory/__init__.py`: build optional derived memory adapters from a PG connection.
- Modify `app/memory/nvidia_backend.py`: remove SQLite path/schema usage and select candidates from PG episode/fact tables.
- Modify `scripts/seed_demo.py`, `scripts/run_performance_evals.py`, `scripts/rebuild_memory_index.py`, `scripts/replay_dead_letter.py`, `scripts/run_memory_ablation.py`, and `scripts/verify_memory_parity.py`: use explicit PG connections and tenant setup.
- Modify `tests/conftest.py` and `tests/security/conftest.py`: shared PG setup and tenant-scoped seed helpers.
- Modify `tests/test_api.py` and database-dependent `tests/security/*.py`: replace SQLite fixtures with PG connections and request-scoped app instances.

## Task 1: Request Context And App Factory

**Files:**
- Create: `app/request_context.py`
- Create: `tests/test_pg_api.py`
- Modify: `app/main.py`

- [x] **Step 1: Write failing request-context tests**

Add tests that construct the app with a test connection factory and verify the
request connection is isolated and cleaned up:

```python
def test_public_create_uses_default_tenant(client_factory):
    client, connection = client_factory()
    response = client.post(
        "/v1/students",
        json={"name": "Ari", "daily_minutes": 15, "target_score": 1100},
    )
    assert response.status_code == 201
    row = connection.execute(
        "SELECT tenant_id FROM students WHERE id = %s",
        (response.json()["id"],),
    ).fetchone()
    assert row["tenant_id"] == "tenant_demo"


def test_request_connection_is_closed_after_response(client_factory):
    client, connection = client_factory()
    assert client.get("/health").status_code == 200
    assert connection.closed is True
```

- [x] **Step 2: Run the new tests and verify the expected failure**

Run: `pytest tests/test_pg_api.py -q`

Expected: FAIL during collection or app construction because `create_app` and
request-scoped connection handling do not exist yet.

- [x] **Step 3: Implement `app/request_context.py`**

Define these concrete interfaces:

```python
ConnectionFactory = Callable[[], psycopg.Connection]
DEFAULT_TENANT = "tenant_demo"

def request_connection(request: Request) -> psycopg.Connection:
    return request.state.connection

def request_token_store(request: Request) -> TokenStore:
    return request.state.token_store

class TenantContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        connection = self.connection_factory()
        request.state.connection = connection
        request.state.token_store = TokenStore(connection)
        try:
            tenant_id = self._tenant_for_request(request)
            connection.execute(
                "SELECT set_config('app.tenant_id', %s, false)",
                (tenant_id,),
            )
            connection.commit()
            return await call_next(request)
        finally:
            try:
                connection.rollback()
            finally:
                connection.close()
```

`_tenant_for_request` must call `TokenStore.resolve_tenant` when a bearer token
exists and use `BRIDGESAT_DEFAULT_TENANT` or `tenant_demo` when the token is
missing or invalid. Invalid protected requests remain failures of
`require_student`, not tenant-selection errors.

- [x] **Step 4: Implement `create_app` in `app/main.py`**

Move module-level migration and route construction behind:

Use `create_app(connection_factory: Callable[[], psycopg.Connection] | None = None, *, run_migrations: bool = True) -> FastAPI` and keep the module-level `app = create_app()` production entrypoint.

Use `pg.connect_admin()` plus `migrate_database()` only when
`run_migrations=True`. Store the app-role connection factory on app state and
register `TenantContextMiddleware` before route inclusion. Do not pass a
`Path` to `apply_migrations`, `StudentRepository`, `TokenStore`, or
`OutboxWorker`.

- [x] **Step 5: Run the request-context tests**

Run: `pytest tests/test_pg_api.py -q`

Expected: the request-context tests pass, including default tenant creation
and connection cleanup.

## Task 2: Request-Scoped Authentication And Routes

**Files:**
- Modify: `app/auth.py`
- Modify: `app/main.py`
- Modify: `app/knowledge/router.py`
- Modify: `app/sync/router.py`
- Test: `tests/test_pg_api.py`

- [x] **Step 1: Add failing authentication and route tests**

Add tests for missing/invalid tokens, tenant isolation, cross-student sync
scope, and request-scoped service construction:

```python
def test_invalid_token_returns_401(client_factory):
    client, _ = client_factory()
    response = client.post("/v1/diagnostics", headers={"Authorization": "Bearer bad"}, json={"answers": []})
    assert response.status_code == 401


def test_cross_tenant_student_is_hidden(client_factory, seed_student):
    token_a, student_a = seed_student("tenant_a")
    _, student_b = seed_student("tenant_b")
    client = client_factory()[0]
    response = client.get(f"/v1/sync/snapshot?student_id={student_b}", headers={"Authorization": f"Bearer {token_a}"})
    assert response.status_code in (403, 404)


def test_sync_body_student_mismatch_is_403(client_factory, seed_student, sync_payload):
    token_a, student_a = seed_student("tenant_a")
    _, student_b = seed_student("tenant_a")
    payload = sync_payload(student_b)
    response = client_factory()[0].post(
        "/v1/sync/events", headers={"Authorization": f"Bearer {token_a}"}, json=payload
    )
    assert response.status_code == 403
```

- [x] **Step 2: Run the tests and confirm the old global-store failure**

Run: `pytest tests/test_pg_api.py -q`

Expected: FAIL because `require_student` imports `app.main.token_store` and
the sync/knowledge routers still construct services from `DATABASE_PATH`.

- [x] **Step 3: Make `require_student` request-scoped**

Change its signature to accept `Request` and use
`request.state.token_store.resolve(token)`. Preserve the existing 401 detail
strings and do not query `students` before token validation.

- [x] **Step 4: Make main route stores request-scoped**

Inject `StudentRepository(request_connection(request))` and
`TokenStore(request_connection(request))` into `/v1/students`, diagnostics,
and adapt routes. The public create route must run after the middleware sets
the default tenant.

- [x] **Step 5: Make sync and knowledge routers reuse request state**

Replace the cached sync service with:

```python
def get_service(request: Request) -> SyncService:
    return SyncService(request_connection(request))
```

Replace `knowledge.router.get_backend()` with a dependency that wraps
`KnowledgeBackend(request_connection(request))` and does not close the
middleware-owned connection. Keep the existing route payload and response
contracts unchanged.

- [x] **Step 6: Run API tests**

Run: `pytest tests/test_pg_api.py tests/test_pg_retrieval.py -q`

Expected: all tests pass with 401/403/404 behavior preserved.

## Task 3: PG Orchestrator And Fallback Memory

**Files:**
- Modify: `app/agent/orchestrator.py`
- Modify: `app/memory/fallback_backend.py`
- Modify: `app/memory/__init__.py`
- Modify: `app/memory/nvidia_backend.py`
- Modify: `tests/test_fallback_memory.py`
- Modify: `tests/test_memory_ablation.py`
- Modify: `tests/security/test_timeout_fallback.py`
- Modify: `tests/security/test_memory_poisoning.py`

- [x] **Step 1: Write failing PG fallback/orchestrator tests**

Add a PG fixture that sets `app.tenant_id`, creates a student, and verifies
that fallback reads use `learning_episodes` rather than opening a SQLite file:

```python
def test_fallback_uses_pg_memory(pg_connection, seeded_episode):
    fallback = FallbackStudentMemory(pg_connection, mnemis=None)
    result = asyncio.run(
        fallback.recall_similar(
            student_id=seeded_episode.student_id,
            skill=seeded_episode.skill,
            misconception=seeded_episode.misconception,
        )
    )
    assert result.route == "pg"
    assert result.hits[0].episode_id == seeded_episode.episode_id
```

Add a constructor test asserting `SessionOrchestrator(pg_connection)` creates
`PGMemory` and `EpisodeBuilder` without a `Path`.

- [x] **Step 2: Run the tests and verify the old Path/SQLite failure**

Run: `pytest tests/test_fallback_memory.py tests/test_memory_ablation.py -q`

Expected: FAIL because the current classes construct `SQLiteMemory(Path)`.

- [x] **Step 3: Convert orchestrator and fallback constructors**

Use these signatures:

```python
class SessionOrchestrator:
    def __init__(self, connection: psycopg.Connection, llm=None) -> None:
        self.connection = connection
        self.events = EventStore(connection)
        self.learner = LearnerStore(connection)
        self.memory = PGMemory(connection)
        self.episodes = EpisodeBuilder(connection)
        self.llm = llm

class FallbackStudentMemory:
    def __init__(self, connection: psycopg.Connection, mnemis=None, *, timeout_ms=None, offline_snapshot=None):
        self.pg = PGMemory(connection)
        self.mnemis = mnemis
        self.timeout_ms = timeout_ms or getattr(mnemis, "timeout_ms", None) or SYSTEM_1_TIMEOUT_MS
        self.offline_snapshot = offline_snapshot
        self._route_counts = {}
        self._latencies = deque(maxlen=200)
```

Keep route labels compatible by changing the authoritative fallback label to
`pg`, and retain `mnemis_system1` and `offline_snapshot` labels. All fallback
exceptions must still return authoritative PG results.

- [x] **Step 4: Replace the optional local Nvidia SQLite store**

Change `NvidiaMemoryIndex(connection, llm=llm)` to use the existing PG
`learning_episodes` and `student_memory_facts` rows as its candidate source.
`/index/upsert` must remain idempotent but must not create a second
authoritative store; the episode/fact write already happened in PG.
`/student/delete` must delete only derived in-memory state and leave
authoritative deletion to `StudentMemoryDeletionService`. Candidate queries
must include `student_id`, `skill`, and optional misconception filters.

- [x] **Step 5: Update memory factory and tests**

Change `build_mnemis_index(connection)` and update all callers. Run:

```bash
pytest tests/test_fallback_memory.py tests/test_memory_ablation.py \
  tests/security/test_timeout_fallback.py tests/security/test_memory_poisoning.py -q
```

Expected: all fallback tests pass, including timeout and unavailable-index
degradation.

## Task 4: Tenant-Aware Outbox Dispatcher

**Files:**
- Create: `app/memory/tenant_dispatcher.py`
- Modify: `app/memory/worker.py` only if a bounded batch hook is needed
- Modify: `app/main.py`
- Create: `tests/test_pg_tenant_dispatcher.py`

- [x] **Step 1: Write failing dispatcher tests**

Cover two tenants, one failing tenant, and cleanup:

```python
def test_dispatcher_processes_each_tenant_with_rls(pg_admin, app_connection_factory, seed_outbox):
    seed_outbox("tenant_a")
    seed_outbox("tenant_b")
    dispatcher = TenantOutboxDispatcher(pg_admin, app_connection_factory, index_factory)
    assert dispatcher.run_once() == 2
    assert indexed_students("tenant_a") == ["student_a"]
    assert indexed_students("tenant_b") == ["student_b"]


def test_one_tenant_failure_does_not_stop_other_tenants(pg_admin, app_connection_factory, seed_outbox):
    seed_outbox("tenant_a")
    seed_outbox("tenant_b")
    failed = {"tenant_a"}
    dispatcher = TenantOutboxDispatcher(
        pg_admin,
        app_connection_factory,
        lambda tenant_id: FailingIndex(tenant_id) if tenant_id in failed else RecordingIndex(tenant_id),
    )
    assert dispatcher.run_once() == 1
    assert dispatcher.last_errors == {"tenant_a": "index failure"}
    assert dispatcher.processed_tenants == ["tenant_b"]
```

The concrete assertions must verify `app.tenant_id` inside the index adapter,
not merely the number of processed rows.

- [x] **Step 2: Run the dispatcher tests and verify missing implementation**

Run: `pytest tests/test_pg_tenant_dispatcher.py -q`

Expected: FAIL because `TenantOutboxDispatcher` does not exist.

- [x] **Step 3: Implement bounded tenant dispatch**

Implement `TenantOutboxDispatcher.__init__(admin_connection, app_connection_factory, index_factory)`,
`tenant_ids() -> list[str]`, synchronous `run_once() -> int`, and async
`run_pending_async() -> int`. The instance must expose `last_errors: dict[str,
str]` and `processed_tenants: list[str]` so the failure-isolation tests can
assert the exact tenant behavior.

`tenant_ids()` may use the admin connection to read distinct tenant IDs from
`memory_outbox`; each tenant iteration opens an app connection, sets
`app.tenant_id`, commits that setting, constructs `OutboxWorker`, processes
one batch, rolls back, and closes. Catch errors per tenant, continue to the
next tenant, and return the total successfully processed rows.

- [x] **Step 4: Wire the dispatcher into lifespan**

In `main.lifespan`, create the dispatcher once, start its existing bounded
poll loop only in enhanced mode, cancel it on shutdown, and close all admin
and app connections. Local mode must leave outbox rows pending and must not
start a derived index.

- [x] **Step 5: Run dispatcher and memory regression tests**

Run: `pytest tests/test_pg_tenant_dispatcher.py tests/test_pg_outbox.py tests/test_pg_memory.py -q`

Expected: all pass.

## Task 5: PG Demo, Rebuild, Ablation, And Performance Scripts

**Files:**
- Modify: `scripts/seed_demo.py`
- Modify: `scripts/run_performance_evals.py`
- Modify: `scripts/rebuild_memory_index.py`
- Modify: `scripts/replay_dead_letter.py`
- Modify: `scripts/run_memory_ablation.py`
- Modify: `scripts/verify_memory_parity.py`
- Modify: `tests/test_scripts.py`

- [x] **Step 1: Add failing script smoke tests**

For each script that currently calls `apply_migrations(Path)` or constructs a
PG store with a `Path`, add a subprocess test with `BRIDGESAT_DB` set to the
test DSN. Assert the command does not contain `PosixPath` or
`'Connection' object has no attribute 'encode'` in stderr.

- [x] **Step 2: Run script tests and record the existing failures**

Run: `pytest tests/test_scripts.py tests/test_memory_ablation.py -q`

Expected: existing path-based constructors fail before the conversion.

- [x] **Step 3: Convert script setup to explicit PG connections**

Every script must follow this setup pattern:

```python
admin = pg.connect_admin()
try:
    migrate_database(admin)
finally:
    admin.close()

connection = pg.connect()
try:
    connection.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant_id,))
    connection.commit()
    # construct LearnerStore, SyncService, PGMemory, or worker here
finally:
    connection.rollback()
    connection.close()
```

Use unique tenant/student/device IDs for independent scenarios. Replace
temporary SQLite files with a clean PG schema lifecycle in test mode. Preserve
the scripts' existing stdout/report formats.

- [x] **Step 4: Run script and evaluation tests**

Run:

```bash
pytest tests/test_scripts.py tests/test_memory_ablation.py -q
python scripts/run_performance_evals.py --samples-policy 20 --samples-fts5 20 --samples-restore 5
```

Expected: script tests pass and the performance command writes a valid JSON
report without constructing any SQLite-backed runtime store.

## Task 6: API And Security Fixture Migration

**Files:**
- Modify: `tests/conftest.py`
- Modify: `tests/security/conftest.py`
- Modify: `tests/test_api.py`
- Modify: database-dependent `tests/security/test_cross_student_isolation.py`, `test_deletion_propagation.py`, `test_forged_offline_events.py`, `test_memory_poisoning.py`, `test_prompt_injection.py`, `test_request_limits.py`, and `test_timeout_fallback.py`

- [x] **Step 1: Add a reusable PG test fixture**

Provide `pg_connection` and `pg_app` fixtures that migrate a clean schema,
set a tenant, construct `create_app(connection_factory=lambda: pg.connect())`,
and drop the schema after rollback/connection close. Do not mutate
`app.main.repository` or `app.main.token_store` globals.

- [x] **Step 2: Convert security seed helpers**

Change `seed_student` inserts to `%s` parameters and explicit `tenant_id`
values through `current_setting('app.tenant_id')`. Use `LearnerStore(connection)`
and `SyncService(connection)`. Replace direct SQLite deletes with transaction-
wrapped PG deletes under the correct tenant.

- [x] **Step 3: Convert API/security tests**

Preserve each test's acceptance behavior: cross-student reads/deletes are
blocked, forged events do not duplicate projections, prompt injection does not
change bounded actions, request limits remain enforced, and timeout fallback
returns PG-backed results.

- [x] **Step 4: Run the migrated API/security suites**

Run:

```bash
pytest tests/test_pg_api.py tests/test_api.py tests/security/ -q
```

Expected: all API and security tests pass without SQLite path errors.

## Task 7: Full Verification And Handoff

**Files:**
- Modify only tests/docs if a verified failure requires a focused correction.

- [x] **Step 1: Run focused PG regressions**

```bash
pytest tests/test_pg_api.py tests/test_pg_tenant_dispatcher.py \
  tests/test_pg_retrieval.py tests/test_pg_memory.py tests/test_pg_outbox.py \
  tests/test_retrieval.py tests/test_api.py tests/security/ -q
```

- [x] **Step 2: Run the complete Python suite**

Run: `pytest tests/ -x -q`

Expected: collection succeeds and the suite has zero failures.

- [x] **Step 3: Check static and repository invariants**

Run:

```bash
.venv/bin/python -m py_compile app/main.py app/auth.py app/request_context.py \
  app/memory/tenant_dispatcher.py app/agent/orchestrator.py
git diff --check
```

- [x] **Step 4: Review the final diff**

Confirm no changed runtime path passes a `Path` to a PG constructor, no test
mutates module-level tenant state, and no secret or database credential is
introduced.

- [x] **Step 5: Commit the scoped Task 14 changes**

```bash
git add app tests scripts docs/superpowers/specs/2026-08-09-task-14-request-scoped-pg-design.md docs/superpowers/plans/2026-08-09-task-14-request-scoped-pg-runtime.md
git commit -m "feat: wire runtime API and memory paths to PostgreSQL"
```
