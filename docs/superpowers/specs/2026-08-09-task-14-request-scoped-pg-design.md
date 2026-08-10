# Task 14: Request-Scoped PostgreSQL Runtime

## Status

Approved design direction: request-scoped PostgreSQL connections with a
tenant-aware background dispatcher.

## Problem

The PG storage modules now accept `psycopg.Connection`, but the application
entrypoint, sync router, optional memory paths, and performance scripts still
pass SQLite `Path` values. The current module-level state also cannot safely
carry tenant context across concurrent requests. API tests and security tests
must exercise the same tenant boundaries as the PG repositories.

## Goals

- Remove SQLite `Path` construction from runtime API wiring and background
  memory delivery.
- Keep one isolated PG connection per HTTP request.
- Resolve bearer-token tenant identity before tenant-scoped queries and set
  `app.tenant_id` on the request connection.
- Preserve existing public routes and authentication status codes.
- Dispatch memory outbox work tenant by tenant without bypassing RLS in the
  application role.
- Migrate API, security, fallback, and performance tests to the PG runtime.

## Non-Goals

- Changing the PG schema or RLS policy definitions.
- Moving the content registry tables out of SQLite; that remains a separate
  data-migration concern, while knowledge retrieval uses its PG index.
- Adding a connection-pool dependency before request-scoped correctness is
  established.

## Runtime Design

### Application Factory

`create_app()` owns the application wiring. Startup uses a short-lived admin
connection for migrations, then each request obtains an app-role connection.
Global `DATABASE_PATH`, SQLite repositories, and path-based worker
construction are removed from the API path.

### Request Context

Middleware creates `request.state.connection` and a request-scoped
`TokenStore`. It parses an optional bearer token and calls the security-definer
`resolve_token()` function before tenant-scoped reads. A valid token sets
`app.tenant_id` to the resolved tenant. Requests without a token use the
configured default tenant (`BRIDGESAT_DEFAULT_TENANT`, default `tenant_demo`)
so public student creation can insert through RLS.

Route dependencies construct `StudentRepository`, `SyncService`,
`KnowledgeBackend`, and memory facades from the request connection. The
existing `require_student` behavior remains: missing or invalid credentials
return 401, cross-student sync requests retain their 403 behavior, and unknown
students retain their 404 behavior. Middleware always rolls back and closes
the connection in a `finally` path, including cleanup failures.

### Background Memory Dispatch

The worker uses a privileged dispatcher only to obtain the set of tenant IDs.
For each tenant it opens an app-role connection, sets `app.tenant_id`, creates
the configured PG memory/outbox adapter, processes one bounded batch, rolls
back, and closes the connection. A tenant failure is isolated and does not
prevent other tenants from being serviced. Derived Mnemis/Nvidia/fallback
adapters remain optional; PG memory remains authoritative when they are
unavailable or time out.

### Scripts

Performance and rebuild scripts use PG connections and explicit migration
setup. Temporary evaluation state is isolated and cleaned up rather than
passing a filesystem path into a PG constructor.

## Error and Trust Boundaries

- Token resolution occurs through the existing security-definer function and
  never relies on an already-set tenant.
- Tenant-scoped queries run only after `app.tenant_id` is set.
- Every request and tenant worker iteration ends with rollback and close.
- Invalid tokens cannot select a tenant; protected dependencies still return
  401.
- A request body student ID must match the token-scoped student ID before sync
  processing.
- Optional memory/index errors are recorded by the existing bounded outbox
  retry state machine and never replace authoritative PG state.

## Verification

- New `tests/test_pg_api.py` covers app creation, public student creation,
  token authentication, tenant isolation, and sync ownership.
- Existing API and security fixtures are migrated to request-scoped PG setup.
- New worker tests cover tenant dispatch, isolation, rollback, and one-tenant
  failure recovery.
- Fallback and performance tests verify PG connection construction and safe
  degradation.
- Run targeted PG/API/security tests, then `pytest tests/ -x -q`.

## Open Implementation Constraint

The current route modules use the module-level `app.main.token_store` and
`app.main.repository` globals. They must be replaced with request-scoped
dependencies or request-state adapters; retaining mutable global tenant state
is not an acceptable shortcut.
