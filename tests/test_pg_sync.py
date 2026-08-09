"""SyncService on PostgreSQL — device registration, event batch round trip,
and snapshot delivery."""
from __future__ import annotations

import hashlib
import json

import pytest

from app.infrastructure import pg
from app.infrastructure.migration_runner import migrate_database
from app.sync.service import SyncService
from app.sync.protocol import SyncEventEnvelope as SyncEnvelope
from app.sync.protocol import SyncRequest

PACK_VERSION = "0.1.0"
Q_LINEAR = "sync.linear.001"

STUDENT_ID = "student_01"
DEVICE_A = "device_a"


def _integrity(event_type: str, payload: dict) -> str:
    digest = hashlib.sha256()
    digest.update(event_type.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return f"sha256:{digest.hexdigest()}"


def _envelope(
    *,
    event_id: str,
    device_id: str = DEVICE_A,
    device_sequence: int = 1,
    event_type: str = "ANSWER_SUBMITTED",
    payload: dict | None = None,
    question_id: str | None = Q_LINEAR,
    question_version: int | None = 1,
) -> dict:
    payload = payload or {
        "question_id": question_id,
        "question_version": question_version,
        "selected_choice_id": "A",
        "hint_level": 0,
        "attempt_id": event_id,
    }
    return {
        "event_id": event_id,
        "student_id": STUDENT_ID,
        "session_id": "session_01",
        "session_branch_id": "branch_" + device_id,
        "device_id": device_id,
        "device_sequence": device_sequence,
        "event_type": event_type,
        "payload": payload,
        "content_pack_version": PACK_VERSION,
        "question_id": question_id,
        "question_version": question_version,
        "policy_version": "offline-policy-v1",
        "depends_on_event_ids": [],
        "device_occurred_at": "2026-08-07T16:00:00+08:00",
        "integrity_hash": _integrity(event_type, payload),
    }


@pytest.fixture()
def service():
    admin = pg.connect_admin()
    migrate_database(admin)
    admin.close()
    conn = pg.connect()
    conn.execute("SELECT set_config('app.tenant_id', 'tenant_test', false)")
    conn.commit()
    yield SyncService(conn)
    conn.rollback()
    admin = pg.connect_admin()
    admin.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    admin.commit()
    admin.close()
    conn.close()


def _seed_student(service: SyncService) -> None:
    from app.infrastructure.pg import transaction

    now = "2026-08-07T15:00:00+08:00"
    with transaction(service.connection):
        service.connection.execute(
            """
            INSERT INTO students (
                tenant_id, id, name, daily_minutes, target_score, mastery_json,
                status, created_at, updated_at
            ) VALUES (
                current_setting('app.tenant_id'), %s, %s, %s, %s, '{}', 'active', %s, %s
            )
            """,
            (STUDENT_ID, "Test Student", 20, 1200, now, now),
        )
        service.connection.execute(
            """
            INSERT INTO learning_events (
                tenant_id, event_id, student_id, session_id, event_type, payload_json,
                policy_version, content_version, occurred_at, received_at,
                device_id, device_sequence, origin, integrity_hash
            ) VALUES (
                current_setting('app.tenant_id'), %s, %s, '', 'STUDENT_CREATED', %s,
                'policy-0.1.0', NULL, %s, %s, NULL, NULL, 'online', %s
            )
            """,
            (
                f"evt_seed_{STUDENT_ID}",
                STUDENT_ID,
                json.dumps({"name": "Test Student", "daily_minutes": 20, "target_score": 1200}),
                now,
                now,
                _integrity(
                    "STUDENT_CREATED",
                    {"name": "Test Student", "daily_minutes": 20, "target_score": 1200},
                ),
            ),
        )


def _process(service: SyncService, events: list[dict], device_id: str = DEVICE_A):
    return service.process_batch(
        SyncRequest(
            device_id=device_id,
            student_id=STUDENT_ID,
            events=[SyncEnvelope(**e) for e in events],
        )
    )


def test_register_and_verify_device(service: SyncService) -> None:
    _seed_student(service)
    registration = service.register_device(STUDENT_ID, "phone", device_id=DEVICE_A)
    assert registration.status == "active"
    service._verify_device(DEVICE_A, STUDENT_ID)


def test_revoke_device_blocks_batch(service: SyncService) -> None:
    _seed_student(service)
    service.register_device(STUDENT_ID, "phone", device_id=DEVICE_A)
    service.revoke_device(DEVICE_A, STUDENT_ID)
    with pytest.raises(Exception):
        _process(service, [_envelope(event_id="evt_1")])


def test_process_batch_round_trip(service: SyncService) -> None:
    _seed_student(service)
    service.register_device(STUDENT_ID, "phone", device_id=DEVICE_A)
    response = _process(service, [_envelope(event_id="evt_1")])
    assert response.accepted_event_ids == ["evt_1"]
    assert response.new_snapshot_version >= 1
    assert response.new_server_cursor.startswith("cursor_")
    snapshot = service.build_snapshot(STUDENT_ID)
    linear = [s for s in snapshot.skill_states if s["skill"] == "linear_equations"]
    assert len(linear) == 1
    assert linear[0]["evidence_count"] == 1


def test_duplicate_event_acknowledged_not_reapplied(service: SyncService) -> None:
    _seed_student(service)
    service.register_device(STUDENT_ID, "phone", device_id=DEVICE_A)
    first = _process(service, [_envelope(event_id="evt_1")])
    assert first.accepted_event_ids == ["evt_1"]
    second = _process(service, [_envelope(event_id="evt_1")])
    assert second.duplicate_event_ids == ["evt_1"]
    row = service.connection.execute(
        "SELECT COUNT(*) AS total FROM learning_events WHERE event_id = 'evt_1'"
    ).fetchone()
    assert row["total"] == 1
    service.connection.commit()


def test_parallel_branch_conflict_recorded(service: SyncService) -> None:
    from app.sync.protocol import SyncRequest

    _seed_student(service)
    service.register_device(STUDENT_ID, "device a", device_id=DEVICE_A)
    service.register_device(STUDENT_ID, "device b", device_id="device_b")
    first = _process(service, [_envelope(event_id="evt_1")])
    second = service.process_batch(
        SyncRequest(
            device_id="device_b",
            student_id=STUDENT_ID,
            events=[
                SyncEnvelope(
                    **_envelope(
                        event_id="evt_2",
                        device_id="device_b",
                        device_sequence=1,
                    )
                )
            ],
        )
    )
    assert first.accepted_event_ids == ["evt_1"]
    assert second.accepted_event_ids == ["evt_2"]
    assert any(c.conflict_type == "PARALLEL_ATTEMPT_DETECTED" for c in second.conflicts)
    rows = service.connection.execute(
        "SELECT conflict_type FROM sync_conflicts WHERE event_id = 'evt_2'"
    ).fetchall()
    assert [r["conflict_type"] for r in rows] == ["PARALLEL_ATTEMPT_DETECTED"]
    service.connection.commit()


def test_snapshot_includes_strategy_memory(service: SyncService) -> None:
    _seed_student(service)
    service.register_device(STUDENT_ID, "phone", device_id=DEVICE_A)
    _process(service, [_envelope(event_id="evt_1")])
    snapshot = service.build_snapshot(STUDENT_ID)
    assert "intervention_stats" in snapshot.strategy_memory
    assert "facts" in snapshot.strategy_memory


def test_missing_dependency_queued_retryable(service: SyncService) -> None:
    _seed_student(service)
    service.register_device(STUDENT_ID, "phone", device_id=DEVICE_A)
    envelope = _envelope(event_id="evt_2")
    envelope["depends_on_event_ids"] = ["evt_1"]
    response = _process(service, [envelope])
    assert response.rejected_events[0].code == "MISSING_DEPENDENCY"
    assert response.rejected_events[0].retryable is True
