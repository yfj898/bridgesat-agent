"""Acceptance test 4: repeated forged sync events do not duplicate mastery
changes.

THREAT_MODEL.md section 5.4: clients submit observations, never
authoritative mastery; the server recomputes scores and projections,
dedupes by event ID, and rejects tampered integrity hashes and unknown
content versions.
"""

from __future__ import annotations

import psycopg
import pytest

from app.sync.protocol import SyncEventEnvelope, SyncRequest
from app.sync.service import DeviceNotFoundError, SyncService

from tests.security.conftest import envelope, seed_student

Q_LINEAR = "sync.linear.001"


def _seed_student(connection: psycopg.Connection, student_id: str) -> None:
    seed_student(connection, student_id)


def _process(
    sync: SyncService,
    student_id: str,
    device_id: str,
    events: list[dict],
) -> dict:
    response = sync.process_batch(
        SyncRequest(
            device_id=device_id,
            student_id=student_id,
            events=[SyncEventEnvelope(**event) for event in events],
        )
    )
    return response.model_dump()


def _attempt_count(connection: psycopg.Connection, student_id: str) -> int:
    return connection.execute(
        """
        SELECT COUNT(*) AS c
        FROM answer_attempts
        WHERE student_id = %s
          AND tenant_id = current_setting('app.tenant_id', true)
        """,
        (student_id,),
    ).fetchone()["c"]


def _evidence_count(
    connection: psycopg.Connection,
    student_id: str,
    skill: str = "linear_equations",
) -> int:
    return connection.execute(
        """
        SELECT evidence_count
        FROM student_skill_states
        WHERE student_id = %s
          AND skill = %s
          AND tenant_id = current_setting('app.tenant_id', true)
        """,
        (student_id, skill),
    ).fetchone()["evidence_count"]


def test_replayed_event_never_reapplies_mastery(
    db: psycopg.Connection, pg_tenant: str
) -> None:
    student_id = f"{pg_tenant}_forge"
    device_id = f"{student_id}_device"
    _seed_student(db, student_id)
    sync = SyncService(db)
    sync.register_device(student_id, "d", device_id=device_id)
    event_id = f"{student_id}_replay"
    event = envelope(event_id=event_id, student_id=student_id, device_id=device_id)

    first = _process(sync, student_id, device_id, [event])
    assert first["accepted_event_ids"] == [event_id]
    assert _evidence_count(db, student_id) == 1

    for _ in range(3):
        replay = _process(sync, student_id, device_id, [event])
        assert replay["duplicate_event_ids"] == [event_id]
        assert replay["accepted_event_ids"] == []

    assert _evidence_count(db, student_id) == 1
    assert _attempt_count(db, student_id) == 1


def test_tampered_integrity_hash_rejected_before_scoring(
    db: psycopg.Connection, pg_tenant: str
) -> None:
    student_id = f"{pg_tenant}_forge"
    device_id = f"{student_id}_device"
    _seed_student(db, student_id)
    sync = SyncService(db)
    sync.register_device(student_id, "d", device_id=device_id)
    event = envelope(
        event_id=f"{student_id}_tamper",
        student_id=student_id,
        device_id=device_id,
    )
    event["payload"]["selected_choice_id"] = "C"
    event["integrity_hash"] = "sha256:" + "0" * 64
    response = _process(sync, student_id, device_id, [event])
    assert response["accepted_event_ids"] == []
    assert response["rejected_events"][0]["code"] == "INVALID_SCHEMA"
    assert _attempt_count(db, student_id) == 0


def test_client_claimed_mastery_is_ignored(
    db: psycopg.Connection, pg_tenant: str
) -> None:
    student_id = f"{pg_tenant}_forge"
    device_id = f"{student_id}_device"
    _seed_student(db, student_id)
    sync = SyncService(db)
    sync.register_device(student_id, "d", device_id=device_id)
    event_id = f"{student_id}_claim"
    event = envelope(
        event_id=event_id,
        student_id=student_id,
        device_id=device_id,
        payload={
            "question_id": Q_LINEAR,
            "question_version": 1,
            "selected_choice_id": "A",
            "hint_level": 0,
            "attempt_id": event_id,
            "mastery_claimed": 0.99,
            "is_correct_claimed": True,
        },
    )
    response = _process(sync, student_id, device_id, [event])
    assert response["accepted_event_ids"] == [event_id]
    snapshot = sync.build_snapshot(student_id)
    linear = [s for s in snapshot.skill_states if s["skill"] == "linear_equations"][0]
    assert linear["mastery"] != 0.99


def test_forged_unknown_question_version_rejected(
    db: psycopg.Connection, pg_tenant: str
) -> None:
    student_id = f"{pg_tenant}_forge"
    device_id = f"{student_id}_device"
    _seed_student(db, student_id)
    sync = SyncService(db)
    sync.register_device(student_id, "d", device_id=device_id)
    event = envelope(
        event_id=f"{student_id}_unknown",
        student_id=student_id,
        device_id=device_id,
        question_version=99,
    )
    response = _process(sync, student_id, device_id, [event])
    assert response["accepted_event_ids"] == []
    assert response["rejected_events"][0]["code"] == "QUESTION_VERSION_UNKNOWN"
    assert _attempt_count(db, student_id) == 0


def test_device_from_another_student_cannot_write(
    db: psycopg.Connection, two_students
) -> None:
    (a, _), (b, _) = two_students
    sync = SyncService(db)
    device_id = f"{a}_device"
    sync.register_device(a, "device a", device_id=device_id)
    event = envelope(event_id=f"{b}_cross", student_id=b, device_id=device_id)
    with pytest.raises(DeviceNotFoundError):
        sync.process_batch(
            SyncRequest(
                device_id=device_id,
                student_id=b,
                events=[SyncEventEnvelope(**event)],
            )
        )
