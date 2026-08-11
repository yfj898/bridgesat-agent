"""Script-level smoke tests for the PostgreSQL memory maintenance scripts."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime
import inspect
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg
import pytest

from app.domain.events import LearningEvent, LearningEventType
from app.infrastructure import pg
from app.infrastructure.learner_store import LearnerStore
from app.infrastructure.migration_runner import SCHEMA_VERSION, migrate_database
from app.memory.episode_builder import EpisodeBuilder
from app.memory.mnemis_stub import InMemoryMnemisIndex
from app.memory.outbox import OutboxRepository
from app.memory.pg_memory import PGMemory
from app.memory.worker import OutboxWorker
from app.memory import outbox as memory_outbox
from app.memory import worker as memory_worker
from app.knowledge.local_backend import index_pack
from app.sync.service import SyncService
from tests.pg_test_helpers import cleanup_tenant, unique_tenant_id
from scripts import (
    rebuild_memory_index,
    replay_dead_letter,
    run_memory_ablation,
    run_performance_evals,
    seed_demo,
    verify_memory_parity,
)


PACKS_ROOT = Path(__file__).resolve().parents[1] / "content" / "packs"
ROOT = Path(__file__).resolve().parents[1]


class FailingIndex(InMemoryMnemisIndex):
    async def upsert_episode(self, episode, idempotency_key):  # noqa: ANN001
        raise RuntimeError("mnemis down")


class DeleteFailsFirstIndex(InMemoryMnemisIndex):
    def __init__(self) -> None:
        super().__init__()
        self.delete_calls = 0
        self.upsert_calls = 0

    async def delete_student(self, student_id, idempotency_key):  # noqa: ANN001
        self.delete_calls += 1
        if self.delete_calls == 1:
            raise RuntimeError("delete unavailable")
        await super().delete_student(student_id, idempotency_key)

    async def upsert_episode(self, episode, idempotency_key):  # noqa: ANN001
        self.upsert_calls += 1
        await super().upsert_episode(episode, idempotency_key)

    async def upsert_fact(self, fact, idempotency_key):  # noqa: ANN001
        self.upsert_calls += 1
        await super().upsert_fact(fact, idempotency_key)


class CountingFailingIndex(InMemoryMnemisIndex):
    def __init__(self) -> None:
        super().__init__()
        self.episode_attempts = 0

    async def upsert_episode(self, episode, idempotency_key):  # noqa: ANN001
        self.episode_attempts += 1
        raise RuntimeError("episode index unavailable")


def _event(session_id: str, event_id: str, student_id: str) -> LearningEvent:
    return LearningEvent(
        event_id=event_id,
        student_id=student_id,
        session_id=session_id,
        event_type=LearningEventType.ANSWER_EVALUATED,
        payload={},
        occurred_at="2026-08-06T10:00:00+00:00",
        received_at="2026-08-06T10:00:00+00:00",
    )


def _seed(env: tuple[object, str, str]) -> tuple[EpisodeBuilder, PGMemory]:
    connection, student_id, _ = env
    builder = EpisodeBuilder(connection)
    memory = PGMemory(connection)
    for session, ep in (("s1", "ep_1"), ("s2", "ep_2")):
        episode = builder.build_candidate(
            student_id=student_id,
            session_id=session,
            skill="linear_equations",
            misconception="sign_error",
            intervention="SHOW_WORKED_EXAMPLE",
            context_event=_event(session, "ctx", student_id),
            evidence_events=[_event(session, "obs", student_id)],
            outcome_event=_event(session, "out", student_id),
            outcome_correct=True,
            outcome_hint_level=0,
            outcome_content_id=f"out_{ep}",
            teaching_content_id="same",
            summary="x",
            episode_id=ep,
        )
        builder.validate(episode)
        episode = builder.get_episode(ep)
        assert episode is not None
        memory.upsert_fact_for_episode(episode)
    return builder, memory


def _count_indexed(index: InMemoryMnemisIndex, student_id: str) -> tuple[int, int]:
    return asyncio.run(index.count_episodes(student_id)), asyncio.run(index.count_facts(student_id))


def _tenant_row_count(tenant_id: str, admin_target: str | None = None) -> int:
    admin = pg.connect_admin(admin_target)
    try:
        return sum(
            int(
                admin.execute(
                    f"SELECT COUNT(*) AS total FROM {table} WHERE tenant_id = %s",
                    (tenant_id,),
                ).fetchone()["total"]
            )
            for table in run_performance_evals.TENANT_CLEANUP_ORDER
        )
    finally:
        try:
            admin.rollback()
        finally:
            admin.close()


def _knowledge_row_count(app_target: str | None = None) -> int:
    connection = pg.connect(app_target)
    try:
        return int(
            connection.execute(
                "SELECT COUNT(*) AS total FROM knowledge_fts"
            ).fetchone()["total"]
        )
    finally:
        try:
            connection.rollback()
        finally:
            connection.close()


def _outbox_snapshot(connection: object, student_id: str) -> list[tuple]:
    rows = connection.execute(
        """
        SELECT outbox_id, aggregate_type, aggregate_id, operation, status,
               attempt_count, next_attempt_at, last_error
        FROM memory_outbox
        WHERE student_id = %s
        ORDER BY outbox_id
        """,
        (student_id,),
    ).fetchall()
    return [
        tuple(row[field] for field in (
            "outbox_id",
            "aggregate_type",
            "aggregate_id",
            "operation",
            "status",
            "attempt_count",
            "next_attempt_at",
            "last_error",
        ))
        for row in rows
    ]


def _parity_state_snapshot(connection: object, student_id: str) -> dict:
    snapshot: dict[str, object] = {}
    for table in (
        "students",
        "learning_episodes",
        "student_memory_facts",
        "memory_outbox",
    ):
        rows = connection.execute(
            f"SELECT * FROM {table} "
            "WHERE tenant_id = current_setting('app.tenant_id')",
        ).fetchall()
        snapshot[table] = sorted(
            json.dumps(dict(row), sort_keys=True, default=str) for row in rows
        )
    snapshot["schema_version"] = connection.execute(
        "SELECT MAX(version) AS version FROM schema_migrations"
    ).fetchone()["version"]
    snapshot["rls_catalog"] = [
        (row["relname"], row["relrowsecurity"], row["has_policy"])
        for row in connection.execute(
            """
            SELECT c.relname, c.relrowsecurity,
                   BOOL_OR(p.polname = 'tenant_isolation') AS has_policy
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            LEFT JOIN pg_policy AS p ON p.polrelid = c.oid
            WHERE n.nspname = 'public'
              AND c.relname IN (
                  'students', 'learning_episodes', 'student_memory_facts',
                  'memory_outbox'
              )
            GROUP BY c.relname, c.relrowsecurity
            ORDER BY c.relname
            """
        ).fetchall()
    ]
    return snapshot


def _dead_letter_episode_rows(connection: object) -> OutboxRepository:
    worker = OutboxWorker(connection, index=FailingIndex())
    for _ in range(6):
        connection.execute(
            "UPDATE memory_outbox SET next_attempt_at = '2020-01-01T00:00:00+00:00'"
        )
        connection.commit()
        worker.run_pending()
    return OutboxRepository(connection)


@pytest.fixture()
def env() -> tuple[object, str, str]:
    admin = pg.connect_admin()
    try:
        migrate_database(admin)
    finally:
        try:
            admin.rollback()
        finally:
            admin.close()

    tenant_id = unique_tenant_id("task5a_scripts")
    connection = pg.connect()
    try:
        connection.execute(
            "SELECT set_config('app.tenant_id', %s, false)", (tenant_id,)
        )
        connection.commit()
        learner = LearnerStore(connection)
        student_id, _ = learner.create_student("Ari", 20, 1200)
        yield connection, student_id, tenant_id
    finally:
        try:
            connection.rollback()
        finally:
            connection.close()
        cleanup = pg.connect_admin()
        try:
            cleanup_tenant(cleanup, tenant_id)
        finally:
            try:
                cleanup.rollback()
            finally:
                cleanup.close()


@pytest.fixture()
def isolated_script_database() -> tuple[str, str, str]:
    database_name = f"bridgesat_task5b_{uuid.uuid4().hex}"
    maintenance_dsn = psycopg.conninfo.make_conninfo(
        pg.admin_dsn(), dbname="postgres"
    )
    admin_dsn = psycopg.conninfo.make_conninfo(
        pg.admin_dsn(), dbname=database_name
    )
    app_dsn = psycopg.conninfo.make_conninfo(
        pg.dsn(), dbname=database_name
    )

    maintenance = psycopg.connect(maintenance_dsn, autocommit=True)
    try:
        maintenance.execute(
            psycopg.sql.SQL("CREATE DATABASE {}")
            .format(psycopg.sql.Identifier(database_name))
        )
    finally:
        maintenance.close()

    try:
        admin = pg.connect_admin(admin_dsn)
        try:
            migrate_database(admin)
        finally:
            try:
                admin.rollback()
            finally:
                admin.close()

        tenant_id = unique_tenant_id("task5b_protected")
        connection = pg.connect(app_dsn)
        try:
            connection.execute(
                "SELECT set_config('app.tenant_id', %s, false)", (tenant_id,)
            )
            connection.commit()
            LearnerStore(connection).create_student("Protected", 20, 1200)
            admin = pg.connect_admin(admin_dsn)
            try:
                with admin.transaction():
                    index_pack(admin, PACKS_ROOT / "bridgesat-math-0.1.0")
            finally:
                admin.close()
        finally:
            try:
                connection.rollback()
            finally:
                connection.close()
        yield admin_dsn, app_dsn, tenant_id
    finally:
        maintenance = psycopg.connect(maintenance_dsn, autocommit=True)
        try:
            maintenance.execute(
                psycopg.sql.SQL("DROP DATABASE {} WITH (FORCE)")
                .format(psycopg.sql.Identifier(database_name))
            )
        finally:
            maintenance.close()


def test_rebuild_uses_pg_connection_and_preserves_authoritative_rows(
    env: tuple[object, str, str]
) -> None:
    connection, student_id, _ = env
    _seed(env)
    assert connection.execute(
        "SELECT COUNT(*) AS total FROM learning_episodes WHERE student_id = %s",
        (student_id,),
    ).fetchone()["total"] == 2

    index = InMemoryMnemisIndex()
    report = rebuild_memory_index.rebuild_student(connection, student_id, index)
    assert report["episodes_enqueued"] == 2
    assert report["facts_enqueued"] == 1
    assert _count_indexed(index, student_id) == (2, 1)
    assert report["delivery"]["deleted"] == 1
    assert report["delivery"]["indexed"] == 3
    assert connection.execute(
        "SELECT COUNT(*) AS total FROM learning_episodes WHERE student_id = %s",
        (student_id,),
    ).fetchone()["total"] == 2


def test_rebuild_rejects_cross_tenant_student_without_side_effect(
    env: tuple[object, str, str]
) -> None:
    connection_a, _, _ = env
    tenant_b = unique_tenant_id("task5a_rebuild_other")
    connection_b = pg.connect()
    try:
        connection_b.execute(
            "SELECT set_config('app.tenant_id', %s, false)", (tenant_b,)
        )
        connection_b.commit()
        student_b, _ = LearnerStore(connection_b).create_student("Bri", 20, 1200)
        repo_b = OutboxRepository(connection_b)
        repo_b.enqueue(
            connection_b,
            student_id=student_b,
            aggregate_type="episode",
            aggregate_id="tenant_b_sentinel",
            operation="upsert_episode",
            payload={"student_id": student_b, "episode_id": "tenant_b_sentinel"},
            version=1,
        )
        connection_b.commit()
        before_a = connection_a.execute(
            "SELECT COUNT(*) AS total FROM memory_outbox"
        ).fetchone()["total"]
        before_b = connection_b.execute(
            "SELECT COUNT(*) AS total FROM memory_outbox"
        ).fetchone()["total"]

        with pytest.raises(ValueError, match="tenant"):
            rebuild_memory_index.rebuild_student(
                connection_a, student_b, InMemoryMnemisIndex()
            )

        assert connection_a.execute(
            "SELECT COUNT(*) AS total FROM memory_outbox"
        ).fetchone()["total"] == before_a
        assert connection_b.execute(
            "SELECT COUNT(*) AS total FROM memory_outbox"
        ).fetchone()["total"] == before_b
        assert repo_b.list_by_status("pending")[0].aggregate_id == "tenant_b_sentinel"
    finally:
        try:
            connection_b.rollback()
        finally:
            connection_b.close()
        cleanup = pg.connect_admin()
        try:
            cleanup_tenant(cleanup, tenant_b)
        finally:
            try:
                cleanup.rollback()
            finally:
                cleanup.close()


def test_rebuild_is_idempotent(env: tuple[object, str, str]) -> None:
    connection, student_id, _ = env
    _seed(env)
    first = InMemoryMnemisIndex()
    rebuild_memory_index.rebuild_student(connection, student_id, first)
    assert _count_indexed(first, student_id) == (2, 1)

    second = InMemoryMnemisIndex()
    rebuild_memory_index.rebuild_student(connection, student_id, second)
    assert _count_indexed(second, student_id) == (2, 1)
    assert connection.execute(
        "SELECT COUNT(*) AS total FROM memory_outbox WHERE student_id = %s",
        (student_id,),
    ).fetchone()["total"] == 4


def test_rebuild_stops_before_upserts_when_delete_delivery_fails(
    env: tuple[object, str, str]
) -> None:
    connection, student_id, _ = env
    _seed(env)
    index = DeleteFailsFirstIndex()

    report = rebuild_memory_index.rebuild_student(connection, student_id, index)

    assert report["episodes_enqueued"] == 0
    assert report["facts_enqueued"] == 0
    assert report["delivery"]["failed"] == 1
    assert report["delivery"]["retrying"] == 1
    assert report["delivery"]["deleted"] == 0
    assert index.delete_calls == 1
    assert index.upsert_calls == 0
    operations = connection.execute(
        "SELECT operation FROM memory_outbox WHERE student_id = %s ORDER BY created_at",
        (student_id,),
    ).fetchall()
    assert [row["operation"] for row in operations] == ["delete_student"]


def test_rebuild_rejects_inactive_student(env: tuple[object, str, str]) -> None:
    connection, student_id, _ = env
    connection.execute(
        "UPDATE students SET status = 'inactive' WHERE id = %s",
        (student_id,),
    )
    connection.commit()

    with pytest.raises(ValueError, match="active"):
        rebuild_memory_index.rebuild_student(
            connection, student_id, InMemoryMnemisIndex()
        )


def test_rebuild_rejects_student_with_any_deletion_state(
    env: tuple[object, str, str]
) -> None:
    connection, student_id, _ = env
    connection.execute(
        """
        INSERT INTO student_deletions (
            deletion_id, tenant_id, student_id, requested_at, state
        ) VALUES (
            'delete_guard', current_setting('app.tenant_id'), %s,
            '2026-08-10T00:00:00+00:00', 'verified'
        )
        """,
        (student_id,),
    )
    connection.commit()

    with pytest.raises(ValueError, match="deletion"):
        rebuild_memory_index.rebuild_student(
            connection, student_id, InMemoryMnemisIndex()
        )


def test_student_advisory_lock_blocks_other_connection(
    env: tuple[object, str, str]
) -> None:
    connection, student_id, tenant_id = env
    other = pg.connect()
    try:
        other.execute(
            "SELECT set_config('app.tenant_id', %s, false)", (tenant_id,)
        )
        other.commit()
        with memory_outbox.student_advisory_lock(connection, student_id):
            acquired = other.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS acquired",
                (memory_outbox.student_lock_key(student_id),),
            ).fetchone()["acquired"]
            assert acquired is False
        acquired_after = other.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS acquired",
            (memory_outbox.student_lock_key(student_id),),
        ).fetchone()["acquired"]
        assert acquired_after is True
        other.execute(
            "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
            (memory_outbox.student_lock_key(student_id),),
        )
    finally:
        pg.quiet_close(other)


def test_student_advisory_lock_preserves_caller_transaction(
    env: tuple[object, str, str]
) -> None:
    connection, student_id, _ = env
    original_name = connection.execute(
        "SELECT name FROM students WHERE id = %s", (student_id,)
    ).fetchone()["name"]
    connection.execute(
        "UPDATE students SET name = %s WHERE id = %s",
        ("uncommitted-lock-write", student_id),
    )

    with memory_outbox.student_advisory_lock(connection, student_id):
        assert connection.execute(
            "SELECT name FROM students WHERE id = %s", (student_id,)
        ).fetchone()["name"] == "uncommitted-lock-write"

    connection.rollback()
    assert connection.execute(
        "SELECT name FROM students WHERE id = %s", (student_id,)
    ).fetchone()["name"] == original_name


def test_worker_acquires_student_lock_around_delivery(
    env: tuple[object, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    connection, student_id, _ = env
    _seed(env)
    events: list[tuple[str, str]] = []

    @contextmanager
    def recording_lock(connection_arg: object, locked_student_id: str):
        events.append(("enter", locked_student_id))
        try:
            yield
        finally:
            events.append(("exit", locked_student_id))

    monkeypatch.setattr(memory_worker, "student_advisory_lock", recording_lock)
    OutboxWorker(connection, index=InMemoryMnemisIndex()).run_pending()
    assert events
    assert events[0] == ("enter", student_id)
    assert events[-1] == ("exit", student_id)


def test_worker_releases_student_lock_on_cancellation(
    env: tuple[object, str, str]
) -> None:
    connection, student_id, tenant_id = env
    _seed(env)

    class CancellingIndex:
        async def upsert_episode(self, episode, idempotency_key):  # noqa: ANN001
            raise asyncio.CancelledError()

        async def upsert_fact(self, fact, idempotency_key):  # noqa: ANN001
            raise asyncio.CancelledError()

    other = pg.connect()
    worker_connection = pg.connect()
    try:
        other.execute(
            "SELECT set_config('app.tenant_id', %s, false)", (tenant_id,)
        )
        other.commit()
        worker_connection.execute(
            "SELECT set_config('app.tenant_id', %s, false)", (tenant_id,)
        )
        worker_connection.commit()
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                OutboxWorker(
                    worker_connection, index=CancellingIndex()
                ).run_pending_async()
            )
        acquired = other.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS acquired",
            (memory_outbox.student_lock_key(student_id),),
        ).fetchone()["acquired"]
        assert acquired is True
        other.execute(
            "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
            (memory_outbox.student_lock_key(student_id),),
        )
    finally:
        pg.quiet_close(other)
        pg.quiet_close(worker_connection)


def test_rebuild_drains_all_outbox_batches(env: tuple[object, str, str]) -> None:
    connection, student_id, _ = env
    builder = EpisodeBuilder(connection)
    for index in range(25):
        session_id = f"multi_session_{index}"
        episode = builder.build_candidate(
            student_id=student_id,
            session_id=session_id,
            skill="linear_equations",
            misconception="sign_error",
            intervention="SHOW_WORKED_EXAMPLE",
            context_event=_event(session_id, f"multi_ctx_{index}", student_id),
            evidence_events=[_event(session_id, f"multi_obs_{index}", student_id)],
            outcome_event=_event(session_id, f"multi_out_{index}", student_id),
            outcome_correct=True,
            outcome_hint_level=0,
            outcome_content_id=f"multi_outcome_{index}",
            teaching_content_id="multi_teaching",
            summary="multi-batch rebuild",
            episode_id=f"multi_episode_{index}",
        )
        builder.validate(episode)

    index = InMemoryMnemisIndex()
    report = rebuild_memory_index.rebuild_student(connection, student_id, index)
    delivery = report["delivery"]
    assert delivery["claimed"] == 26
    assert delivery["successful"] == 26
    assert delivery["failed"] == 0
    assert delivery["pending"] == 0
    assert delivery["deleted"] == 1
    assert delivery["indexed"] == 25
    assert _count_indexed(index, student_id) == (25, 0)


def test_dead_letter_replay_is_connection_scoped_and_idempotent(
    env: tuple[object, str, str]
) -> None:
    connection, student_id, _ = env
    _seed(env)

    repo = _dead_letter_episode_rows(connection)
    assert len(repo.list_by_status("dead_letter")) == 2
    assert repo.list_by_status("pending") == []

    index = InMemoryMnemisIndex()
    report = replay_dead_letter.replay(connection, index, 3)
    assert report["reset_rows"] == 2
    assert report["processed"] == 2
    assert len(repo.list_by_status("indexed")) == 4
    assert _count_indexed(index, student_id) == (2, 0)


def test_replay_max_attempts_are_actual_virtual_time_attempts(
    env: tuple[object, str, str]
) -> None:
    connection, student_id, _ = env
    _seed(env)
    repo = _dead_letter_episode_rows(connection)
    assert len(repo.list_by_status("dead_letter")) == 2
    index = CountingFailingIndex()

    report = replay_dead_letter.replay(
        connection,
        index,
        3,
        now="2026-08-10T00:00:00+00:00",
    )

    assert index.episode_attempts == 6
    assert report["processed"] == 6
    assert report["failed"] == 6
    rows = connection.execute(
        "SELECT attempt_count, status FROM memory_outbox WHERE student_id = %s "
        "AND operation = 'upsert_episode' ORDER BY outbox_id",
        (student_id,),
    ).fetchall()
    assert [(row["attempt_count"], row["status"]) for row in rows] == [
        (3, "retrying"),
        (3, "retrying"),
    ]


def test_replay_does_not_claim_unrelated_pending_rows(
    env: tuple[object, str, str]
) -> None:
    connection, student_id, _ = env
    _seed(env)
    repo = _dead_letter_episode_rows(connection)
    unrelated_id = repo.enqueue(
        connection,
        student_id=student_id,
        aggregate_type="episode",
        aggregate_id="unrelated_pending",
        operation="upsert_episode",
        payload={"student_id": student_id, "episode_id": "unrelated_pending"},
        version=1,
    )
    connection.commit()
    report = replay_dead_letter.replay(
        connection,
        InMemoryMnemisIndex(),
        1,
        now="2026-08-10T00:00:00+00:00",
    )

    assert report["reset_rows"] == 2
    assert report["processed"] == 2
    assert report["indexed"] == 2
    assert connection.execute(
        "SELECT status FROM memory_outbox WHERE outbox_id = %s",
        (unrelated_id,),
    ).fetchone()["status"] == "pending"


def test_replay_resets_dead_letters_under_sorted_student_locks(
    env: tuple[object, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    connection, first_student_id, _ = env
    second_student_id, _ = LearnerStore(connection).create_student("Bea", 20, 1200)
    repo = OutboxRepository(connection)
    for student_id, suffix in (
        (second_student_id, "second"),
        (first_student_id, "first"),
    ):
        repo.enqueue(
            connection,
            student_id=student_id,
            aggregate_type="episode",
            aggregate_id=f"replay_{suffix}",
            operation="upsert_episode",
            payload={"student_id": student_id, "episode_id": f"replay_{suffix}"},
            version=1,
        )
    connection.execute(
        "UPDATE memory_outbox SET status = 'dead_letter' WHERE operation = 'upsert_episode'"
    )
    connection.commit()
    events: list[tuple[str, str]] = []

    @contextmanager
    def recording_lock(connection_arg: object, student_id: str):
        events.append(("enter", student_id))
        try:
            yield
        finally:
            events.append(("exit", student_id))

    monkeypatch.setattr(replay_dead_letter, "student_advisory_lock", recording_lock)
    report = replay_dead_letter.replay(connection, None, 1)

    expected_students = sorted((first_student_id, second_student_id))
    assert report["reset_rows"] == 2
    assert [student for action, student in events if action == "enter"] == expected_students


def test_replay_batch_covers_all_selected_rows_in_one_window(
    env: tuple[object, str, str]
) -> None:
    connection, student_id, _ = env
    repo = OutboxRepository(connection)
    for index in range(21):
        repo.enqueue(
            connection,
            student_id=student_id,
            aggregate_type="episode",
            aggregate_id=f"replay_batch_{index}",
            operation="upsert_episode",
            payload={"student_id": student_id, "episode_id": f"replay_batch_{index}"},
            version=1,
        )
    connection.execute(
        "UPDATE memory_outbox SET status = 'dead_letter' "
        "WHERE tenant_id = current_setting('app.tenant_id')"
    )
    connection.commit()

    index = CountingFailingIndex()
    report = replay_dead_letter.replay(
        connection,
        index,
        1,
        now="2026-08-10T00:00:00+00:00",
    )

    assert report["reset_rows"] == 21
    assert report["processed"] == 21
    assert index.episode_attempts == 21


def test_replay_rejects_cross_tenant_dead_letters_without_side_effect(
    env: tuple[object, str, str]
) -> None:
    _, _, tenant_a = env
    tenant_b = unique_tenant_id("task5a_replay_other")
    connection_b = pg.connect()
    admin_scoped_a = pg.connect_admin()
    try:
        connection_b.execute(
            "SELECT set_config('app.tenant_id', %s, false)", (tenant_b,)
        )
        connection_b.commit()
        student_b, _ = LearnerStore(connection_b).create_student("Bri", 20, 1200)
        repo_b = OutboxRepository(connection_b)
        repo_b.enqueue(
            connection_b,
            student_id=student_b,
            aggregate_type="episode",
            aggregate_id="replay_tenant_b_sentinel",
            operation="upsert_episode",
            payload={"student_id": student_b, "episode_id": "replay_tenant_b_sentinel"},
            version=1,
        )
        connection_b.commit()
        _dead_letter_episode_rows(connection_b)
        assert len(repo_b.list_by_status("dead_letter")) == 1

        admin_scoped_a.execute(
            "SELECT set_config('app.tenant_id', %s, false)", (tenant_a,)
        )
        admin_scoped_a.commit()
        report = replay_dead_letter.replay(
            admin_scoped_a, InMemoryMnemisIndex(), 1
        )
        assert report["reset_rows"] == 0
        assert report["processed"] == 0
        assert len(repo_b.list_by_status("dead_letter")) == 1
    finally:
        try:
            admin_scoped_a.rollback()
        finally:
            admin_scoped_a.close()
        try:
            connection_b.rollback()
        finally:
            connection_b.close()
        cleanup = pg.connect_admin()
        try:
            cleanup_tenant(cleanup, tenant_b)
        finally:
            try:
                cleanup.rollback()
            finally:
                cleanup.close()


def test_replay_is_noop_without_dead_letters(env: tuple[object, str, str]) -> None:
    connection, student_id, _ = env
    _seed(env)
    OutboxWorker(connection, index=InMemoryMnemisIndex()).run_pending()
    report = replay_dead_letter.replay(connection, InMemoryMnemisIndex(), 1)
    assert report == {
        "reset_rows": 0,
        "processed": 0,
        "successful": 0,
        "failed": 0,
        "failures": {},
        "pending": 0,
        "retrying": 0,
        "processing": 0,
        "dead_letter": 0,
        "indexed": 4,
        "deleted": 0,
        "mode": "enhanced",
    }


def test_replay_skips_dead_letters_of_deleting_student(
    env: tuple[object, str, str],
) -> None:
    """Replay must not resurrect delivery intent for a student whose deletion
    is in flight; those rows belong to the deletion protocol, not to replay."""
    connection, student_id, _ = env
    connection.execute("SELECT set_config('app.tenant_id', %s, false)", (env[2],))
    connection.execute(
        "UPDATE students SET status = 'deletion_pending' WHERE id = %s",
        (student_id,),
    )
    connection.commit()
    repo = OutboxRepository(connection)
    outbox_id = repo.enqueue(
        connection,
        student_id=student_id,
        aggregate_type="episode",
        aggregate_id="delete_in_flight",
        operation="upsert_episode",
        payload={"student_id": student_id, "episode_id": "delete_in_flight"},
        version=1,
    )
    connection.execute(
        "UPDATE memory_outbox SET status = 'dead_letter', last_error = %s "
        "WHERE outbox_id = %s",
        ("replay should skip", outbox_id),
    )
    connection.commit()

    report = replay_dead_letter.replay(connection, InMemoryMnemisIndex(), 1)

    assert report["reset_rows"] == 0
    assert report["processed"] == 0
    row = connection.execute(
        "SELECT status FROM memory_outbox WHERE outbox_id = %s", (outbox_id,)
    ).fetchone()
    assert row["status"] == "dead_letter"


def test_local_replay_resets_dead_letters_without_delivery_failure(
    env: tuple[object, str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    connection, _, _ = env
    _seed(env)
    repo = _dead_letter_episode_rows(connection)
    assert len(repo.list_by_status("dead_letter")) == 2

    report = replay_dead_letter.replay(connection, None, 1)
    assert report["reset_rows"] == 2
    assert report["processed"] == 0
    assert report["failed"] == 0
    assert report["pending"] == 2
    assert report["mode"] == "local"
    assert "Local memory mode" in capsys.readouterr().err


def test_rebuild_reports_delivery_failures_and_returns_nonzero(
    env: tuple[object, str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    connection, student_id, tenant_id = env
    _seed(env)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rebuild_memory_index.py",
            "--db",
            pg.dsn(),
            "--tenant",
            tenant_id,
            "--student",
            student_id,
            "--index",
            "adapter",
        ],
    )

    assert rebuild_memory_index.main() == 2
    report = json.loads(capsys.readouterr().out)
    delivery = report["students"][0]["delivery"]
    assert delivery["failed"] > 0
    assert delivery["failures"]
    assert delivery["retrying"] > 0 or delivery["dead_letter"] > 0


def test_replay_reports_delivery_failures_and_returns_nonzero(
    env: tuple[object, str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    connection, _, tenant_id = env
    _seed(env)
    repo = _dead_letter_episode_rows(connection)
    assert len(repo.list_by_status("dead_letter")) == 2
    monkeypatch.setenv("BRIDGESAT_MODE", "enhanced")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "replay_dead_letter.py",
            "--db",
            pg.dsn(),
            "--tenant",
            tenant_id,
            "--max-attempts",
            "1",
        ],
    )

    assert replay_dead_letter.main() == 2
    report = json.loads(capsys.readouterr().out)
    assert report["failed"] > 0
    assert report["failures"]


def test_replay_rejects_nonpositive_max_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["replay_dead_letter.py", "--max-attempts", "0"],
    )
    with pytest.raises(SystemExit) as raised:
        replay_dead_letter.main()
    assert raised.value.code == 2


def test_seed_demo_uses_dsn_and_remains_idempotent(
    env: tuple[object, str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, _, tenant_id = env
    monkeypatch.setenv("BRIDGESAT_PACKS_ROOT", str(PACKS_ROOT))
    from app import question_bank

    question_bank.clear_cache()
    args = ["--db", pg.dsn(), "--tenant", tenant_id]

    assert seed_demo.main(args) == 0
    first_output = capsys.readouterr().out
    assert "Seeded demo student" in first_output
    assert "PosixPath" not in first_output

    assert seed_demo.main(args) == 0
    second_output = capsys.readouterr().out
    assert "already seeded" in second_output


def test_seed_demo_local_mode_leaves_outbox_pending(
    env: tuple[object, str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    connection, _, tenant_id = env
    monkeypatch.setenv("BRIDGESAT_PACKS_ROOT", str(PACKS_ROOT))
    monkeypatch.setenv("BRIDGESAT_MODE", "local")

    assert seed_demo.main(["--db", pg.dsn(), "--tenant", tenant_id]) == 0
    output = capsys.readouterr().out
    pending = connection.execute(
        "SELECT COUNT(*) AS total FROM memory_outbox "
        "WHERE tenant_id = current_setting('app.tenant_id') "
        "AND status = 'pending'"
    ).fetchone()["total"]
    assert pending > 0
    assert "memory outbox left pending" in output
    assert "memory outbox drained" not in output


def test_seed_demo_configures_derived_index_only_in_enhanced_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = object()
    monkeypatch.setattr(seed_demo, "build_mnemis_index", lambda connection: marker)
    monkeypatch.setattr(seed_demo, "memory_mode", lambda: seed_demo.MemoryMode.LOCAL)
    assert seed_demo._configured_index(object()) is None
    monkeypatch.setattr(
        seed_demo, "memory_mode", lambda: seed_demo.MemoryMode.ENHANCED
    )
    assert seed_demo._configured_index(object()) is marker


def test_seed_demo_fails_closed_on_partial_namespace(
    env: tuple[object, str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    connection, _, tenant_id = env
    monkeypatch.setenv("BRIDGESAT_PACKS_ROOT", str(PACKS_ROOT))
    partial_student, _ = LearnerStore(connection).create_student(
        "Demo Student", 20, 1200
    )
    identifiers = seed_demo._demo_identifiers(tenant_id)
    SyncService(connection).register_device(
        partial_student, "demo laptop", device_id=identifiers.device_id
    )
    before_events = connection.execute(
        "SELECT COUNT(*) AS total FROM learning_events"
    ).fetchone()["total"]

    result = seed_demo.main(["--db", pg.dsn(), "--tenant", tenant_id])
    captured = capsys.readouterr()
    assert result == 2
    assert "partial" in captured.err.lower() or "inconsistent" in captured.err.lower()
    assert "Seeded demo student" not in captured.out
    assert connection.execute(
        "SELECT COUNT(*) AS total FROM learning_events"
    ).fetchone()["total"] == before_events


def test_seed_demo_returns_nonzero_on_worker_delivery_failure(
    env: tuple[object, str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, _, tenant_id = env
    monkeypatch.setenv("BRIDGESAT_PACKS_ROOT", str(PACKS_ROOT))

    class FailingWorker:
        def __init__(self, connection: object, index: object | None = None) -> None:
            self.calls = 0
            self.failed_total = 0
            self.last_errors: dict[str, str] = {}

        def run_pending(self, **kwargs: object) -> int:
            self.calls += 1
            if self.calls == 1:
                self.failed_total = 1
                self.last_errors = {"out_demo": "index unavailable"}
                return 1
            self.failed_total = 0
            self.last_errors = {}
            return 0

    monkeypatch.setattr(seed_demo, "OutboxWorker", FailingWorker)
    result = seed_demo.main(["--db", pg.dsn(), "--tenant", tenant_id])
    captured = capsys.readouterr()
    assert result == 2
    assert "delivery failed" in captured.err


def test_seed_demo_identifiers_are_deterministic_and_tenant_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRIDGESAT_PACKS_ROOT", str(PACKS_ROOT))
    first = seed_demo._demo_identifiers("Tenant/A")
    same = seed_demo._demo_identifiers("Tenant/A")
    other = seed_demo._demo_identifiers("Tenant_B")

    assert first == same
    assert first.device_id != other.device_id
    assert first.session_id != other.session_id
    assert first.branch_id != other.branch_id
    assert first.event_prefix != other.event_prefix
    assert first.attempt_prefix != other.attempt_prefix

    first_events = seed_demo._practice_events("student", first)
    other_events = seed_demo._practice_events("student", other)
    assert max(len(event.event_id) for event in first_events) <= 64
    assert all(
        datetime.fromisoformat(event.device_occurred_at)
        for event in first_events
    )
    assert {event.event_id for event in first_events}.isdisjoint(
        event.event_id for event in other_events
    )
    first_attempts = {
        event.payload["attempt_id"]
        for event in first_events
        if event.event_type == "ANSWER_SUBMITTED"
    }
    other_attempts = {
        event.payload["attempt_id"]
        for event in other_events
        if event.event_type == "ANSWER_SUBMITTED"
    }
    assert first_attempts.isdisjoint(other_attempts)


def test_replay_cli_subprocess_uses_dsn_and_returns_json() -> None:
    tenant_id = unique_tenant_id("task5a_replay_cli")
    environment = os.environ.copy()
    environment["BRIDGESAT_MODE"] = "local"
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "replay_dead_letter.py"),
                "--db",
                pg.dsn(),
                "--tenant",
                tenant_id,
                "--max-attempts",
                "1",
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout)
        assert report == {
            "reset_rows": 0,
            "processed": 0,
            "successful": 0,
            "failed": 0,
            "failures": {},
            "pending": 0,
            "retrying": 0,
            "processing": 0,
            "dead_letter": 0,
            "indexed": 0,
            "deleted": 0,
            "mode": "local",
        }
        assert "PosixPath" not in result.stderr
        assert "Connection object" not in result.stderr
        assert "object has no attribute 'encode'" not in result.stderr
    finally:
        cleanup = pg.connect_admin()
        try:
            cleanup_tenant(cleanup, tenant_id)
        finally:
            try:
                cleanup.rollback()
            finally:
                cleanup.close()


def test_rebuild_cli_accepts_dsn_without_path_encode_error(
    env: tuple[object, str, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, student_id, tenant_id = env
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rebuild_memory_index.py",
            "--db",
            pg.dsn(),
            "--tenant",
            tenant_id,
            "--student",
            student_id,
        ],
    )
    assert rebuild_memory_index.main() == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["db"] == "postgresql"
    assert pg.dsn() not in captured.out
    assert "PosixPath" not in captured.err


def test_task5_scripts_accept_explicit_admin_dsn() -> None:
    for module in (seed_demo, rebuild_memory_index, replay_dead_letter):
        source = inspect.getsource(module)
        assert 'parser.add_argument("--admin-db"' in source
        assert "pg.connect_admin(admin_target)" in source
        assert "pg.assert_matching_database" in source


def test_all_task5_scripts_validate_the_effective_app_role() -> None:
    for module in (
        seed_demo,
        rebuild_memory_index,
        replay_dead_letter,
        run_performance_evals,
        run_memory_ablation,
        verify_memory_parity,
    ):
        assert "pg.assert_safe_app_role" in inspect.getsource(module)


def test_evaluation_scripts_use_shared_database_identity_guard() -> None:
    for module in (run_performance_evals, run_memory_ablation):
        source = inspect.getsource(module)
        assert "def _database_identity" not in source
        assert "def _assert_matching_database" not in source
        assert "pg.assert_matching_database" in source


def test_parity_uses_tenant_scoped_direct_authoritative_counts() -> None:
    source = inspect.getsource(verify_memory_parity)
    assert "limit=100_000" not in source
    assert "COUNT(*) AS total FROM learning_episodes" in source
    assert "COUNT(*) AS total FROM student_memory_facts" in source
    assert source.count("tenant_id = current_setting('app.tenant_id'") >= 6
    assert "rolbypassrls" in source
    assert "pg_get_expr" in source
    assert "using_predicate" in source


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        (
            "(tenant_id = current_setting('app.tenant_id'::text, true))",
            True,
        ),
        ("tenant_id = current_setting('app.tenant_id', true) OR TRUE", False),
        ("tenant_id = current_setting('app.tenant_id', true) AND true", False),
        ("true", False),
    ],
)
def test_parity_policy_predicate_normalization_is_exact(
    expression: str, expected: bool
) -> None:
    assert verify_memory_parity._is_exact_tenant_predicate(expression) is expected


def test_parity_rejects_extra_permissive_tenant_policy(
    env: tuple[object, str, str]
) -> None:
    connection, _, _ = env
    admin = pg.connect_admin()
    try:
        admin.execute(
            "CREATE POLICY parity_extra_permissive ON students USING (true)"
        )
        admin.commit()
        with pytest.raises(RuntimeError, match="policy"):
            verify_memory_parity._require_schema(connection)
    finally:
        admin.execute("DROP POLICY IF EXISTS parity_extra_permissive ON students")
        admin.commit()
        pg.quiet_close(admin)


def test_database_identity_guard_rejects_mismatch_without_dsn_leak() -> None:
    class FakeResult:
        def __init__(self, row: dict[str, object]) -> None:
            self.row = row

        def fetchone(self) -> dict[str, object]:
            return self.row

    class FakeConnection:
        def __init__(self, row: dict[str, object]) -> None:
            self.row = row

        def execute(self, query: str) -> FakeResult:
            return FakeResult(self.row)

    with pytest.raises(RuntimeError, match="different PostgreSQL database targets") as raised:
        pg.assert_matching_database(
            FakeConnection(
                {
                    "database": "one",
                    "host": "127.0.0.1",
                    "port": 5432,
                    "postmaster_start_time": "2026-08-10T00:00:00+00:00",
                }
            ),
            FakeConnection(
                {
                    "database": "two",
                    "host": "127.0.0.1",
                    "port": 5432,
                    "postmaster_start_time": "2026-08-10T00:00:00+00:00",
                }
            ),
        )
    assert "postgresql://admin:secret" not in str(raised.value)


def test_safe_app_role_uses_current_user_and_rejects_superuser() -> None:
    admin = pg.connect_admin()
    try:
        with pytest.raises(RuntimeError, match="non-superuser"):
            pg.assert_safe_app_role(admin)
    finally:
        pg.quiet_close(admin)


def test_safe_app_role_accepts_configured_application_role() -> None:
    connection = pg.connect()
    try:
        pg.assert_safe_app_role(connection)
    finally:
        pg.quiet_close(connection)


def test_pg_connect_closes_rejected_application_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeConnection:
        def __init__(self) -> None:
            self.rollback_calls = 0
            self.close_calls = 0

        def rollback(self) -> None:
            self.rollback_calls += 1

        def close(self) -> None:
            self.close_calls += 1

    connection = FakeConnection()
    monkeypatch.setattr(pg.psycopg, "connect", lambda *args, **kwargs: connection)

    def reject(_connection: object) -> None:
        raise RuntimeError("unsafe application role")

    monkeypatch.setattr(pg, "assert_safe_app_role", reject)
    with pytest.raises(RuntimeError, match="unsafe application role"):
        pg.connect("postgresql://app@localhost/example")
    assert connection.rollback_calls == 1
    assert connection.close_calls == 1


def test_pg_connect_returns_a_clean_transaction_after_role_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeConnection:
        def __init__(self) -> None:
            self.rollback_calls = 0

        def rollback(self) -> None:
            self.rollback_calls += 1

        def close(self) -> None:
            pass

    connection = FakeConnection()
    monkeypatch.setattr(pg.psycopg, "connect", lambda *args, **kwargs: connection)
    monkeypatch.setattr(pg, "assert_safe_app_role", lambda _connection: None)

    assert pg.connect("postgresql://app@localhost/example") is connection
    assert connection.rollback_calls == 1


def test_database_identity_guard_rejects_different_postmaster_start() -> None:
    class FakeResult:
        def __init__(self, row: dict[str, object]) -> None:
            self.row = row

        def fetchone(self) -> dict[str, object]:
            return self.row

    class FakeConnection:
        def __init__(self, row: dict[str, object]) -> None:
            self.row = row

        def execute(self, query: str) -> FakeResult:
            return FakeResult(self.row)

    first = {
        "database": "same",
        "host": "127.0.0.1",
        "port": 5432,
        "postmaster_start_time": "2026-08-10T00:00:00+00:00",
    }
    second = {**first, "postmaster_start_time": "2026-08-10T00:01:00+00:00"}
    with pytest.raises(RuntimeError, match="different PostgreSQL database targets"):
        pg.assert_matching_database(FakeConnection(first), FakeConnection(second))


def test_pg_transaction_preserves_primary_exception_when_rollback_fails() -> None:
    class Connection:
        def commit(self) -> None:
            raise AssertionError("commit should not run")

        def rollback(self) -> None:
            raise RuntimeError("rollback failed")

    with pytest.raises(ValueError, match="primary failure"):
        with pg.transaction(Connection()):
            raise ValueError("primary failure")


def test_quiet_close_attempts_close_after_rollback_failure() -> None:
    calls: list[str] = []

    class Connection:
        def rollback(self) -> None:
            calls.append("rollback")
            raise RuntimeError("rollback failed")

        def close(self) -> None:
            calls.append("close")
            raise RuntimeError("close failed")

    pg.quiet_close(Connection())
    assert calls == ["rollback", "close"]


@pytest.mark.parametrize(
    ("script_name", "extra_args"),
    [
        ("seed_demo.py", []),
        ("rebuild_memory_index.py", []),
        ("replay_dead_letter.py", ["--max-attempts", "1"]),
    ],
)
def test_task5_scripts_reject_mismatched_admin_target(
    script_name: str,
    extra_args: list[str],
    isolated_script_database: tuple[str, str, str],
) -> None:
    _, app_dsn, _ = isolated_script_database
    alternate_admin_dsn = psycopg.conninfo.make_conninfo(
        pg.admin_dsn(), dbname="postgres"
    )
    tenant_id = unique_tenant_id("task5_pair_mismatch")
    environment = os.environ.copy()
    environment["BRIDGESAT_MODE"] = "local"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / script_name),
            "--db",
            app_dsn,
            "--admin-db",
            alternate_admin_dsn,
            "--tenant",
            tenant_id,
            *extra_args,
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 2
    assert "different PostgreSQL database targets" in result.stderr
    assert app_dsn not in result.stderr
    assert alternate_admin_dsn not in result.stderr
    assert "secret" not in result.stderr


def test_task5_scripts_honor_alternate_admin_and_app_dsns(
    isolated_script_database: tuple[str, str, str],
) -> None:
    admin_dsn, app_dsn, _ = isolated_script_database
    environment = os.environ.copy()
    environment["BRIDGESAT_MODE"] = "local"
    environment["BRIDGESAT_PACKS_ROOT"] = str(PACKS_ROOT)
    tenant_id = unique_tenant_id("task5_pair_success")
    commands = [
        ["seed_demo.py"],
        ["rebuild_memory_index.py"],
        ["replay_dead_letter.py", "--max-attempts", "1"],
    ]
    for command_args in commands:
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / command_args[0]),
                "--db",
                app_dsn,
                "--admin-db",
                admin_dsn,
                "--tenant",
                tenant_id,
                *command_args[1:],
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert "PosixPath" not in result.stderr
        assert "object has no attribute 'encode'" not in result.stderr


@pytest.mark.parametrize(
    "module",
    [run_performance_evals, run_memory_ablation, verify_memory_parity],
)
def test_task5b_scripts_have_pg_runtime_only(module: object) -> None:
    source = inspect.getsource(module)
    assert "SQLiteMemory" not in source
    assert "sqlite3" not in source
    assert "apply_migrations" not in source
    assert "app.infrastructure.database" not in source
    assert "TemporaryDirectory" not in source
    assert "DROP SCHEMA" not in source
    if module is run_performance_evals:
        assert "index_pack" not in source
    if module is verify_memory_parity:
        assert "rebuild_student" not in source
        assert "PGMemory" not in source
        assert "OutboxRepository" not in source
        assert "DELETE FROM memory_outbox" not in source
        assert "migrate_database" not in source
        assert "connect_admin" not in source
        assert "--admin-db" not in source
        assert "BEGIN READ ONLY" in source
        assert "set_config('app.tenant_id', %s, true)" in source


def _json_prefix(stdout: str) -> dict:
    decoder = json.JSONDecoder()
    value, _ = decoder.raw_decode(stdout.lstrip())
    return value


def test_performance_eval_cli_uses_pg_and_preserves_report_shape(
    tmp_path: Path, isolated_script_database: tuple[str, str, str]
) -> None:
    admin_dsn, app_dsn, protected_tenant = isolated_script_database
    tenant_label = protected_tenant
    report_path = tmp_path / "performance.json"
    environment = os.environ.copy()
    environment["BRIDGESAT_DB"] = app_dsn
    environment["BRIDGESAT_ADMIN_DB"] = admin_dsn
    environment["BRIDGESAT_PACKS_ROOT"] = str(PACKS_ROOT)
    protected_before = _tenant_row_count(protected_tenant, admin_dsn)
    fts_before = _knowledge_row_count(app_dsn)
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "run_performance_evals.py"),
                "--tenant",
                tenant_label,
                "--json",
                str(report_path),
                "--samples-policy",
                "20",
                "--samples-tsvector",
                "20",
                "--samples-restore",
                "5",
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert result.returncode in (0, 2), result.stderr
        assert "PosixPath" not in result.stderr
        assert "object has no attribute 'encode'" not in result.stderr
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert set(report) >= {
            "schema_version",
            "label",
            "results",
            "sync_throughput_events_per_sec",
            "max_rss_mb",
            "targets",
            "all_gates_passed",
        }
        assert set(report["results"]) == {
            "local_policy",
            "tsvector",
            "session_restore",
        }
        actual_tenant = report["tenant_id"]
        assert actual_tenant.startswith(f"{tenant_label}_")
        assert actual_tenant != tenant_label
        assert _tenant_row_count(actual_tenant, admin_dsn) == 0
        assert _tenant_row_count(protected_tenant, admin_dsn) == protected_before
        assert _knowledge_row_count(app_dsn) == fts_before
    finally:
        cleanup = pg.connect_admin(admin_dsn)
        try:
            if "actual_tenant" in locals():
                cleanup_tenant(cleanup, actual_tenant)
        finally:
            cleanup.close()


def test_memory_ablation_cli_uses_pg_and_preserves_metrics_shape(
    tmp_path: Path, env: tuple[object, str, str]
) -> None:
    _, _, protected_tenant = env
    tenant_label = protected_tenant
    report_path = tmp_path / "REPORT.md"
    environment = os.environ.copy()
    environment["BRIDGESAT_DB"] = pg.dsn()
    environment["BRIDGESAT_ADMIN_DB"] = pg.admin_dsn()
    command = [
        sys.executable,
        str(ROOT / "scripts" / "run_memory_ablation.py"),
        "--tenant",
        tenant_label,
        "--golden",
        str(ROOT / "evals" / "memory" / "golden.jsonl"),
        "--out",
        str(report_path),
    ]
    protected_before = _tenant_row_count(protected_tenant)
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert "PosixPath" not in result.stderr
        assert "object has no attribute 'encode'" not in result.stderr
        payload = _json_prefix(result.stdout)
        assert set(payload) == {"summary", "probes"}
        assert set(payload["summary"]) >= {
            "probes",
            "no_memory",
            "recent_postgres",
            "similar_postgres",
            "mnemis_system1",
            "mnemis_dual",
        }
        assert report_path.is_file()
        actual_tenant = payload["summary"]["tenant_id"]
        assert actual_tenant.startswith(f"{tenant_label}_")
        assert actual_tenant != tenant_label
        assert f"tenant_id: {actual_tenant}" in report_path.read_text(encoding="utf-8")
        assert _tenant_row_count(actual_tenant) == 0
        assert _tenant_row_count(protected_tenant) == protected_before

        second = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert second.returncode == 0, second.stderr
        assert "PosixPath" not in second.stderr
        assert "object has no attribute 'encode'" not in second.stderr
        second_payload = _json_prefix(second.stdout)
        assert set(second_payload) == {"summary", "probes"}
        second_tenant = second_payload["summary"]["tenant_id"]
        assert second_tenant.startswith(f"{tenant_label}_")
        assert second_tenant != actual_tenant
        assert _tenant_row_count(second_tenant) == 0
        assert _tenant_row_count(protected_tenant) == protected_before
    finally:
        cleanup = pg.connect_admin()
        try:
            for candidate in (locals().get("actual_tenant"), locals().get("second_tenant")):
                if candidate:
                    cleanup_tenant(cleanup, candidate)
        finally:
            cleanup.close()


def test_memory_parity_cli_uses_pg_and_is_idempotent(env: tuple[object, str, str]) -> None:
    connection, student_id, tenant_id = env
    _seed(env)
    outbox_before = _outbox_snapshot(connection, student_id)
    state_before = _parity_state_snapshot(connection, student_id)
    command = [
        sys.executable,
        str(ROOT / "scripts" / "verify_memory_parity.py"),
        "--tenant",
        tenant_id,
        "--student",
        student_id,
    ]
    environment = os.environ.copy()
    environment["BRIDGESAT_DB"] = pg.dsn()
    environment["BRIDGESAT_ADMIN_DB"] = (
        "postgresql://invalid:invalid@127.0.0.1:1/invalid"
    )
    first = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    second = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    first_report = json.loads(first.stdout)
    second_report = json.loads(second.stdout)
    assert first_report["parity"] == second_report["parity"] == "ok"
    assert first_report["students"] == second_report["students"]
    assert set(first_report) == {"parity", "students", "outbox"}
    assert set(first_report["students"][0]) == {
        "student_id",
        "sqlite",
        "indexed",
        "parity",
    }
    assert "PosixPath" not in first.stderr
    assert "object has no attribute 'encode'" not in first.stderr
    assert _outbox_snapshot(connection, student_id) == outbox_before
    assert _parity_state_snapshot(connection, student_id) == state_before


def test_performance_uses_explicit_app_and_admin_dsns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_script_database: tuple[str, str, str],
) -> None:
    admin_dsn, app_dsn, _ = isolated_script_database
    app_targets: list[str | None] = []
    admin_targets: list[str | None] = []
    real_connect = pg.connect
    real_connect_admin = pg.connect_admin

    def tracked_connect(target: str | None = None):
        app_targets.append(target)
        return real_connect(target)

    def tracked_connect_admin(target: str | None = None):
        admin_targets.append(target)
        return real_connect_admin(target)

    monkeypatch.setattr(run_performance_evals.pg, "connect", tracked_connect)
    monkeypatch.setattr(
        run_performance_evals.pg, "connect_admin", tracked_connect_admin
    )
    monkeypatch.setenv("BRIDGESAT_PACKS_ROOT", str(PACKS_ROOT))
    report_path = tmp_path / "dsn-performance.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_performance_evals.py",
            "--db",
            app_dsn,
            "--admin-db",
            admin_dsn,
            "--tenant",
            "task5b_dsn_probe",
            "--json",
            str(report_path),
            "--samples-policy",
            "1",
            "--samples-tsvector",
            "1",
            "--samples-restore",
            "1",
        ],
    )

    result = run_performance_evals.main()
    assert result in (0, 2)
    assert app_targets and set(app_targets) == {app_dsn}
    assert admin_targets and set(admin_targets) == {admin_dsn}
    report_text = report_path.read_text(encoding="utf-8")
    assert app_dsn not in report_text
    assert admin_dsn not in report_text
    report = json.loads(report_text)
    assert report["tenant_id"].startswith("task5b_dsn_probe_")


@pytest.mark.parametrize(
    "flag",
    [
        "--samples-policy",
        "--samples-tsvector",
        "--samples-fts5",
        "--samples-restore",
    ],
)
def test_performance_rejects_nonpositive_samples(
    monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_performance_evals.py", flag, "0"],
    )
    with pytest.raises(SystemExit) as raised:
        run_performance_evals.main()
    assert raised.value.code == 2


def test_performance_requires_prepopulated_knowledge_index() -> None:
    class EmptyIndexConnection:
        def execute(self, query: str):
            return type("Result", (), {"fetchone": lambda self: {"total": 0}})()

    with pytest.raises(RuntimeError, match="knowledge_fts is empty"):
        run_performance_evals._require_knowledge_index(EmptyIndexConnection())


def test_parity_rejects_missing_schema_without_migration() -> None:
    class MissingSchemaConnection:
        def execute(self, query: str):
            return type(
                "Result",
                (),
                {
                    "fetchone": lambda self: {
                        "schema_migrations": None,
                        "students": None,
                        "learning_episodes": None,
                        "student_memory_facts": None,
                        "memory_outbox": None,
                    }
                },
            )()

    with pytest.raises(RuntimeError, match="not migrated"):
        verify_memory_parity._require_schema(MissingSchemaConnection())


def test_parity_rejects_superuser_role(env: tuple[object, str, str]) -> None:
    admin = pg.connect_admin()
    try:
        migrate_database(admin)
        with pytest.raises(RuntimeError, match="non-superuser"):
            verify_memory_parity._require_schema(admin)
    finally:
        pg.quiet_close(admin)


def test_parity_rejects_missing_required_column() -> None:
    class IncompleteSchemaConnection:
        def execute(self, query: str, params: object = None):
            if "to_regclass" in query:
                row = {
                    "schema_migrations": "schema_migrations",
                    "students": "students",
                    "learning_episodes": "learning_episodes",
                    "student_memory_facts": "student_memory_facts",
                    "memory_outbox": "memory_outbox",
                }
            elif "MAX(version)" in query:
                row = {"version": SCHEMA_VERSION}
            else:
                row = None

            class Result:
                def fetchone(self):
                    return row

                def fetchall(self):
                    return []

            return Result()

    with pytest.raises(RuntimeError, match="required columns"):
        verify_memory_parity._require_schema(IncompleteSchemaConnection())


def test_pg_retrieval_fixture_does_not_drop_shared_schema() -> None:
    source = (ROOT / "tests" / "test_pg_retrieval.py").read_text(encoding="utf-8")
    assert "DROP SCHEMA" not in source


def test_memory_ablation_rejects_empty_golden(tmp_path: Path) -> None:
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(SystemExit) as raised:
        run_memory_ablation.main(
            ["--golden", str(empty), "--out", str(tmp_path / "empty-report.md")]
        )
    assert raised.value.code == 2


def test_parity_requires_students_or_allow_empty() -> None:
    tenant_id = unique_tenant_id("task5b_parity_empty")
    environment = os.environ.copy()
    environment["BRIDGESAT_DB"] = pg.dsn()
    environment["BRIDGESAT_ADMIN_DB"] = pg.admin_dsn()
    command = [
        sys.executable,
        str(ROOT / "scripts" / "verify_memory_parity.py"),
        "--tenant",
        tenant_id,
    ]
    try:
        missing = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert missing.returncode == 2
        assert "student" in missing.stderr.lower()

        allowed = subprocess.run(
            [*command, "--allow-empty"],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert allowed.returncode == 0, allowed.stderr
        report = json.loads(allowed.stdout)
        assert report["parity"] == "ok"
        assert report["students"] == []
        assert set(report) == {"parity", "students", "outbox"}
        assert "object has no attribute 'encode'" not in allowed.stderr
    finally:
        cleanup = pg.connect_admin()
        try:
            cleanup_tenant(cleanup, tenant_id)
        finally:
            cleanup.close()


@pytest.mark.parametrize("module", [seed_demo, rebuild_memory_index, replay_dead_letter])
def test_pg_scripts_have_no_sqlite_runtime_or_schema_wide_cleanup(module: object) -> None:
    source = inspect.getsource(module)
    assert "apply_migrations" not in source
    assert "app.infrastructure.database" not in source
    assert "DROP SCHEMA" not in source
