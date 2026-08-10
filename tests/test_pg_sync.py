"""SyncService on PostgreSQL — device registration, event batch round trip,
and snapshot delivery."""
from __future__ import annotations

import hashlib
import json
import threading

import psycopg
import pytest

from app.infrastructure import pg
from app.infrastructure.migration_runner import migrate_database
from app.infrastructure.pg import transaction
from app.memory.outbox import student_advisory_lock
from app.models import StudentCreate
from app.repository import StudentRepository
from app.sync.service import (
    EventValidationError,
    StudentInactiveError,
    SyncService,
    _event_savepoint,
)
from app.sync.protocol import SyncEventEnvelope as SyncEnvelope
from app.sync.protocol import SyncErrorCode, SyncRequest
from tests.pg_test_helpers import unique_tenant_id

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
    session_id: str = "session_01",
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
        "session_id": session_id,
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


def _process_as(
    service: SyncService,
    student_id: str,
    device_id: str,
    events: list[dict],
):
    return service.process_batch(
        SyncRequest(
            device_id=device_id,
            student_id=student_id,
            events=[SyncEnvelope(**e) for e in events],
        )
    )


def _seed_student_with_id(service: SyncService, student_id: str) -> None:
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
            (student_id, "Second Student", 20, 1200, now, now),
        )


