"""Acceptance test 4: repeated forged sync events do not duplicate mastery
changes.

THREAT_MODEL.md section 5.4: clients submit observations, never
authoritative mastery; the server recomputes scores and projections,
dedupes by event ID, and rejects tampered integrity hashes and unknown
content versions.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.infrastructure.database import connect
from app.sync.protocol import SyncEventEnvelope, SyncRequest
from app.sync.service import DeviceNotFoundError, SyncService

from tests.security.conftest import envelope, seed_student

Q_LINEAR = "sync.linear.001"
STUDENT_ID = "student_forge"


def _seed_student(db: Path, student_id: str = STUDENT_ID) -> None:
    seed_student(db, student_id)


def _process(sync: SyncService, events: list[dict]) -> dict:
    response = sync.process_batch(
        SyncRequest(
            device_id="dev_forge",
            student_id=STUDENT_ID,
            events=[SyncEventEnvelope(**event) for event in events],
        )
    )
    return response.model_dump()


def _attempt_count(db: Path) -> int:
    with connect(db) as connection:
        return connection.execute(
            "SELECT COUNT(*) AS c FROM answer_attempts WHERE student_id = ?",
            (STUDENT_ID,),
        ).fetchone()["c"]


def _evidence_count(db: Path, skill: str = "linear_equations") -> int:
    with connect(db) as connection:
        return connection.execute(
            "SELECT evidence_count FROM student_skill_states WHERE student_id = ? AND skill = ?",
            (STUDENT_ID, skill),
        ).fetchone()["evidence_count"]


def test_replayed_event_never_reapplies_mastery(db: Path) -> None:
    _seed_student(db)
    sync = SyncService(db)
    sync.register_device(STUDENT_ID, "d", device_id="dev_forge")
    event = envelope(event_id="evt_replay", student_id=STUDENT_ID)

    first = _process(sync, [event])
    assert first["accepted_event_ids"] == ["evt_replay"]
    assert _evidence_count(db) == 1

    for _ in range(3):
        replay = _process(sync, [event])
        assert replay["duplicate_event_ids"] == ["evt_replay"]
        assert replay["accepted_event_ids"] == []

    assert _evidence_count(db) == 1
    assert _attempt_count(db) == 1


def test_tampered_integrity_hash_rejected_before_scoring(db: Path) -> None:
    _seed_student(db)
    sync = SyncService(db)
    sync.register_device(STUDENT_ID, "d", device_id="dev_forge")
    event = envelope(event_id="evt_tamper", student_id=STUDENT_ID)
    event["payload"]["selected_choice_id"] = "C"
    event["integrity_hash"] = "sha256:" + "0" * 64
    response = _process(sync, [event])
    assert response["accepted_event_ids"] == []
    assert response["rejected_events"][0]["code"] == "INVALID_SCHEMA"
    assert _attempt_count(db) == 0


def test_client_claimed_mastery_is_ignored(db: Path) -> None:
    _seed_student(db)
    sync = SyncService(db)
    sync.register_device(STUDENT_ID, "d", device_id="dev_forge")
    event = envelope(
        event_id="evt_claim",
        student_id=STUDENT_ID,
        payload={
            "question_id": Q_LINEAR,
            "question_version": 1,
            "selected_choice_id": "A",
            "hint_level": 0,
            "attempt_id": "evt_claim",
            "mastery_claimed": 0.99,
            "is_correct_claimed": True,
        },
    )
    response = _process(sync, [event])
    assert response["accepted_event_ids"] == ["evt_claim"]
    snapshot = sync.build_snapshot(STUDENT_ID)
    linear = [s for s in snapshot.skill_states if s["skill"] == "linear_equations"][0]
    assert linear["mastery"] != 0.99


def test_forged_unknown_question_version_rejected(db: Path) -> None:
    _seed_student(db)
    sync = SyncService(db)
    sync.register_device(STUDENT_ID, "d", device_id="dev_forge")
    event = envelope(event_id="evt_forge", student_id=STUDENT_ID, question_version=99)
    response = _process(sync, [event])
    assert response["accepted_event_ids"] == []
    assert response["rejected_events"][0]["code"] == "QUESTION_VERSION_UNKNOWN"
    assert _attempt_count(db) == 0


def test_device_from_another_student_cannot_write(db: Path, two_students) -> None:
    (a, _), (b, _) = two_students
    sync = SyncService(db)
    sync.register_device(a, "device a", device_id="dev_a")
    event = envelope(event_id="evt_cross", student_id=b, device_id="dev_a")
    with pytest.raises(DeviceNotFoundError):
        sync.process_batch(
            SyncRequest(
                device_id="dev_a",
                student_id=b,
                events=[SyncEventEnvelope(**event)],
            )
        )
