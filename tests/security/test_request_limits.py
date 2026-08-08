"""Acceptance test 10: oversized requests and excessive retrieval loops are
rejected.

THREAT_MODEL.md section 5.11 and plan sections 7-8: sync batches are capped
at 100 events, retrieval is capped at 20 results, queries are length-bound,
and schema validation rejects malformed inputs before processing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.sync.protocol import SyncEventEnvelope, SyncRequest
from app.sync.service import SyncService

from tests.security.conftest import envelope, seed_student

STUDENT_ID = "student_limits"


def _seed_student(db: Path) -> None:
    seed_student(db, STUDENT_ID)


def test_sync_batch_over_100_rejected(db: Path) -> None:
    _seed_student(db)
    sync = SyncService(db)
    sync.register_device(STUDENT_ID, "d", device_id="dev_lim")
    events = [
        envelope(event_id=f"evt_{i}", student_id=STUDENT_ID, device_id="dev_lim")
        for i in range(101)
    ]
    response = sync.process_batch(
        SyncRequest(
            device_id="dev_lim",
            student_id=STUDENT_ID,
            events=[SyncEventEnvelope(**event) for event in events],
        )
    )
    assert response.rejected_events[0].code == "PAYLOAD_TOO_LARGE"
    assert response.rejected_events[0].retryable is False


def test_knowledge_retrieval_result_cap_and_query_bounds() -> None:
    client = TestClient(__import__("app.main", fromlist=["app"]).app)
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


def test_retrieval_failure_is_fail_closed(tmp_path: Path, monkeypatch) -> None:
    """Retrieval against an index with no approved content must return an
    explicit no-result, never degraded or unauthorized content."""
    empty_db = tmp_path / "empty_registry.db"
    monkeypatch.setenv("BRIDGESAT_KNOWLEDGE_DB", str(empty_db))
    client = TestClient(__import__("app.main", fromlist=["app"]).app)
    response = client.post("/v1/knowledge/retrieve", json={"query": "solve for x"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["results"] == []
    assert payload["explicit_no_result"] is True


def test_diagnostic_payload_bounds_rejected(tmp_path: Path) -> None:
    db = tmp_path / "limits.db"
    from app.auth import TokenStore
    from app.infrastructure.migration_runner import apply_migrations
    from app.repository import StudentRepository

    import app.main as main

    apply_migrations(db)
    main.repository = StudentRepository(db)
    main.token_store = TokenStore(db)
    student_id = main.repository.create(
        __import__("app.models", fromlist=["StudentCreate"]).StudentCreate(
            name="Limits", daily_minutes=15, target_score=1100
        )
    ).id
    token = main.token_store.issue(student_id)
    client = TestClient(main.app)
    response = client.post(
        "/v1/diagnostics",
        headers={"Authorization": f"Bearer {token}"},
        json={"student_id": student_id, "answers": []},
    )
    assert response.status_code == 422


def test_oversized_student_name_rejected() -> None:
    client = TestClient(__import__("app.main", fromlist=["app"]).app)
    response = client.post(
        "/v1/students",
        json={"name": "x" * 200, "daily_minutes": 20, "target_score": 1200},
    )
    assert response.status_code == 422


def test_single_payload_over_64kb_rejected(db: Path) -> None:
    _seed_student(db)
    sync = SyncService(db)
    sync.register_device(STUDENT_ID, "d", device_id="dev_lim")
    event = envelope(
        event_id="evt_huge",
        student_id=STUDENT_ID,
        device_id="dev_lim",
        payload={"question_id": "sync.linear.001", "blob": "x" * (70 * 1024)},
    )
    response = sync.process_batch(
        SyncRequest(
            device_id="dev_lim",
            student_id=STUDENT_ID,
            events=[SyncEventEnvelope(**event)],
        )
    )
    assert response.accepted_event_ids == []
    assert response.rejected_events[0].code == "PAYLOAD_TOO_LARGE"
    assert response.rejected_events[0].retryable is False


def test_missing_integrity_hash_rejected(db: Path) -> None:
    _seed_student(db)
    sync = SyncService(db)
    sync.register_device(STUDENT_ID, "d", device_id="dev_lim")
    event = envelope(
        event_id="evt_nohash",
        student_id=STUDENT_ID,
        device_id="dev_lim",
        include_hash=False,
    )
    response = sync.process_batch(
        SyncRequest(
            device_id="dev_lim",
            student_id=STUDENT_ID,
            events=[SyncEventEnvelope(**event)],
        )
    )
    assert response.accepted_event_ids == []
    assert response.rejected_events[0].code == "INVALID_SCHEMA"
    assert response.rejected_events[0].retryable is False


def test_out_of_order_device_sequence_rejected(db: Path) -> None:
    _seed_student(db)
    sync = SyncService(db)
    sync.register_device(STUDENT_ID, "d", device_id="dev_lim")
    first = envelope(
        event_id="evt_seq_1", student_id=STUDENT_ID, device_id="dev_lim", device_sequence=1
    )
    response = sync.process_batch(
        SyncRequest(
            device_id="dev_lim",
            student_id=STUDENT_ID,
            events=[SyncEventEnvelope(**first)],
        )
    )
    assert response.accepted_event_ids == ["evt_seq_1"]

    stale = envelope(
        event_id="evt_seq_1_replay",
        student_id=STUDENT_ID,
        device_id="dev_lim",
        device_sequence=1,
    )
    stale["payload"]["attempt_id"] = "evt_seq_1_replay"
    response = sync.process_batch(
        SyncRequest(
            device_id="dev_lim",
            student_id=STUDENT_ID,
            events=[SyncEventEnvelope(**stale)],
        )
    )
    assert response.accepted_event_ids == []
    assert response.rejected_events[0].code == "INVALID_SCHEMA"

    next_event = envelope(
        event_id="evt_seq_2", student_id=STUDENT_ID, device_id="dev_lim", device_sequence=2
    )
    response = sync.process_batch(
        SyncRequest(
            device_id="dev_lim",
            student_id=STUDENT_ID,
            events=[SyncEventEnvelope(**next_event)],
        )
    )
    assert response.accepted_event_ids == ["evt_seq_2"]
