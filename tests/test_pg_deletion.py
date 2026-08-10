"""PostgreSQL deletion/rebuild serialization tests."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
import threading

import pytest

from app.auth import TokenStore
from app.infrastructure import pg
from app.infrastructure.learner_store import LearnerStore
from app.infrastructure.migration_runner import migrate_database
from app.memory import deletion
from app.memory.deletion import StudentMemoryDeletionService
from app.memory.mnemis_stub import InMemoryMnemisIndex
from app.memory.outbox import student_advisory_lock
from tests.pg_test_helpers import cleanup_tenant, unique_tenant_id


@pytest.fixture()
def env() -> tuple[object, str, str]:
    admin = pg.connect_admin()
    try:
        migrate_database(admin)
    finally:
        pg.quiet_close(admin)
    tenant_id = unique_tenant_id("task5_pg_deletion")
    connection = pg.connect()
    connection.execute(
        "SELECT set_config('app.tenant_id', %s, false)", (tenant_id,)
    )
    connection.commit()
    student_id, _ = LearnerStore(connection).create_student("Ari", 20, 1200)
    yield connection, student_id, tenant_id
    pg.quiet_close(connection)
    cleanup = pg.connect_admin()
    try:
        cleanup_tenant(cleanup, tenant_id)
    finally:
        pg.quiet_close(cleanup)


def test_request_and_sqlite_deletion_hold_the_student_lock(
    env: tuple[object, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    connection, student_id, _ = env
    events: list[tuple[str, str]] = []

    @contextmanager
    def recording_lock(connection_arg: object, locked_student_id: str):
        events.append(("enter", locked_student_id))
        try:
            yield
        finally:
            events.append(("exit", locked_student_id))

    monkeypatch.setattr(deletion, "student_advisory_lock", recording_lock)
    service = StudentMemoryDeletionService(connection)
    service.request_deletion(student_id)
    service.execute_sqlite_deletion(student_id)

    assert events == [
        ("enter", student_id),
        ("exit", student_id),
        ("enter", student_id),
        ("exit", student_id),
    ]


def test_complete_index_deletion_holds_async_student_lock(
    env: tuple[object, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    connection, student_id, _ = env
    events: list[tuple[str, str]] = []

    @asynccontextmanager
    async def recording_lock(connection_arg: object, locked_student_id: str):
        events.append(("enter", locked_student_id))
        try:
            yield
        finally:
            events.append(("exit", locked_student_id))

    monkeypatch.setattr(deletion, "student_advisory_lock_async", recording_lock)
    service = StudentMemoryDeletionService(connection, index=None)
    service.request_deletion(student_id)
    service.execute_sqlite_deletion(student_id)

    assert asyncio.run(service.complete_index_deletion(student_id)) is True
    assert events == [("enter", student_id), ("exit", student_id)]


def test_complete_index_deletion_serializes_nested_worker_lock(
    env: tuple[object, str, str]
) -> None:
    connection, student_id, _ = env
    index = InMemoryMnemisIndex()
    asyncio.run(
        index.upsert_episode(
            {"student_id": student_id, "episode_id": "delete-me"},
            "seed-delete-me",
        )
    )
    service = StudentMemoryDeletionService(connection, index=index)
    service.request_deletion(student_id)
    service.execute_sqlite_deletion(student_id)

    assert asyncio.run(service.complete_index_deletion(student_id)) is True
    assert service.deletion_status(student_id) == "verified"
    assert asyncio.run(index.count_episodes(student_id)) == 0


def test_complete_index_deletion_requires_a_legal_pending_state(
    env: tuple[object, str, str]
) -> None:
    connection, student_id, _ = env
    service = StudentMemoryDeletionService(connection, index=None)

    with pytest.raises(ValueError, match="sqlite_deleted"):
        asyncio.run(service.complete_index_deletion(student_id))

    assert service.deletion_status(student_id) is None
    assert connection.execute(
        "SELECT status FROM students WHERE id = %s", (student_id,)
    ).fetchone()["status"] == "active"


def _assert_deletion_row_lock_released(
    student_id: str,
    tenant_id: str,
) -> None:
    other = pg.connect()
    other.execute(
        "SELECT set_config('app.tenant_id', %s, false)", (tenant_id,)
    )
    other.execute("SET lock_timeout = '250ms'")
    other.commit()
    started = threading.Event()
    finished = threading.Event()
    errors: list[BaseException] = []

    def probe() -> None:
        started.set()
        try:
            with student_advisory_lock(other, student_id):
                other.execute(
                    """
                    SELECT state
                    FROM student_deletions
                    WHERE student_id = %s
                      AND tenant_id = current_setting('app.tenant_id', true)
                    FOR UPDATE
                    """,
                    (student_id,),
                ).fetchone()
                other.rollback()
        except BaseException as exc:  # pragma: no cover - asserted by caller
            errors.append(exc)
            try:
                other.rollback()
            except BaseException:
                pass
        finally:
            finished.set()

    thread = threading.Thread(target=probe, daemon=True)
    thread.start()
    assert started.wait(timeout=2)
    assert finished.wait(timeout=2), "second connection remained blocked by a row lock"
    thread.join(timeout=2)
    try:
        assert not errors, errors
    finally:
        pg.quiet_close(other)


def test_complete_index_deletion_rolls_back_invalid_state_before_releasing_locks(
    env: tuple[object, str, str]
) -> None:
    connection, student_id, tenant_id = env
    service = StudentMemoryDeletionService(connection, index=None)
    service.request_deletion(student_id)

    with pytest.raises(ValueError, match="sqlite_deleted") as error:
        asyncio.run(service.complete_index_deletion(student_id))

    assert str(error.value).startswith("complete_index_deletion requires state")
    _assert_deletion_row_lock_released(student_id, tenant_id)


def test_complete_index_deletion_rolls_back_verification_failure_before_releasing_locks(
    env: tuple[object, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection, student_id, tenant_id = env
    service = StudentMemoryDeletionService(connection, index=object())
    service.request_deletion(student_id)
    service._set_state(student_id, "sqlite_deleted")

    async def skip_worker(self, **kwargs):  # noqa: ANN001, ANN003
        return 0

    async def fail_verification(_student_id: str) -> bool:
        raise RuntimeError("verification query failed")

    monkeypatch.setattr(deletion.OutboxWorker, "run_pending_async", skip_worker)
    monkeypatch.setattr(service, "verify_not_retrievable", fail_verification)

    with pytest.raises(RuntimeError, match="verification query failed"):
        asyncio.run(service.complete_index_deletion(student_id))

    _assert_deletion_row_lock_released(student_id, tenant_id)


def test_request_deletion_requires_owned_active_student(
    env: tuple[object, str, str]
) -> None:
    connection, student_id, _ = env
    service = StudentMemoryDeletionService(connection)

    with pytest.raises(ValueError, match="tenant"):
        service.request_deletion("unknown-student")

    connection.execute(
        "UPDATE students SET status = 'inactive' WHERE id = %s", (student_id,)
    )
    connection.commit()
    with pytest.raises(ValueError, match="active"):
        service.request_deletion(student_id)
    assert service.deletion_status(student_id) is None


def test_request_deletion_revokes_all_student_tokens_atomically(
    env: tuple[object, str, str]
) -> None:
    connection, student_id, _ = env
    token_store = TokenStore(connection)
    first = token_store.issue(student_id)
    second = token_store.issue(student_id)

    StudentMemoryDeletionService(connection).request_deletion(student_id)

    assert token_store.resolve(first) is None
    assert token_store.resolve(second) is None
    assert connection.execute(
        "SELECT status FROM students WHERE id = %s", (student_id,)
    ).fetchone()["status"] == "deletion_pending"
    assert connection.execute(
        "SELECT state FROM student_deletions WHERE student_id = %s", (student_id,)
    ).fetchone()["state"] == "requested"


def test_request_deletion_waits_for_another_connection_student_lock(
    env: tuple[object, str, str]
) -> None:
    connection, student_id, tenant_id = env
    other = pg.connect()
    other.execute(
        "SELECT set_config('app.tenant_id', %s, false)", (tenant_id,)
    )
    other.commit()
    started = threading.Event()
    finished = threading.Event()
    errors: list[BaseException] = []

    def request() -> None:
        started.set()
        try:
            StudentMemoryDeletionService(other).request_deletion(student_id)
        except BaseException as exc:  # pragma: no cover - asserted by caller
            errors.append(exc)
        finally:
            finished.set()

    try:
        with student_advisory_lock(connection, student_id):
            thread = threading.Thread(target=request, daemon=True)
            thread.start()
            assert started.wait(timeout=2)
            assert not finished.wait(timeout=0.25)
            connection.execute(
                "UPDATE students SET status = 'inactive' WHERE id = %s", (student_id,)
            )
            connection.commit()
        thread.join(timeout=5)
        assert not thread.is_alive()
    finally:
        pg.quiet_close(other)

    assert len(errors) == 1
    assert isinstance(errors[0], ValueError)
    assert connection.execute(
        "SELECT COUNT(*) AS total FROM student_deletions WHERE student_id = %s",
        (student_id,),
    ).fetchone()["total"] == 0


def test_verified_finalization_rolls_back_atomically_on_student_update_failure(
    env: tuple[object, str, str]
) -> None:
    connection, student_id, _ = env
    service = StudentMemoryDeletionService(connection, index=None)
    service.request_deletion(student_id)
    service.execute_sqlite_deletion(student_id)

    class FailingFinalizationConnection:
        def __init__(self, real_connection) -> None:  # noqa: ANN001
            self.real_connection = real_connection

        def execute(self, query, params=None, **kwargs):  # noqa: ANN001
            if "status = 'deleted'" in " ".join(str(query).split()).lower():
                raise RuntimeError("student finalization failure")
            return self.real_connection.execute(query, params, **kwargs)

        def __getattr__(self, name: str):
            return getattr(self.real_connection, name)

    failing = FailingFinalizationConnection(connection)
    failing_service = StudentMemoryDeletionService(failing, index=None)
    with pytest.raises(RuntimeError, match="finalization failure"):
        asyncio.run(failing_service.complete_index_deletion(student_id))

    row = connection.execute(
        "SELECT state FROM student_deletions WHERE student_id = %s",
        (student_id,),
    ).fetchone()
    assert row["state"] == "sqlite_deleted"
    assert connection.execute(
        "SELECT status FROM students WHERE id = %s", (student_id,)
    ).fetchone()["status"] == "deletion_pending"


def test_verified_finalization_preserves_primary_error_when_rollback_fails(
    env: tuple[object, str, str]
) -> None:
    connection, student_id, _ = env
    service = StudentMemoryDeletionService(connection, index=None)
    service.request_deletion(student_id)
    service.execute_sqlite_deletion(student_id)

    class RollbackFailingFinalizationConnection:
        def __init__(self, real_connection) -> None:  # noqa: ANN001
            self.real_connection = real_connection

        def execute(self, query, params=None, **kwargs):  # noqa: ANN001
            if "status = 'deleted'" in " ".join(str(query).split()).lower():
                raise RuntimeError("student finalization failure")
            return self.real_connection.execute(query, params, **kwargs)

        def rollback(self):
            raise RuntimeError("rollback cleanup failure")

        def __getattr__(self, name: str):
            return getattr(self.real_connection, name)

    failing = RollbackFailingFinalizationConnection(connection)
    failing_service = StudentMemoryDeletionService(failing, index=None)
    with pytest.raises(RuntimeError, match="student finalization failure"):
        asyncio.run(failing_service.complete_index_deletion(student_id))


def test_request_deletion_preserves_primary_error_when_rollback_fails(
    env: tuple[object, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection, student_id, _ = env

    class RollbackFailingConnection:
        def rollback(self) -> None:
            connection.rollback()
            raise RuntimeError("rollback cleanup failure")

        def __getattr__(self, name: str):
            return getattr(connection, name)

    failing_connection = RollbackFailingConnection()
    service = StudentMemoryDeletionService(failing_connection, index=None)

    def fail_state(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("deletion request failure")

    monkeypatch.setattr(service, "_set_state", fail_state)

    with pytest.raises(RuntimeError, match="deletion request failure"):
        service.request_deletion(student_id)


def test_sqlite_deletion_preserves_primary_error_when_rollback_fails(
    env: tuple[object, str, str],
) -> None:
    connection, student_id, _ = env
    StudentMemoryDeletionService(connection, index=None).request_deletion(student_id)

    class RollbackFailingConnection:
        def rollback(self) -> None:
            connection.rollback()
            raise RuntimeError("rollback cleanup failure")

        def __getattr__(self, name: str):
            return getattr(connection, name)

    class FailingOutbox:
        def enqueue(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise RuntimeError("sqlite deletion failure")

    failing_connection = RollbackFailingConnection()
    service = StudentMemoryDeletionService(
        failing_connection,
        index=None,
        outbox=FailingOutbox(),
    )

    with pytest.raises(RuntimeError, match="sqlite deletion failure"):
        service.execute_sqlite_deletion(student_id)


def test_state_update_preserves_primary_error_when_rollback_fails(
    env: tuple[object, str, str],
) -> None:
    connection, student_id, _ = env

    class CommitRollbackFailingConnection:
        def commit(self) -> None:
            raise RuntimeError("state commit failure")

        def rollback(self) -> None:
            connection.rollback()
            raise RuntimeError("rollback cleanup failure")

        def __getattr__(self, name: str):
            return getattr(connection, name)

    failing_connection = CommitRollbackFailingConnection()
    service = StudentMemoryDeletionService(failing_connection, index=None)

    with pytest.raises(RuntimeError, match="state commit failure"):
        service._set_state(student_id, "requested")


class VerifyCapableOnlyIndex:
    """An adapter that can delete and verify but exposes no count methods."""

    async def delete_student(self, student_id, idempotency_key) -> None:  # noqa: ANN001
        return None

    async def verify_student_deleted(self, student_id) -> bool:  # noqa: ANN001
        return True


class BareDeleteOnlyIndex:
    """An adapter that can only delete; verification capability is absent."""

    async def delete_student(self, student_id, idempotency_key) -> None:  # noqa: ANN001
        return None


def test_complete_uses_explicit_verification_capability_when_exposed(
    env: tuple[object, str, str],
) -> None:
    """An optional adapter that exposes verify_student_deleted must be asked
    instead of being refused by the count-only stub path."""
    connection, student_id, _ = env
    service = StudentMemoryDeletionService(connection, index=VerifyCapableOnlyIndex())
    service.request_deletion(student_id)
    service.execute_sqlite_deletion(student_id)

    assert asyncio.run(service.verify_not_retrievable(student_id)) is True
    assert asyncio.run(service.complete_index_deletion(student_id)) is True
    assert service.deletion_status(student_id) == "verified"


def test_complete_stays_index_deletion_pending_without_any_verification_capability(
    env: tuple[object, str, str],
) -> None:
    """Without count methods or verify_student_deleted, remote verification
    is impossible, so deletion must remain conservatively pending."""
    connection, student_id, _ = env
    service = StudentMemoryDeletionService(connection, index=BareDeleteOnlyIndex())
    service.request_deletion(student_id)
    service.execute_sqlite_deletion(student_id)

    assert asyncio.run(service.verify_not_retrievable(student_id)) is False
    assert asyncio.run(service.complete_index_deletion(student_id)) is False
    assert service.deletion_status(student_id) == "index_deletion_pending"
