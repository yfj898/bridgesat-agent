"""Tenant-aware PostgreSQL outbox dispatch.

The tenant boundary assertions in this module intentionally use real
PostgreSQL application-role connections. Only the derived index and the
connection wrapper are test doubles: the former makes failure isolation
deterministic, and the latter records rollback/close without changing the
underlying RLS behavior.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
import threading
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.infrastructure import pg
from app.infrastructure.learner_store import LearnerStore
from app.infrastructure.migration_runner import migrate_database
from app.memory.outbox import MAX_ATTEMPTS, OutboxRepository, utc_now_iso
from app.memory.tenant_dispatcher import TenantOutboxDispatcher
from tests.pg_test_helpers import cleanup_tenant, unique_tenant_id


@pytest.fixture(scope="module")
def migrated_database() -> None:
    admin = pg.connect_admin()
    try:
        migrate_database(admin)
    finally:
        admin.close()


@pytest.fixture()
def tenant_cleanup(migrated_database: None):
    tenants: list[str] = []
    yield tenants
    cleanup = pg.connect_admin()
    try:
        for tenant_id in tenants:
            cleanup_tenant(cleanup, tenant_id)
    finally:
        cleanup.close()


class TrackedConnection:
    """Instrument a real app-role connection without replacing its behavior."""

    def __init__(self, connection: psycopg.Connection) -> None:
        self.connection = connection
        self.rollback_count = 0
        self.close_count = 0
        self.closed = False

    def commit(self) -> Any:
        return self.connection.commit()

    def rollback(self) -> Any:
        self.rollback_count += 1
        return self.connection.rollback()

    def close(self) -> Any:
        self.close_count += 1
        self.closed = True
        return self.connection.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.connection, name)


class SetupFailingConnection(TrackedConnection):
    """Real app connection that fails at one tenant setup operation."""

    def __init__(self, connection: psycopg.Connection, failure_point: str) -> None:
        super().__init__(connection)
        self.failure_point = failure_point

    def execute(self, query: Any, params: Any = None) -> Any:
        if self.failure_point == "set_config" and "set_config('app.tenant_id'" in str(query):
            raise RuntimeError("set_config failure")
        return self.connection.execute(query, params)

    def commit(self) -> Any:
        if self.failure_point == "commit":
            raise RuntimeError("commit failure")
        return super().commit()


class RecordingIndex:
    def __init__(
        self,
        connection: TrackedConnection,
        tenant_id: str,
        calls: list[dict[str, str]],
    ) -> None:
        self.connection = connection
        self.tenant_id = tenant_id
        self.calls = calls

    async def upsert_episode(self, payload: dict, idempotency_key: str) -> None:
        current = self.connection.execute(
            "SELECT current_setting('app.tenant_id', true) AS tenant_id"
        ).fetchone()["tenant_id"]
        self.calls.append(
            {
                "factory_tenant": self.tenant_id,
                "session_tenant": str(current),
                "student_id": str(payload["student_id"]),
                "idempotency_key": idempotency_key,
            }
        )


def _seed_outbox(tenant_id: str, *, suffix: str = "one") -> tuple[str, str]:
    connection = pg.connect()
    try:
        connection.execute(
            "SELECT set_config('app.tenant_id', %s, false)", (tenant_id,)
        )
        connection.commit()
        student_id, _ = LearnerStore(connection).create_student("Ari", 20, 1200)
        outbox_id = OutboxRepository(connection).enqueue(
            connection,
            student_id=student_id,
            aggregate_type="episode",
            aggregate_id=f"episode-{tenant_id}-{suffix}",
            operation="upsert_episode",
            payload={
                "student_id": student_id,
                "episode_id": f"episode-{tenant_id}-{suffix}",
            },
            version=1,
        )
        connection.commit()
        return student_id, outbox_id
    finally:
        connection.rollback()
        connection.close()


def _tracked_factory(
    opened: list[TrackedConnection],
) -> Callable[[], TrackedConnection]:
    def factory() -> TrackedConnection:
        connection = TrackedConnection(pg.connect())
        opened.append(connection)
        return connection

    return factory


def test_empty_outbox_returns_zero(migrated_database: None) -> None:
    admin = pg.connect_admin()
    opened: list[TrackedConnection] = []

    def unexpected_connection() -> TrackedConnection:
        raise AssertionError("empty outbox must not open an app connection")

    try:
        dispatcher = TenantOutboxDispatcher(
            admin,
            unexpected_connection,
            lambda connection, tenant_id: AssertionError("no index expected"),
        )
        assert dispatcher.tenant_ids() == []
        assert dispatcher.run_once() == 0
        assert dispatcher.last_errors == {}
        assert dispatcher.processed_tenants == []
        assert opened == []
    finally:
        admin.rollback()
        admin.close()


def test_tenant_discovery_only_returns_claimable_statuses(
    tenant_cleanup: list[str],
) -> None:
    claimable = {
        status: unique_tenant_id(f"task4_discovery_{status}")
        for status in ("pending", "retrying", "deletion_pending", "processing")
    }
    terminal = {
        status: unique_tenant_id(f"task4_discovery_{status}")
        for status in ("indexed", "dead_letter", "deleted")
    }
    tenant_cleanup.extend((*claimable.values(), *terminal.values()))
    for tenant_id in (*claimable.values(), *terminal.values()):
        _seed_outbox(tenant_id)

    admin = pg.connect_admin()
    try:
        for status, tenant_id in (*claimable.items(), *terminal.items()):
            if status == "pending":
                continue
            admin.execute(
                "UPDATE memory_outbox SET status = %s WHERE tenant_id = %s",
                (status, tenant_id),
            )
        admin.commit()

        dispatcher = TenantOutboxDispatcher(
            admin,
            lambda: pytest.fail("terminal-only tenants must not open app connections"),
            lambda connection, tenant_id: object(),
        )
        discovered = set(dispatcher.tenant_ids())
        assert set(claimable.values()) <= discovered
        assert not discovered.intersection(terminal.values())
    finally:
        admin.rollback()
        admin.close()


def test_two_tenants_are_processed_under_their_own_rls_context(
    tenant_cleanup: list[str],
) -> None:
    tenant_a = unique_tenant_id("task4_dispatch_a")
    tenant_b = unique_tenant_id("task4_dispatch_b")
    tenant_cleanup.extend((tenant_a, tenant_b))
    student_a, outbox_a = _seed_outbox(tenant_a)
    student_b, outbox_b = _seed_outbox(tenant_b)

    admin = pg.connect_admin()
    opened: list[TrackedConnection] = []
    calls: list[dict[str, str]] = []
    factory_connections: dict[str, TrackedConnection] = {}

    def index_factory(connection: TrackedConnection, tenant_id: str) -> RecordingIndex:
        factory_connections[tenant_id] = connection
        return RecordingIndex(connection, tenant_id, calls)

    try:
        dispatcher = TenantOutboxDispatcher(
            admin,
            _tracked_factory(opened),
            index_factory,
        )
        assert dispatcher.run_once() == 2
        assert dispatcher.processed_tenants == [tenant_a, tenant_b]
        assert dispatcher.last_errors == {}
        assert len(calls) == 2
        assert calls[0]["factory_tenant"] == tenant_a
        assert calls[0]["session_tenant"] == tenant_a
        assert calls[0]["student_id"] == student_a
        assert calls[1]["factory_tenant"] == tenant_b
        assert calls[1]["session_tenant"] == tenant_b
        assert calls[1]["student_id"] == student_b
        assert {call["student_id"] for call in calls} == {student_a, student_b}
        assert {call["factory_tenant"] for call in calls} == {tenant_a, tenant_b}
        assert factory_connections[tenant_a] is not factory_connections[tenant_b]

        rows = admin.execute(
            "SELECT tenant_id, outbox_id, status FROM memory_outbox "
            "WHERE outbox_id IN (%s, %s) ORDER BY tenant_id",
            (outbox_a, outbox_b),
        ).fetchall()
        assert rows == [
            {"tenant_id": tenant_a, "outbox_id": outbox_a, "status": "indexed"},
            {"tenant_id": tenant_b, "outbox_id": outbox_b, "status": "indexed"},
        ]
    finally:
        admin.rollback()
        admin.close()

    assert len(opened) == 2
    assert all(connection.rollback_count >= 1 for connection in opened)
    assert all(connection.close_count == 1 for connection in opened)
    assert all(connection.closed for connection in opened)


def test_one_tenant_failure_does_not_stop_another(
    tenant_cleanup: list[str],
) -> None:
    failed_tenant = unique_tenant_id("task4_dispatch_a_failure")
    healthy_tenant = unique_tenant_id("task4_dispatch_b_healthy")
    tenant_cleanup.extend((failed_tenant, healthy_tenant))
    _, failed_outbox = _seed_outbox(failed_tenant)
    healthy_student, healthy_outbox = _seed_outbox(healthy_tenant)

    admin = pg.connect_admin()
    opened: list[TrackedConnection] = []
    calls: list[dict[str, str]] = []

    def index_factory(connection: TrackedConnection, tenant_id: str) -> RecordingIndex:
        if tenant_id == failed_tenant:
            raise RuntimeError("index failure")
        return RecordingIndex(connection, tenant_id, calls)

    try:
        dispatcher = TenantOutboxDispatcher(
            admin,
            _tracked_factory(opened),
            index_factory,
        )
        assert dispatcher.run_once() == 1
        assert dispatcher.last_errors == {failed_tenant: "index failure"}
        assert dispatcher.processed_tenants == [healthy_tenant]
        assert len(calls) == 1
        assert calls[0]["factory_tenant"] == healthy_tenant
        assert calls[0]["session_tenant"] == healthy_tenant
        assert calls[0]["student_id"] == healthy_student

        rows = admin.execute(
            "SELECT tenant_id, outbox_id, status FROM memory_outbox "
            "WHERE outbox_id IN (%s, %s) ORDER BY tenant_id",
            (failed_outbox, healthy_outbox),
        ).fetchall()
        assert rows == [
            {
                "tenant_id": failed_tenant,
                "outbox_id": failed_outbox,
                "status": "pending",
            },
            {
                "tenant_id": healthy_tenant,
                "outbox_id": healthy_outbox,
                "status": "indexed",
            },
        ]
    finally:
        admin.rollback()
        admin.close()

    assert len(opened) == 2
    assert all(connection.rollback_count >= 1 for connection in opened)
    assert all(connection.close_count == 1 for connection in opened)
    assert all(connection.closed for connection in opened)


def test_delivery_failure_is_reported_without_changing_retry_state_machine(
    tenant_cleanup: list[str],
) -> None:
    tenant_id = unique_tenant_id("task4_delivery_failure")
    tenant_cleanup.append(tenant_id)
    _, outbox_id = _seed_outbox(tenant_id)

    admin = pg.connect_admin()
    opened: list[TrackedConnection] = []
    session_tenants: list[str] = []

    class FailingIndex:
        def __init__(self, connection: TrackedConnection) -> None:
            self.connection = connection

        async def upsert_episode(self, payload: dict, idempotency_key: str) -> None:
            current = self.connection.execute(
                "SELECT current_setting('app.tenant_id', true) AS tenant_id"
            ).fetchone()["tenant_id"]
            session_tenants.append(str(current))
            raise RuntimeError("derived index failure")

    def app_factory() -> TrackedConnection:
        connection = TrackedConnection(pg.connect())
        opened.append(connection)
        return connection

    try:
        dispatcher = TenantOutboxDispatcher(
            admin,
            app_factory,
            lambda connection, current_tenant: FailingIndex(connection),
        )
        for attempt in range(MAX_ATTEMPTS + 1):
            assert dispatcher.run_once() == 0
            assert dispatcher.last_errors == {tenant_id: "derived index failure"}
            assert dispatcher.processed_tenants == []

            row = admin.execute(
                "SELECT status, attempt_count FROM memory_outbox WHERE outbox_id = %s",
                (outbox_id,),
            ).fetchone()
            assert row["attempt_count"] == attempt + 1
            assert row["status"] == (
                "dead_letter" if attempt == MAX_ATTEMPTS else "retrying"
            )
            admin.rollback()
            if attempt < MAX_ATTEMPTS:
                admin.execute(
                    "UPDATE memory_outbox SET next_attempt_at = %s "
                    "WHERE outbox_id = %s",
                    (utc_now_iso(), outbox_id),
                )
                admin.commit()
    finally:
        admin.rollback()
        admin.close()

    assert session_tenants == [tenant_id] * (MAX_ATTEMPTS + 1)
    assert len(opened) == MAX_ATTEMPTS + 1
    assert all(connection.rollback_count >= 1 for connection in opened)
    assert all(connection.close_count == 1 for connection in opened)
    assert all(connection.closed for connection in opened)


def test_mixed_delivery_batch_counts_only_successful_rows(
    tenant_cleanup: list[str],
) -> None:
    tenant_id = unique_tenant_id("task4_mixed_delivery")
    tenant_cleanup.append(tenant_id)
    _, successful_outbox = _seed_outbox(tenant_id, suffix="success")
    _, failed_outbox = _seed_outbox(tenant_id, suffix="failure")

    admin = pg.connect_admin()
    opened: list[TrackedConnection] = []

    class MixedIndex:
        async def upsert_episode(self, payload: dict, idempotency_key: str) -> None:
            if str(payload["episode_id"]).endswith("-failure"):
                raise RuntimeError("mixed delivery failure")

    def app_factory() -> TrackedConnection:
        connection = TrackedConnection(pg.connect())
        opened.append(connection)
        return connection

    try:
        dispatcher = TenantOutboxDispatcher(
            admin,
            app_factory,
            lambda connection, current_tenant: MixedIndex(),
        )
        assert dispatcher.run_once() == 1
        assert dispatcher.last_errors == {tenant_id: "mixed delivery failure"}
        assert dispatcher.processed_tenants == []

        rows = admin.execute(
            "SELECT outbox_id, status FROM memory_outbox "
            "WHERE outbox_id IN (%s, %s)",
            (successful_outbox, failed_outbox),
        ).fetchall()
        assert {row["outbox_id"]: row["status"] for row in rows} == {
            successful_outbox: "indexed",
            failed_outbox: "retrying",
        }
    finally:
        admin.rollback()
        admin.close()

    assert len(opened) == 1
    assert opened[0].rollback_count >= 1
    assert opened[0].close_count == 1
    assert opened[0].closed is True


def test_async_delivery_failure_keeps_tenant_context_and_cleans_connection(
    tenant_cleanup: list[str],
) -> None:
    tenant_id = unique_tenant_id("task4_async_delivery_failure")
    tenant_cleanup.append(tenant_id)
    _, outbox_id = _seed_outbox(tenant_id)

    admin = pg.connect_admin()
    opened: list[TrackedConnection] = []
    session_tenants: list[str] = []

    class FailingIndex:
        def __init__(self, connection: TrackedConnection) -> None:
            self.connection = connection

        async def upsert_episode(self, payload: dict, idempotency_key: str) -> None:
            current = self.connection.execute(
                "SELECT current_setting('app.tenant_id', true) AS tenant_id"
            ).fetchone()["tenant_id"]
            session_tenants.append(str(current))
            raise RuntimeError("async derived index failure")

    def app_factory() -> TrackedConnection:
        connection = TrackedConnection(pg.connect())
        opened.append(connection)
        return connection

    try:
        dispatcher = TenantOutboxDispatcher(
            admin,
            app_factory,
            lambda connection, current_tenant: FailingIndex(connection),
        )
        assert asyncio.run(dispatcher.run_pending_async()) == 0
        assert dispatcher.last_errors == {tenant_id: "async derived index failure"}
        assert dispatcher.processed_tenants == []

        row = admin.execute(
            "SELECT status, attempt_count FROM memory_outbox WHERE outbox_id = %s",
            (outbox_id,),
        ).fetchone()
        assert row == {"status": "retrying", "attempt_count": 1}
    finally:
        admin.rollback()
        admin.close()

    assert session_tenants == [tenant_id]
    assert len(opened) == 1
    assert opened[0].rollback_count >= 1
    assert opened[0].close_count == 1
    assert opened[0].closed is True


def test_async_dispatch_offloads_sync_run_and_waits_for_cancellation() -> None:
    admin = _LifecycleAdmin()
    dispatcher = TenantOutboxDispatcher(
        admin,
        lambda: object(),
        lambda connection, tenant_id: object(),
    )
    entered = threading.Event()
    release = threading.Event()
    thread_ids: list[int] = []
    main_thread_id = threading.get_ident()

    def blocking_run_once() -> int:
        thread_ids.append(threading.get_ident())
        entered.set()
        assert release.wait(5)
        return 7

    dispatcher.run_once = blocking_run_once  # type: ignore[method-assign]

    async def exercise() -> None:
        task = asyncio.create_task(dispatcher.run_pending_async())
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert task.done() is False
        assert admin.close_count == 0
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        dispatcher.close()

    asyncio.run(exercise())
    assert thread_ids and thread_ids[0] != main_thread_id
    assert admin.close_count == 1


@pytest.mark.parametrize("failure_point", ["set_config", "commit"])
def test_tenant_setup_failure_rolls_back_and_closes_connection(
    tenant_cleanup: list[str],
    failure_point: str,
) -> None:
    tenant_id = unique_tenant_id(f"task4_setup_{failure_point}")
    tenant_cleanup.append(tenant_id)
    _seed_outbox(tenant_id)

    admin = pg.connect_admin()
    opened: list[SetupFailingConnection] = []

    def app_factory() -> SetupFailingConnection:
        connection = SetupFailingConnection(pg.connect(), failure_point)
        opened.append(connection)
        return connection

    try:
        dispatcher = TenantOutboxDispatcher(
            admin,
            app_factory,
            lambda connection, current_tenant: RecordingIndex(
                connection, current_tenant, []
            ),
        )
        assert dispatcher.run_once() == 0
        assert dispatcher.last_errors[tenant_id] == f"{failure_point} failure"
        assert dispatcher.processed_tenants == []
    finally:
        admin.rollback()
        admin.close()

    assert opened
    assert all(connection.rollback_count >= 1 for connection in opened)
    assert all(connection.close_count == 1 for connection in opened)
    assert all(connection.closed for connection in opened)


def test_async_dispatch_processes_one_bounded_batch(
    tenant_cleanup: list[str],
) -> None:
    tenant_id = unique_tenant_id("task4_dispatch_async")
    tenant_cleanup.append(tenant_id)
    _seed_outbox(tenant_id, suffix="one")
    _seed_outbox(tenant_id, suffix="two")

    admin = pg.connect_admin()
    opened: list[TrackedConnection] = []
    calls: list[dict[str, str]] = []
    try:
        dispatcher = TenantOutboxDispatcher(
            admin,
            _tracked_factory(opened),
            lambda connection, current_tenant: RecordingIndex(
                connection, current_tenant, calls
            ),
        )
        import asyncio

        assert asyncio.run(dispatcher.run_pending_async()) == 2
        assert len(calls) == 2
    finally:
        admin.rollback()
        admin.close()


def test_local_mode_does_not_create_dispatcher_or_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRIDGESAT_MODE", "local")

    def unexpected_admin_connection() -> None:
        raise AssertionError("local mode must not open an admin dispatcher connection")

    monkeypatch.setattr(main_module.pg, "connect_admin", unexpected_admin_connection)
    application = main_module.create_app(lambda: object(), run_migrations=False)

    with TestClient(application) as client:
        assert client.get("/health").status_code == 200
        assert application.state.memory_worker is None
        assert application.state.memory_worker_task is None


def test_memory_drain_loop_logs_poll_errors(monkeypatch, caplog) -> None:
    class FailingDispatcher:
        last_errors = {"tenant_a": "index failure"}

        async def run_pending_async(self) -> int:
            raise RuntimeError("poll failure")

    async def stop_sleep(_delay: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(main_module.asyncio, "sleep", stop_sleep)
    with caplog.at_level(logging.ERROR, logger=main_module.__name__):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(main_module._memory_drain_loop(FailingDispatcher()))

    assert "poll failure" in caplog.text
    assert "tenant_a" in caplog.text


def test_enhanced_lifespan_wires_and_closes_tenant_dispatcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRIDGESAT_MODE", "enhanced")
    admin = _LifecycleAdmin()
    app_connection_factory = lambda: object()
    captured: dict[str, Any] = {}

    monkeypatch.setattr(main_module.pg, "connect_admin", lambda: admin)

    def fake_build_index(connection: object) -> object:
        captured["index_connection"] = connection
        return object()

    monkeypatch.setattr(main_module, "build_mnemis_index", fake_build_index)

    class FakeDispatcher:
        def __init__(self, admin_connection, connection_factory, index_factory) -> None:
            captured["admin"] = admin_connection
            captured["connection_factory"] = connection_factory
            captured["index_factory"] = index_factory
            self.poll_count = 0
            self.closed = False

        async def run_pending_async(self) -> int:
            self.poll_count += 1
            await asyncio.sleep(60)
            return 0

        def close(self) -> None:
            self.closed = True
            admin.rollback()
            admin.close()

    monkeypatch.setattr(main_module, "TenantOutboxDispatcher", FakeDispatcher)
    application = main_module.create_app(app_connection_factory, run_migrations=False)

    with TestClient(application):
        dispatcher = application.state.memory_worker
        assert isinstance(dispatcher, FakeDispatcher)
        assert captured["admin"] is admin
        assert captured["connection_factory"] is app_connection_factory
        assert captured["index_factory"]("connection", "tenant_a") is not None
        assert captured["index_connection"] == "connection"
        assert application.state.memory_worker_task is not None

    assert dispatcher.closed is True
    assert admin.rollback_count == 1
    assert admin.close_count == 1


class _LifecycleAdmin:
    def __init__(self) -> None:
        self.rollback_count = 0
        self.close_count = 0

    def rollback(self) -> None:
        self.rollback_count += 1

    def close(self) -> None:
        self.close_count += 1