def _seed_memory_fact(service: SyncService) -> None:
    now = "2026-08-07T15:00:00+08:00"
    with transaction(service.connection):
        service.connection.execute(
            """
            INSERT INTO student_memory_facts (
                tenant_id, fact_id, student_id, category, normalized_key, fact_text,
                confidence, supporting_episode_ids_json,
                contradicting_episode_ids_json, evidence_count,
                contradiction_count, status, first_observed_at,
                last_observed_at, version
            ) VALUES (
                current_setting('app.tenant_id'), %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                "fact_real_01",
                STUDENT_ID,
                "misconception_intervention",
                "linear_equations\x1fsign_error\x1fworked_example",
                "Student improves linear-equation sign errors after a worked example.",
                0.8,
                json.dumps(["episode_01"]),
                json.dumps([]),
                2,
                0,
                "stable",
                now,
                now,
                3,
            ),
        )


def test_sync_service_instances_share_same_student_lock_registry() -> None:
    first = SyncService(object())
    second = SyncService(object())

    assert first._student_lock(STUDENT_ID) is second._student_lock(STUDENT_ID)


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


def test_batch_snapshot_serializes_real_memory_facts(service: SyncService) -> None:
    _seed_student(service)
    _seed_memory_fact(service)
    service.register_device(STUDENT_ID, "phone", device_id=DEVICE_A)

    response = _process(service, [_envelope(event_id="evt_with_fact")])

    assert response.accepted_event_ids == ["evt_with_fact"]
    assert response.memory_snapshot["facts"] == [
        {
            "fact_id": "fact_real_01",
            "student_id": STUDENT_ID,
            "category": "misconception_intervention",
            "normalized_key": "linear_equations\x1fsign_error\x1fworked_example",
            "fact_text": "Student improves linear-equation sign errors after a worked example.",
            "confidence": 0.8,
            "supporting_episode_ids": ["episode_01"],
            "contradicting_episode_ids": [],
            "evidence_count": 2,
            "contradiction_count": 0,
            "status": "stable",
            "version": 3,
        }
    ]
    assert service.connection.execute(
        "SELECT COUNT(*) AS total FROM learning_events WHERE event_id = %s",
        ("evt_with_fact",),
    ).fetchone()["total"] == 1


def test_sync_rejects_cross_student_session_without_mutating_owner(
    service: SyncService,
) -> None:
    _seed_student(service)
    _seed_student_with_id(service, "student_02")
    service.register_device(STUDENT_ID, "phone", device_id=DEVICE_A)
    service.register_device("student_02", "tablet", device_id="device_b")

    owner_event = _envelope(
        event_id="evt_student_b",
        device_id="device_b",
        session_id="session_b",
    )
    owner_response = _process_as(service, "student_02", "device_b", [owner_event])
    assert owner_response.accepted_event_ids == ["evt_student_b"]
    before = service.connection.execute(
        "SELECT student_id, session_state FROM study_sessions WHERE session_id = %s",
        ("session_b",),
    ).fetchone()

    forged = _envelope(
        event_id="evt_cross_student_session",
        session_id="session_b",
        device_sequence=1,
    )
    response = _process(service, [forged])

    assert response.accepted_event_ids == []
    assert response.rejected_events[0].event_id == "evt_cross_student_session"
    assert response.rejected_events[0].code == SyncErrorCode.INVALID_SCHEMA.value
    assert response.rejected_events[0].retryable is False
    after = service.connection.execute(
        "SELECT student_id, session_state FROM study_sessions WHERE session_id = %s",
        ("session_b",),
    ).fetchone()
    assert dict(after) == dict(before)
    assert service.connection.execute(
        "SELECT COUNT(*) AS total FROM answer_attempts WHERE event_id = %s",
        ("evt_cross_student_session",),
    ).fetchone()["total"] == 0
    assert service.connection.execute(
        "SELECT COUNT(*) AS total FROM learning_events WHERE event_id = %s",
        ("evt_cross_student_session",),
    ).fetchone()["total"] == 0


def test_dependency_check_is_scoped_to_authoritative_student(
    service: SyncService,
) -> None:
    _seed_student(service)
    _seed_student_with_id(service, "student_02")
    service.register_device(STUDENT_ID, "phone", device_id=DEVICE_A)
    service.register_device("student_02", "tablet", device_id="device_b")

    owner_response = _process_as(
        service,
        "student_02",
        "device_b",
        [_envelope(event_id="evt_student_b_dependency", device_id="device_b")],
    )
    assert owner_response.accepted_event_ids == ["evt_student_b_dependency"]
    assert service.events.learning_event_exists(
        "evt_student_b_dependency", student_id="student_02"
    ) is True
    assert service.events.learning_event_exists(
        "evt_student_b_dependency", student_id=STUDENT_ID
    ) is False

    forged = _envelope(event_id="evt_dependency_from_other_student")
    forged["depends_on_event_ids"] = ["evt_student_b_dependency"]
    response = _process(service, [forged])

    assert response.accepted_event_ids == []
    assert response.rejected_events[0].code == SyncErrorCode.INVALID_SCHEMA.value
    assert response.rejected_events[0].retryable is False


def test_foreign_learning_event_id_rejects_only_that_event(
    service: SyncService,
) -> None:
    _seed_student(service)
    _seed_student_with_id(service, "student_02")
    service.register_device(STUDENT_ID, "phone", device_id=DEVICE_A)
    service.register_device("student_02", "tablet", device_id="device_b")

    owner_event_id = "evt_foreign_learning_id"
    owner_response = _process_as(
        service,
        "student_02",
        "device_b",
        [_envelope(event_id=owner_event_id, device_id="device_b", session_id="session_b")],
    )
    assert owner_response.accepted_event_ids == [owner_event_id]

    collision = _envelope(event_id=owner_event_id, device_sequence=1)
    sibling = _envelope(event_id="evt_after_foreign_learning_id", device_sequence=2)
    response = _process(service, [collision, sibling])

    assert response.accepted_event_ids == ["evt_after_foreign_learning_id"]
    assert response.rejected_events[0].event_id == owner_event_id
    assert response.rejected_events[0].code == SyncErrorCode.INVALID_SCHEMA.value
    assert response.rejected_events[0].retryable is False
    owner_row = service.connection.execute(
        "SELECT student_id FROM learning_events WHERE event_id = %s",
        (owner_event_id,),
    ).fetchone()
    assert owner_row["student_id"] == "student_02"
    assert service.connection.execute(
        "SELECT COUNT(*) AS total FROM learning_events WHERE event_id = %s",
        ("evt_after_foreign_learning_id",),
    ).fetchone()["total"] == 1
    assert service.connection.execute(
        "SELECT last_device_sequence FROM devices "
        "WHERE device_id = %s AND student_id = %s",
        (DEVICE_A, STUDENT_ID),
    ).fetchone()["last_device_sequence"] == 2


def test_foreign_attempt_id_rejects_only_that_event(
    service: SyncService,
) -> None:
    _seed_student(service)
    _seed_student_with_id(service, "student_02")
    service.register_device(STUDENT_ID, "phone", device_id=DEVICE_A)
    service.register_device("student_02", "tablet", device_id="device_b")

    foreign_attempt_id = "attempt_foreign_student"
    owner_event = _envelope(
        event_id="evt_foreign_attempt_owner",
        device_id="device_b",
        session_id="session_b",
        payload={
            "question_id": Q_LINEAR,
            "question_version": 1,
            "selected_choice_id": "A",
            "hint_level": 0,
            "attempt_id": foreign_attempt_id,
        },
    )
    owner_response = _process_as(service, "student_02", "device_b", [owner_event])
    assert owner_response.accepted_event_ids == ["evt_foreign_attempt_owner"]

    collision = _envelope(
        event_id="evt_foreign_attempt_collision",
        device_sequence=1,
        payload={
            "question_id": Q_LINEAR,
            "question_version": 1,
            "selected_choice_id": "A",
            "hint_level": 0,
            "attempt_id": foreign_attempt_id,
        },
    )
    sibling = _envelope(event_id="evt_after_foreign_attempt", device_sequence=2)
    response = _process(service, [collision, sibling])

    assert response.accepted_event_ids == ["evt_after_foreign_attempt"]
    assert response.rejected_events[0].event_id == "evt_foreign_attempt_collision"
    assert response.rejected_events[0].code == SyncErrorCode.INVALID_SCHEMA.value
    assert response.rejected_events[0].retryable is False
    owner_row = service.connection.execute(
        "SELECT student_id FROM answer_attempts WHERE attempt_id = %s",
        (foreign_attempt_id,),
    ).fetchone()
    assert owner_row["student_id"] == "student_02"
    assert service.connection.execute(
        "SELECT COUNT(*) AS total FROM learning_events WHERE event_id = %s",
        ("evt_foreign_attempt_collision",),
    ).fetchone()["total"] == 0
    assert service.connection.execute(
        "SELECT last_device_sequence FROM devices "
        "WHERE device_id = %s AND student_id = %s",
        (DEVICE_A, STUDENT_ID),
    ).fetchone()["last_device_sequence"] == 2


@pytest.mark.parametrize(
    ("collision_kind", "event_type"),
    [
        ("event", "ANSWER_SUBMITTED"),
        ("attempt", "ANSWER_SUBMITTED"),
        ("session", "CONTENT_PRESENTED"),
    ],
)
def test_hidden_cross_tenant_global_ids_are_rejected_without_rls_bypass(
    isolated_pg_database,
    pg_tenant,
    collision_kind: str,
    event_type: str,
) -> None:
    tenant_b = unique_tenant_id("task2_collision_tenant_b")
    connection_a = _tenant_connection(isolated_pg_database, pg_tenant)
    connection_b = _tenant_connection(isolated_pg_database, tenant_b)
    try:
        student_a = StudentRepository(connection_a).create(
            StudentCreate(name="Tenant A Student", daily_minutes=15, target_score=1100)
        )
        student_b = StudentRepository(connection_b).create(
            StudentCreate(name="Tenant B Student", daily_minutes=15, target_score=1100)
        )
        service_a = SyncService(connection_a)
        service_b = SyncService(connection_b)
        device_a = "task2_collision_device_a"
        device_b = "task2_collision_device_b"
        service_a.register_device(student_a.id, "phone", device_id=device_a)
        service_b.register_device(student_b.id, "phone", device_id=device_b)

        shared_event_id = "task2_shared_event_id"
        shared_attempt_id = "task2_shared_attempt_id"
        shared_session_id = "task2_shared_session_id"
        if collision_kind == "event":
            owner_event_id = shared_event_id
            owner_session_id = "task2_tenant_a_event_session"
            collision_event_id = shared_event_id
            collision_session_id = "task2_tenant_b_event_session"
            owner_attempt_id = "task2_tenant_a_event_attempt"
            collision_attempt_id = "task2_tenant_b_event_attempt"
        elif collision_kind == "attempt":
            owner_event_id = "task2_tenant_a_attempt_owner"
            owner_session_id = "task2_tenant_a_attempt_session"
            collision_event_id = "task2_tenant_b_attempt_collision"
            collision_session_id = "task2_tenant_b_attempt_session"
            owner_attempt_id = shared_attempt_id
            collision_attempt_id = shared_attempt_id
        else:
            owner_event_id = "task2_tenant_a_session_owner"
            owner_session_id = shared_session_id
            collision_event_id = "task2_tenant_b_session_collision"
            collision_session_id = shared_session_id
            owner_attempt_id = "task2_tenant_a_session_attempt"
            collision_attempt_id = "task2_tenant_b_session_attempt"

        def payload(attempt_id: str) -> dict:
            return {
                "question_id": Q_LINEAR,
                "question_version": 1,
                "selected_choice_id": "A",
                "hint_level": 0,
                "attempt_id": attempt_id,
            }

        owner = _envelope(
            event_id=owner_event_id,
            device_id=device_a,
            session_id=owner_session_id,
            event_type=event_type,
            payload=payload(owner_attempt_id),
        )
        owner_response = _process_as(service_a, student_a.id, device_a, [owner])
        assert owner_response.accepted_event_ids == [owner_event_id]

        collision = _envelope(
            event_id=collision_event_id,
            device_id=device_b,
            device_sequence=1,
            session_id=collision_session_id,
            event_type=event_type,
            payload=payload(collision_attempt_id),
        )
        sibling_id = f"task2_{collision_kind}_sibling"
        sibling = _envelope(
            event_id=sibling_id,
            device_id=device_b,
            device_sequence=2,
            session_id=f"task2_{collision_kind}_sibling_session",
            event_type=event_type,
            payload=payload(f"task2_{collision_kind}_sibling_attempt"),
        )

        response = _process_as(service_b, student_b.id, device_b, [collision, sibling])

        assert response.accepted_event_ids == [sibling_id]
        assert len(response.rejected_events) == 1
        assert response.rejected_events[0].event_id == collision_event_id
        assert response.rejected_events[0].code == SyncErrorCode.INVALID_SCHEMA.value
        assert response.rejected_events[0].retryable is False
        assert connection_b.execute(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        ).fetchone() == {"rolsuper": False, "rolbypassrls": False}

        assert connection_b.execute(
            "SELECT event_id FROM learning_events WHERE event_id = %s",
            (owner_event_id,),
        ).fetchone() is None
        assert connection_b.execute(
            "SELECT attempt_id FROM answer_attempts WHERE attempt_id = %s",
            (owner_attempt_id,),
        ).fetchone() is None
        assert connection_b.execute(
            "SELECT session_id FROM study_sessions WHERE session_id = %s",
            (owner_session_id,),
        ).fetchone() is None
        assert connection_b.execute(
            "SELECT COUNT(*) AS total FROM learning_events WHERE event_id = %s",
            (sibling_id,),
        ).fetchone()["total"] == 1
    finally:
        pg.quiet_close(connection_a)
        pg.quiet_close(connection_b)


def test_concurrent_foreign_event_id_race_rejects_only_loser_event(
    isolated_pg_database,
    pg_tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection_a = _tenant_connection(isolated_pg_database, pg_tenant)
    connection_b = _tenant_connection(isolated_pg_database, pg_tenant)
    try:
        student_a = StudentRepository(connection_a).create(
            StudentCreate(name="Race Student A", daily_minutes=15, target_score=1100)
        )
        student_b = StudentRepository(connection_b).create(
            StudentCreate(name="Race Student B", daily_minutes=15, target_score=1100)
        )
        service_a = SyncService(connection_a)
        service_b = SyncService(connection_b)
        device_a = "race_device_a"
        device_b = "race_device_b"
        service_a.register_device(student_a.id, "phone", device_id=device_a)
        service_b.register_device(student_b.id, "phone", device_id=device_b)

        shared_event_id = "evt_concurrent_global_id"
        sibling_a = "evt_race_sibling_a"
        sibling_b = "evt_race_sibling_b"
        request_a = SyncRequest(
            device_id=device_a,
            student_id=student_a.id,
            events=[
                SyncEnvelope(
                    **_envelope(
                        event_id=shared_event_id,
                        device_sequence=1,
                        session_id="race_session_a",
                    )
                ),
                SyncEnvelope(
                    **_envelope(
                        event_id=sibling_a,
                        device_sequence=2,
                        session_id="race_session_a",
                    )
                ),
            ],
        )
        request_b = SyncRequest(
            device_id=device_b,
            student_id=student_b.id,
            events=[
                SyncEnvelope(
                    **_envelope(
                        event_id=shared_event_id,
                        device_sequence=1,
                        session_id="race_session_b",
                    )
                ),
                SyncEnvelope(
                    **_envelope(
                        event_id=sibling_b,
                        device_sequence=2,
                        session_id="race_session_b",
                    )
                ),
            ],
        )

        barrier = threading.Barrier(2)

        def gate_first_insert(service: SyncService):
            original = service._insert_learning_event_row
            first_call = True
            call_lock = threading.Lock()

            def gated(*args, **kwargs):  # noqa: ANN002, ANN003
                nonlocal first_call
                with call_lock:
                    wait = first_call
                    first_call = False
                if wait:
                    barrier.wait(timeout=5)
                return original(*args, **kwargs)

            return gated

        monkeypatch.setattr(service_a, "_insert_learning_event_row", gate_first_insert(service_a))
        monkeypatch.setattr(service_b, "_insert_learning_event_row", gate_first_insert(service_b))

        thread_a, finished_a, result_a, errors_a = _run_in_thread(
            lambda: service_a.process_batch(request_a)
        )
        thread_b, finished_b, result_b, errors_b = _run_in_thread(
            lambda: service_b.process_batch(request_b)
        )
        thread_a.join(timeout=10)
        thread_b.join(timeout=10)

        assert not thread_a.is_alive()
        assert not thread_b.is_alive()
        assert not errors_a
        assert not errors_b
        response_a = result_a[0]
        response_b = result_b[0]
        responses = {
            sibling_a: response_a,
            sibling_b: response_b,
        }
        winner = next(
            response
            for response in (response_a, response_b)
            if shared_event_id in response.accepted_event_ids
        )
        loser = next(
            response
            for response in (response_a, response_b)
            if shared_event_id not in response.accepted_event_ids
        )
        assert len(winner.accepted_event_ids) == 2
        assert winner.accepted_event_ids[0] == shared_event_id
        assert loser.accepted_event_ids in ([sibling_a], [sibling_b])
        assert len(loser.rejected_events) == 1
        assert loser.rejected_events[0].event_id == shared_event_id
        assert loser.rejected_events[0].code == SyncErrorCode.INVALID_SCHEMA.value
        assert loser.rejected_events[0].retryable is False
        assert set(responses[sibling_a].accepted_event_ids) >= {sibling_a}
        assert set(responses[sibling_b].accepted_event_ids) >= {sibling_b}

        with transaction(connection_a):
            assert connection_a.execute(
                "SELECT COUNT(*) AS total FROM learning_events "
                "WHERE event_id IN (%s, %s, %s)",
                (shared_event_id, sibling_a, sibling_b),
            ).fetchone()["total"] == 3
            assert connection_a.execute(
                "SELECT COUNT(*) AS total FROM devices "
                "WHERE device_id IN (%s, %s) AND last_device_sequence = 2",
                (device_a, device_b),
            ).fetchone()["total"] == 2
    finally:
        pg.quiet_close(connection_a)
        pg.quiet_close(connection_b)


@pytest.mark.parametrize("failing_cleanup", ["ROLLBACK TO SAVEPOINT", "RELEASE SAVEPOINT"])
def test_savepoint_cleanup_preserves_primary_exception(failing_cleanup: str) -> None:
    class FailingCleanupConnection:
        def execute(self, statement: str):
            if statement.startswith(failing_cleanup):
                raise RuntimeError("savepoint cleanup failure")
            return None

    with pytest.raises(ValueError, match="primary event failure"):
        with _event_savepoint(FailingCleanupConnection(), "sync_event_test"):
            raise ValueError("primary event failure")


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


def _tenant_connection(resource, tenant_id: str):
    connection = resource.connect_app()
    connection.execute(
        "SELECT set_config('app.tenant_id', %s, false)", (tenant_id,)
    )
    connection.commit()
    return connection


def _run_in_thread(function):
    started = threading.Event()
    finished = threading.Event()
    result: list[object] = []
    errors: list[BaseException] = []

    def runner() -> None:
        started.set()
        try:
            result.append(function())
        except BaseException as exc:  # pragma: no cover - asserted by caller
            errors.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    assert started.wait(timeout=2)
    return thread, finished, result, errors


def test_register_device_waits_for_shared_postgres_student_lock(
    isolated_pg_database,
    pg_tenant,
) -> None:
    connection_a = _tenant_connection(isolated_pg_database, pg_tenant)
    connection_b = _tenant_connection(isolated_pg_database, pg_tenant)
    try:
        student = StudentRepository(connection_a).create(
            StudentCreate(name="Lock Student", daily_minutes=15, target_score=1100)
        )
        service_b = SyncService(connection_b)

        with student_advisory_lock(connection_a, student.id):
            thread, finished, result, errors = _run_in_thread(
                lambda: service_b.register_device(student.id, "blocked")
            )
            assert not finished.wait(timeout=0.25)

        thread.join(timeout=5)
        assert not thread.is_alive()
        assert not errors
        assert result[0].student_id == student.id
    finally:
        pg.quiet_close(connection_a)
        pg.quiet_close(connection_b)


def test_revoke_device_waits_for_shared_postgres_student_lock(
    isolated_pg_database,
    pg_tenant,
) -> None:
    connection_a = _tenant_connection(isolated_pg_database, pg_tenant)
    connection_b = _tenant_connection(isolated_pg_database, pg_tenant)
    try:
        student = StudentRepository(connection_a).create(
            StudentCreate(name="Revoke Lock Student", daily_minutes=15, target_score=1100)
        )
        service_a = SyncService(connection_a)
        service_b = SyncService(connection_b)
        service_a.register_device(student.id, "phone", device_id="device_lock")

        with student_advisory_lock(connection_a, student.id):
            thread, finished, result, errors = _run_in_thread(
                lambda: service_b.revoke_device("device_lock", student.id)
            )
            assert not finished.wait(timeout=0.25)

        thread.join(timeout=5)
        assert not thread.is_alive()
        assert not errors
        assert result == [None]
    finally:
        pg.quiet_close(connection_a)
        pg.quiet_close(connection_b)


def test_process_batch_waits_for_deletion_and_rejects_after_lock_release(
    isolated_pg_database,
    pg_tenant,
) -> None:
    connection_a = _tenant_connection(isolated_pg_database, pg_tenant)
    connection_b = _tenant_connection(isolated_pg_database, pg_tenant)
    try:
        student = StudentRepository(connection_a).create(
            StudentCreate(name="Sync Lock Student", daily_minutes=15, target_score=1100)
        )
        service_a = SyncService(connection_a)
        service_b = SyncService(connection_b)
        service_a.register_device(student.id, "phone", device_id="device_sync_lock")
        request = SyncRequest(
            device_id="device_sync_lock",
            student_id=student.id,
            events=[],
        )

        with student_advisory_lock(connection_a, student.id):
            thread, finished, result, errors = _run_in_thread(
                lambda: service_b.process_batch(request)
            )
            assert not finished.wait(timeout=0.25)
            connection_a.execute(
                "UPDATE students SET status = 'deletion_pending' WHERE id = %s",
                (student.id,),
            )
            connection_a.commit()

        thread.join(timeout=5)
        assert not thread.is_alive()
        assert not errors
        response = result[0]
        assert response.rejected_events[0].code == SyncErrorCode.UNAUTHORIZED_STUDENT.value
        assert connection_a.execute(
            "SELECT COUNT(*) AS total FROM learning_events WHERE student_id = %s",
            (student.id,),
        ).fetchone()["total"] == 0
    finally:
        pg.quiet_close(connection_a)
        pg.quiet_close(connection_b)


def test_process_batch_preserves_unauthorized_response_when_rollback_fails(
    service: SyncService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_student(service)
    real_connection = service.connection

    class RollbackFailingConnection:
        def __init__(self) -> None:
            self.rollback_calls = 0

        def rollback(self) -> None:
            self.rollback_calls += 1
            real_connection.rollback()
            if self.rollback_calls == 2:
                raise RuntimeError("rollback cleanup failure")

        def __getattr__(self, name: str):
            return getattr(real_connection, name)

    failing_connection = RollbackFailingConnection()
    failing_service = SyncService(failing_connection)

    monkeypatch.setattr(
        failing_service,
        "_student_exists",
        lambda *args, **kwargs: True,  # noqa: ARG005, ANN002, ANN003
    )

    def fail_student_check(*args, **kwargs):  # noqa: ANN002, ANN003
        raise StudentInactiveError("student became inactive during verification")

    monkeypatch.setattr(failing_service, "_verify_device", fail_student_check)

    response = _process(failing_service, [])

    assert response.rejected_events[0].code == SyncErrorCode.UNAUTHORIZED_STUDENT.value
    assert failing_connection.rollback_calls == 1


def test_sync_uses_request_student_and_device_ids_for_stored_rows(
    service: SyncService,
) -> None:
    _seed_student(service)
    service.register_device(STUDENT_ID, "phone", device_id=DEVICE_A)
    forged = _envelope(event_id="evt_authoritative_ids")
    forged["student_id"] = "forged-student"
    forged["device_id"] = "forged-device"

    response = _process(service, [forged])

    assert response.accepted_event_ids == ["evt_authoritative_ids"]
    row = service.connection.execute(
        "SELECT student_id, device_id FROM learning_events WHERE event_id = %s",
        ("evt_authoritative_ids",),
    ).fetchone()
    assert row["student_id"] == STUDENT_ID
    assert row["device_id"] == DEVICE_A


def test_fatal_batch_failure_rolls_back_events_projections_and_sequence(
    service: SyncService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_student(service)
    service.register_device(STUDENT_ID, "phone", device_id=DEVICE_A)

    def fail_advance(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("fatal sequence failure")

    monkeypatch.setattr(service, "_advance_device_sequence", fail_advance)

    with pytest.raises(RuntimeError, match="fatal sequence failure"):
        _process(service, [_envelope(event_id="evt_fatal", device_sequence=1)])

    with transaction(service.connection):
        assert service.connection.execute(
            "SELECT COUNT(*) AS total FROM learning_events WHERE event_id = %s",
            ("evt_fatal",),
        ).fetchone()["total"] == 0
        assert service.connection.execute(
            "SELECT COUNT(*) AS total FROM answer_attempts WHERE event_id = %s",
            ("evt_fatal",),
        ).fetchone()["total"] == 0
        assert service.connection.execute(
            "SELECT last_device_sequence FROM devices "
            "WHERE device_id = %s AND student_id = %s",
            (DEVICE_A, STUDENT_ID),
        ).fetchone()["last_device_sequence"] == 0


def test_generic_value_error_aborts_batch_without_commit(
    service: SyncService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_student(service)
    service.register_device(STUDENT_ID, "phone", device_id=DEVICE_A)
    original_apply_event = service._apply_event

    def fail_with_generic_value_error(*args, **kwargs):  # noqa: ANN002, ANN003
        original_apply_event(*args, **kwargs)
        raise ValueError("unexpected projection value")

    monkeypatch.setattr(service, "_apply_event", fail_with_generic_value_error)

    with pytest.raises(ValueError, match="unexpected projection value"):
        _process(service, [_envelope(event_id="evt_generic_value_error")])

    with transaction(service.connection):
        assert service.connection.execute(
            "SELECT COUNT(*) AS total FROM learning_events WHERE event_id = %s",
            ("evt_generic_value_error",),
        ).fetchone()["total"] == 0
        assert service.connection.execute(
            "SELECT COUNT(*) AS total FROM answer_attempts WHERE event_id = %s",
            ("evt_generic_value_error",),
        ).fetchone()["total"] == 0
        assert service.connection.execute(
            "SELECT COUNT(*) AS total FROM student_skill_states WHERE student_id = %s",
            (STUDENT_ID,),
        ).fetchone()["total"] == 0
        assert service.connection.execute(
            "SELECT last_device_sequence FROM devices "
            "WHERE device_id = %s AND student_id = %s",
            (DEVICE_A, STUDENT_ID),
        ).fetchone()["last_device_sequence"] == 0


def test_valid_and_rejected_events_share_one_batch_transaction(
    service: SyncService,
) -> None:
    _seed_student(service)
    service.register_device(STUDENT_ID, "phone", device_id=DEVICE_A)
    valid = _envelope(event_id="evt_valid", device_sequence=1)
    rejected = _envelope(
        event_id="evt_rejected",
        device_sequence=2,
        payload={"attempt_id": "evt_rejected"},
        question_id=None,
        question_version=None,
    )

    response = _process(service, [valid, rejected])

    assert response.accepted_event_ids == ["evt_valid"]
    assert response.rejected_events[0].event_id == "evt_rejected"
    with transaction(service.connection):
        assert service.connection.execute(
            "SELECT COUNT(*) AS total FROM learning_events "
            "WHERE event_id IN ('evt_valid', 'evt_rejected')"
        ).fetchone()["total"] == 1
        assert service.connection.execute(
            "SELECT last_device_sequence FROM devices "
            "WHERE device_id = %s AND student_id = %s",
            (DEVICE_A, STUDENT_ID),
        ).fetchone()["last_device_sequence"] == 1


def test_rejectable_event_failure_rolls_back_savepoint_and_continues(
    service: SyncService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_student(service)
    service.register_device(STUDENT_ID, "phone", device_id=DEVICE_A)
    invalid = _envelope(event_id="evt_rejectable", device_sequence=1)
    valid = _envelope(event_id="evt_after_reject", device_sequence=2)
    original_apply_event = service._apply_event
    apply_calls = 0

    def fail_once(envelope, accepted, rejected, conflicts, server_events, **kwargs):  # noqa: ANN001
        nonlocal apply_calls
        if apply_calls == 0:
            apply_calls += 1
            original_apply_event(
                envelope,
                accepted,
                rejected,
                conflicts,
                server_events,
                **kwargs,
            )
            conflicts.append(
                {
                    "event_id": envelope.event_id,
                    "conflict_type": "rolled_back",
                }
            )
            server_events.append({"source_event_id": envelope.event_id})
            raise EventValidationError("rejectable projection validation")
        return original_apply_event(
            envelope,
            accepted,
            rejected,
            conflicts,
            server_events,
            **kwargs,
        )

    monkeypatch.setattr(service, "_apply_event", fail_once)

    response = _process(service, [invalid, valid])

    assert response.accepted_event_ids == ["evt_after_reject"]
    assert response.conflicts == []
    assert [event["source_event_id"] for event in response.server_events] == [
        "evt_after_reject"
    ]
    assert len(response.rejected_events) == 1
    assert response.rejected_events[0].event_id == "evt_rejectable"
    assert response.rejected_events[0].code == SyncErrorCode.INTERNAL_RETRYABLE.value
    assert response.rejected_events[0].retryable is True
    with transaction(service.connection):
        assert service.connection.execute(
            "SELECT COUNT(*) AS total FROM learning_events "
            "WHERE event_id = %s",
            ("evt_rejectable",),
        ).fetchone()["total"] == 0
        assert service.connection.execute(
            "SELECT COUNT(*) AS total FROM answer_attempts "
            "WHERE event_id = %s",
            ("evt_rejectable",),
        ).fetchone()["total"] == 0
        assert service.connection.execute(
            "SELECT COUNT(*) AS total FROM learning_events "
            "WHERE event_id = %s",
            ("evt_after_reject",),
        ).fetchone()["total"] == 1
        assert service.connection.execute(
            "SELECT last_device_sequence FROM devices "
            "WHERE device_id = %s AND student_id = %s",
            (DEVICE_A, STUDENT_ID),
        ).fetchone()["last_device_sequence"] == 2


def test_unexpected_answer_key_failure_rolls_back_the_whole_batch(
    service: SyncService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_student(service)
    service.register_device(STUDENT_ID, "phone", device_id=DEVICE_A)

    def fail_scoring(*args, **kwargs):  # noqa: ANN002, ANN003
        raise OSError("answer-key read failed")

    monkeypatch.setattr(service.answer_keys, "score", fail_scoring)

    with pytest.raises(OSError, match="answer-key read failed"):
        _process(service, [_envelope(event_id="evt_os_error", device_sequence=1)])

    with transaction(service.connection):
        assert service.connection.execute(
            "SELECT COUNT(*) AS total FROM learning_events WHERE event_id = %s",
            ("evt_os_error",),
        ).fetchone()["total"] == 0
        assert service.connection.execute(
            "SELECT COUNT(*) AS total FROM answer_attempts WHERE event_id = %s",
            ("evt_os_error",),
        ).fetchone()["total"] == 0
        assert service.connection.execute(
            "SELECT COUNT(*) AS total FROM student_skill_states WHERE student_id = %s",
            (STUDENT_ID,),
        ).fetchone()["total"] == 0
        assert service.connection.execute(
            "SELECT COUNT(*) AS total FROM study_sessions WHERE student_id = %s",
            (STUDENT_ID,),
        ).fetchone()["total"] == 0
        assert service.connection.execute(
            "SELECT last_device_sequence FROM devices "
            "WHERE device_id = %s AND student_id = %s",
            (DEVICE_A, STUDENT_ID),
        ).fetchone()["last_device_sequence"] == 0


def test_snapshot_pg_failure_rolls_back_batch_and_propagates(
    service: SyncService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_student(service)
    service.register_device(STUDENT_ID, "phone", device_id=DEVICE_A)

    def fail_get_facts(*args, **kwargs):  # noqa: ANN002, ANN003
        raise psycopg.OperationalError("authoritative facts query failed")

    monkeypatch.setattr(service.memory, "get_facts", fail_get_facts)

    with pytest.raises(psycopg.OperationalError, match="authoritative facts query failed"):
        _process(service, [_envelope(event_id="evt_snapshot_pg_failure")])

    with transaction(service.connection):
        assert service.connection.execute(
            "SELECT COUNT(*) AS total FROM learning_events WHERE event_id = %s",
            ("evt_snapshot_pg_failure",),
        ).fetchone()["total"] == 0
        assert service.connection.execute(
            "SELECT COUNT(*) AS total FROM answer_attempts WHERE event_id = %s",
            ("evt_snapshot_pg_failure",),
        ).fetchone()["total"] == 0
        assert service.connection.execute(
            "SELECT COUNT(*) AS total FROM student_skill_states WHERE student_id = %s",
            (STUDENT_ID,),
        ).fetchone()["total"] == 0
        assert service.connection.execute(
            "SELECT COUNT(*) AS total FROM study_sessions WHERE student_id = %s",
            (STUDENT_ID,),
        ).fetchone()["total"] == 0
        assert service.connection.execute(
            "SELECT last_device_sequence FROM devices "
            "WHERE device_id = %s AND student_id = %s",
            (DEVICE_A, STUDENT_ID),
        ).fetchone()["last_device_sequence"] == 0


def test_independent_student_row_lock_serializes_same_device_sequence(
    isolated_pg_database,
    pg_tenant,
) -> None:
    connection_a = _tenant_connection(isolated_pg_database, pg_tenant)
    connection_b = _tenant_connection(isolated_pg_database, pg_tenant)
    connection_c = _tenant_connection(isolated_pg_database, pg_tenant)
    try:
        student = StudentRepository(connection_a).create(
            StudentCreate(name="Sequence Lock Student", daily_minutes=15, target_score=1100)
        )
        service_a = SyncService(connection_a)
        service_b = SyncService(connection_b)
        service_c = SyncService(connection_c)
        service_a.register_device(student.id, "phone", device_id="device_sequence_lock")
        request_b = SyncRequest(
            device_id="device_sequence_lock",
            student_id=student.id,
            events=[
                SyncEnvelope(
                    **_envelope(
                        event_id="evt_sequence_b",
                        device_sequence=1,
                    )
                )
            ],
        )
        request_c = SyncRequest(
            device_id="device_sequence_lock",
            student_id=student.id,
            events=[
                SyncEnvelope(
                    **_envelope(
                        event_id="evt_sequence_c",
                        device_sequence=1,
                    )
                )
            ],
        )

        connection_a.execute(
            """
            SELECT status
            FROM students
            WHERE id = %s
              AND tenant_id = current_setting('app.tenant_id', true)
            FOR UPDATE
            """,
            (student.id,),
        ).fetchone()
        thread_b, finished_b, result_b, errors_b = _run_in_thread(
            lambda: service_b.process_batch(request_b)
        )
        thread_c, finished_c, result_c, errors_c = _run_in_thread(
            lambda: service_c.process_batch(request_c)
        )
        assert not finished_b.wait(timeout=0.25)
        assert not finished_c.wait(timeout=0.25)

        connection_a.rollback()
        thread_b.join(timeout=5)
        thread_c.join(timeout=5)
        assert not thread_b.is_alive()
        assert not thread_c.is_alive()
        assert not errors_b
        assert not errors_c
        responses = [result_b[0], result_c[0]]
        accepted_ids = [
            response.accepted_event_ids[0]
            for response in responses
            if response.accepted_event_ids
        ]
        assert len(accepted_ids) == 1
        assert accepted_ids[0] in {"evt_sequence_b", "evt_sequence_c"}
        assert sum(bool(response.rejected_events) for response in responses) == 1
        rejected_response = next(response for response in responses if response.rejected_events)
        assert rejected_response.rejected_events[0].code == SyncErrorCode.INVALID_SCHEMA.value
        with transaction(connection_a):
            assert connection_a.execute(
                "SELECT COUNT(*) AS total FROM learning_events "
                "WHERE event_id IN ('evt_sequence_b', 'evt_sequence_c')"
            ).fetchone()["total"] == 1
            assert connection_a.execute(
                "SELECT last_device_sequence FROM devices "
                "WHERE device_id = %s AND student_id = %s",
                ("device_sequence_lock", student.id),
            ).fetchone()["last_device_sequence"] == 1
    finally:
        pg.quiet_close(connection_a)
        pg.quiet_close(connection_b)
        pg.quiet_close(connection_c)
