"""EventStore on PostgreSQL."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from psycopg.errors import NotNullViolation

from app.domain.events import AgentEvent, LearningEvent, LearningEventType
from app.infrastructure import pg
from app.infrastructure.event_store import (
    DuplicateEventError,
    EventStore,
    run_in_transaction,
)
from app.infrastructure.migration_runner import migrate_database


TENANT = "tenant_test"


@pytest.fixture()
def store():
    admin = pg.connect_admin()
    migrate_database(admin)
    admin.close()

    connection = pg.connect()
    try:
        connection.execute(
            "SELECT set_config('app.tenant_id', %s, false)",
            (TENANT,),
        )
        connection.commit()
        yield EventStore(connection)
    finally:
        connection.close()
        cleanup = pg.connect_admin()
        try:
            cleanup.execute("DROP SCHEMA public CASCADE")
            cleanup.execute("CREATE SCHEMA public")
            cleanup.commit()
        finally:
            cleanup.close()


def _learning_event(
    event_id: str,
    *,
    occurred_at: str = "2026-01-01T00:00:00+00:00",
    received_at: str = "2026-01-01T00:00:00+00:00",
) -> LearningEvent:
    return LearningEvent(
        event_id=event_id,
        student_id="stu_1",
        session_id="sess_1",
        event_type=LearningEventType.ANSWER_SUBMITTED,
        payload={},
        policy_version="test",
        occurred_at=occurred_at,
        received_at=received_at,
        origin="online",
    ).with_integrity()


def _agent_event(event_id: str) -> AgentEvent:
    return AgentEvent(
        event_id=event_id,
        student_id="stu_1",
        session_id="sess_1",
        action="insert_micro_lesson",
        action_payload={},
        reason_code="r",
        reason_text="t",
        policy_version="test",
        source="online",
        created_at="2026-01-01T00:00:00+00:00",
    )


def _insert_learning_event(connection, event: LearningEvent) -> None:
    connection.execute(
        """
        INSERT INTO learning_events (
            tenant_id, event_id, student_id, session_id, event_type,
            payload_json, policy_version, occurred_at, received_at,
            origin, integrity_hash
        ) VALUES (
            current_setting('app.tenant_id'), %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s
        )
        """,
        (
            event.event_id,
            event.student_id,
            event.session_id,
            event.event_type.value,
            json.dumps(event.payload, sort_keys=True),
            event.policy_version,
            event.occurred_at,
            event.received_at,
            event.origin,
            event.integrity_hash,
        ),
    )


def test_append_and_get_learning_event_roundtrip(store: EventStore) -> None:
    event = _learning_event("evt_1")

    assert store.append_learning_event(event) is True
    assert store.learning_event_exists(event.event_id) is True
    assert store.get_learning_events("stu_1", session_id="sess_1") == [event]


def test_micro_lesson_presented_event_rehydrates_from_event_store(
    store: EventStore,
) -> None:
    event = LearningEvent(
        event_id="evt_micro_presented",
        student_id="stu_1",
        session_id="sess_1",
        event_type=LearningEventType.MICRO_LESSON_PRESENTED,
        payload={"content_id": "ml_1"},
        policy_version="test",
        occurred_at="2026-01-01T00:00:00+00:00",
        received_at="2026-01-01T00:00:00+00:00",
        origin="online",
    ).with_integrity()

    assert store.append_learning_event(event) is True
    assert store.get_learning_events("stu_1", session_id="sess_1") == [event]


def test_learning_events_are_ordered_by_occurred_then_received(store: EventStore) -> None:
    occurred_first = _learning_event(
        "evt_occurred_first",
        occurred_at="2026-01-01T00:00:00+00:00",
        received_at="2026-01-01T00:02:00+00:00",
    )
    received_first = _learning_event(
        "evt_received_first",
        occurred_at="2026-01-01T00:01:00+00:00",
        received_at="2026-01-01T00:01:00+00:00",
    )
    same_occurred_received_later = _learning_event(
        "evt_same_occurred_received_later",
        occurred_at="2026-01-01T00:02:00+00:00",
        received_at="2026-01-01T00:04:00+00:00",
    )
    same_occurred_received_earlier = _learning_event(
        "evt_same_occurred_received_earlier",
        occurred_at="2026-01-01T00:02:00+00:00",
        received_at="2026-01-01T00:03:00+00:00",
    )

    assert store.append_learning_event(occurred_first) is True
    assert store.append_learning_event(received_first) is True
    assert store.append_learning_event(same_occurred_received_later) is True
    assert store.append_learning_event(same_occurred_received_earlier) is True

    assert [
        event.event_id
        for event in store.get_learning_events("stu_1", session_id="sess_1")
    ] == [
        "evt_occurred_first",
        "evt_received_first",
        "evt_same_occurred_received_earlier",
        "evt_same_occurred_received_later",
    ]


def test_duplicate_learning_event_raises(store: EventStore) -> None:
    event = _learning_event("evt_1")
    store.append_learning_event(event)

    with pytest.raises(DuplicateEventError):
        store.append_learning_event(event, on_duplicate="raise")


def test_duplicate_agent_event_with_ignore_returns_false(store: EventStore) -> None:
    event = _agent_event("aev_1")

    assert store.append_agent_event(event) is True
    assert store.append_agent_event(event, on_duplicate="ignore") is False
    assert store.get_agent_events("stu_1", session_id="sess_1") == [event]


def test_run_in_transaction_commits_two_events(store: EventStore) -> None:
    events = [_learning_event("evt_a"), _learning_event("evt_b")]

    reader_connection = pg.connect()
    reader_connection.execute(
        "SELECT set_config('app.tenant_id', %s, false)",
        (TENANT,),
    )
    reader_connection.commit()
    reader = EventStore(reader_connection)

    def insert_both(connection) -> None:
        for event in events:
            _insert_learning_event(connection, event)

    try:
        run_in_transaction(store.connection, insert_both)

        assert [event.event_id for event in reader.get_learning_events("stu_1")] == [
            "evt_a",
            "evt_b",
        ]
    finally:
        reader_connection.close()


def test_run_in_transaction_rolls_back_callback_error(store: EventStore) -> None:
    event = _learning_event("evt_rollback")
    reader_connection = pg.connect()
    reader_connection.execute(
        "SELECT set_config('app.tenant_id', %s, false)",
        (TENANT,),
    )
    reader_connection.commit()
    reader = EventStore(reader_connection)

    def insert_then_fail(connection) -> None:
        _insert_learning_event(connection, event)
        raise RuntimeError("abort transaction")

    try:
        with pytest.raises(RuntimeError, match="abort transaction"):
            run_in_transaction(store.connection, insert_then_fail)

        assert store.learning_event_exists(event.event_id) is False
        assert reader.learning_event_exists(event.event_id) is False
        reader_connection.commit()

        committed_event = _learning_event("evt_after_rollback")
        assert store.append_learning_event(committed_event) is True
        assert store.learning_event_exists(committed_event.event_id) is True
        assert reader.learning_event_exists(committed_event.event_id) is True
    finally:
        reader_connection.close()


def test_learning_events_are_isolated_by_tenant(store: EventStore) -> None:
    event = _learning_event("evt_tenant_test")
    assert store.append_learning_event(event) is True

    other_connection = pg.connect()
    try:
        other_connection.execute(
            "SELECT set_config('app.tenant_id', %s, false)",
            ("tenant_other",),
        )
        other_connection.commit()
        other_store = EventStore(other_connection)

        assert other_store.get_learning_events("stu_1") == []
        assert other_store.learning_event_exists(event.event_id) is False
    finally:
        other_connection.close()


def test_concurrent_duplicate_learning_event_has_one_winner(store: EventStore) -> None:
    event = _learning_event("evt_concurrent")
    barrier = Barrier(2)

    def append_from_independent_connection() -> tuple[bool, bool]:
        connection = pg.connect()
        try:
            connection.execute(
                "SELECT set_config('app.tenant_id', %s, false)",
                (TENANT,),
            )
            connection.commit()
            barrier.wait(timeout=10)
            event_store = EventStore(connection)
            appended = event_store.append_learning_event(event)
            reusable = event_store.learning_event_exists(event.event_id)
            return appended, reusable
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(append_from_independent_connection)
            for _ in range(2)
        ]
        results = [future.result() for future in futures]

    assert sorted(appended for appended, _ in results) == [False, True]
    assert all(reusable for _, reusable in results)
    assert store.get_learning_events("stu_1") == [event]


@pytest.mark.parametrize(
    ("append_name", "event_factory"),
    [
        ("append_learning_event", _learning_event),
        ("append_agent_event", _agent_event),
    ],
)
def test_append_recovers_after_non_unique_db_error(
    store: EventStore,
    append_name: str,
    event_factory,
) -> None:
    append = getattr(store, append_name)

    invalid_event = event_factory("evt_invalid").model_copy(
        update={"student_id": None}
    )
    with pytest.raises(NotNullViolation):
        append(invalid_event)

    store.connection.execute(
        "SELECT set_config('app.tenant_id', %s, false)",
        (TENANT,),
    )
    store.connection.commit()

    assert append(event_factory(f"evt_after_{append_name}")) is True
