"""Acceptance test 10: oversized requests and excessive retrieval loops are
rejected.

THREAT_MODEL.md section 5.11 and plan sections 7-8: sync batches are capped
at 100 events, retrieval is capped at 20 results, queries are length-bound,
and schema validation rejects malformed inputs before processing.
"""

from __future__ import annotations

import psycopg

from app.sync.protocol import SyncEventEnvelope, SyncRequest
from app.sync.service import SyncService

from tests.security.conftest import envelope, seed_student



def _student_id(pg_tenant: str) -> str:
    return f"{pg_tenant}_limits"


def test_sync_batch_over_100_rejected(
    db: psycopg.Connection, pg_tenant: str
) -> None:
    student_id = _student_id(pg_tenant)
    device_id = f"{student_id}_device"
    seed_student(db, student_id)
    sync = SyncService(db)
    sync.register_device(student_id, "d", device_id=device_id)
    events = [
        envelope(
            event_id=f"{student_id}_evt_{i}",
            student_id=student_id,
            device_id=device_id,
        )
        for i in range(101)
    ]
    response = sync.process_batch(
        SyncRequest(
            device_id=device_id,
            student_id=student_id,
            events=[SyncEventEnvelope(**event) for event in events],
        )
    )
    assert response.rejected_events[0].code == "PAYLOAD_TOO_LARGE"
    assert response.rejected_events[0].retryable is False


def test_knowledge_retrieval_result_cap_and_query_bounds(client) -> None:
    oversized = client.post(
        "/v1/knowledge/retrieve",
        json={"query": "linear equations", "max_results": 21},
    )
    assert oversized.status_code == 422
    too_long = client.post(
        "/v1/knowledge/retrieve",
        json={"query": "x" * 501},
    )
    assert too_long.status_code == 422
    empty = client.post("/v1/knowledge/retrieve", json={"query": ""})
    assert empty.status_code == 422


def test_retrieval_failure_is_fail_closed(client) -> None:
    """Retrieval against an index with no approved content must return an
    explicit no-result, never degraded or unauthorized content."""
    response = client.post(
        "/v1/knowledge/retrieve",
        json={"query": "__definitely_no_approved_content__"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["results"] == []
    assert payload["explicit_no_result"] is True


def test_diagnostic_payload_bounds_rejected(client) -> None:
    created = client.post(
        "/v1/students",
        json={"name": "Limits", "daily_minutes": 15, "target_score": 1100},
    )
    assert created.status_code == 201
    student_id = created.json()["id"]
    token = created.json()["token"]
    response = client.post(
        "/v1/diagnostics",
        headers={"Authorization": f"Bearer {token}"},
        json={"student_id": student_id, "answers": []},
    )
    assert response.status_code == 422


def test_oversized_student_name_rejected(client) -> None:
    response = client.post(
        "/v1/students",
        json={"name": "x" * 200, "daily_minutes": 20, "target_score": 1200},
    )
    assert response.status_code == 422


def test_single_payload_over_64kb_rejected(
    db: psycopg.Connection, pg_tenant: str
) -> None:
    student_id = _student_id(pg_tenant)
    device_id = f"{student_id}_device"
    seed_student(db, student_id)
    sync = SyncService(db)
    sync.register_device(student_id, "d", device_id=device_id)
    event = envelope(
        event_id=f"{student_id}_huge",
        student_id=student_id,
        device_id=device_id,
        payload={"question_id": "sync.linear.001", "blob": "x" * (70 * 1024)},
    )
    response = sync.process_batch(
        SyncRequest(
            device_id=device_id,
            student_id=student_id,
            events=[SyncEventEnvelope(**event)],
        )
    )
    assert response.accepted_event_ids == []
    assert response.rejected_events[0].code == "PAYLOAD_TOO_LARGE"
    assert response.rejected_events[0].retryable is False


def test_missing_integrity_hash_rejected(
    db: psycopg.Connection, pg_tenant: str
) -> None:
    student_id = _student_id(pg_tenant)
    device_id = f"{student_id}_device"
    seed_student(db, student_id)
    sync = SyncService(db)
    sync.register_device(student_id, "d", device_id=device_id)
    event = envelope(
        event_id=f"{student_id}_nohash",
        student_id=student_id,
        device_id=device_id,
        include_hash=False,
    )
    response = sync.process_batch(
        SyncRequest(
            device_id=device_id,
            student_id=student_id,
            events=[SyncEventEnvelope(**event)],
        )
    )
    assert response.accepted_event_ids == []
    assert response.rejected_events[0].code == "INVALID_SCHEMA"
    assert response.rejected_events[0].retryable is False


def test_out_of_order_device_sequence_rejected(
    db: psycopg.Connection, pg_tenant: str
) -> None:
    student_id = _student_id(pg_tenant)
    device_id = f"{student_id}_device"
    seed_student(db, student_id)
    sync = SyncService(db)
    sync.register_device(student_id, "d", device_id=device_id)
    first = envelope(
        event_id=f"{student_id}_seq_1",
        student_id=student_id,
        device_id=device_id,
        device_sequence=1,
    )
    response = sync.process_batch(
        SyncRequest(
            device_id=device_id,
            student_id=student_id,
            events=[SyncEventEnvelope(**first)],
        )
    )
    assert response.accepted_event_ids == [f"{student_id}_seq_1"]

    stale = envelope(
        event_id=f"{student_id}_seq_1_replay",
        student_id=student_id,
        device_id=device_id,
        device_sequence=1,
    )
    stale["payload"]["attempt_id"] = f"{student_id}_seq_1_replay"
    response = sync.process_batch(
        SyncRequest(
            device_id=device_id,
            student_id=student_id,
            events=[SyncEventEnvelope(**stale)],
        )
    )
    assert response.accepted_event_ids == []
    assert response.rejected_events[0].code == "INVALID_SCHEMA"

    next_event = envelope(
        event_id=f"{student_id}_seq_2",
        student_id=student_id,
        device_id=device_id,
        device_sequence=2,
    )
    response = sync.process_batch(
        SyncRequest(
            device_id=device_id,
            student_id=student_id,
            events=[SyncEventEnvelope(**next_event)],
        )
    )
    assert response.accepted_event_ids == [f"{student_id}_seq_2"]
