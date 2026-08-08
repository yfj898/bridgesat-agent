"""EventStore on PostgreSQL."""
from __future__ import annotations

import json

import pytest

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


def test_append_and_get_learning_event_roundtrip(store: EventStore) -> None:
    event = _learning_event("evt_1")

    assert store.append_learning_event(event) is True
    assert store.learning_event_exists(event.event_id) is True
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

    assert store.append_learning_event(occurred_first) is True
    assert store.append_learning_event(received_first) is True

    assert [
        event.event_id
        for event in store.get_learning_events("stu_1", session_id="sess_1")
    ] == ["evt_occurred_first", "evt_received_first"]


def test_duplicate_learning_event_raises(store: EventStore) -> None:
    event = _learning_event("evt_1")
    store.append_learning_event(event)

    with pytest.raises(DuplicateEventError):
        store.append_learning_event(event, on_duplicate="raise")


def test_duplicate_agent_event_with_ignore_returns_false(store: EventStore) -> None:
    event = AgentEvent(
        event_id="aev_1",
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

    assert store.append_agent_event(event) is True
    assert store.append_agent_event(event, on_duplicate="ignore") is False
    assert store.get_agent_events("stu_1", session_id="sess_1") == [event]


def test_run_in_transaction_commits_two_events(store: EventStore) -> None:
    events = [_learning_event("evt_a"), _learning_event("evt_b")]

    def insert_both(connection) -> None:
        for event in events:
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

    run_in_transaction(store.connection, insert_both)

    assert [event.event_id for event in store.get_learning_events("stu_1")] == [
        "evt_a",
        "evt_b",
    ]
