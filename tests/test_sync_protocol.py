"""Tests for the offline synchronization protocol (SYNC_PROTOCOL.md).

Covers: device registration/revocation, idempotent dedup, version-bound
scoring, integrity verification, dependency checks, repeated attempts,
parallel branches, late events after session completion, refresh/restart
recovery, throttled-network batching, and snapshot delivery.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from app.infrastructure import pg
from app.infrastructure.migration_runner import migrate_database
from app.sync.service import SyncService
from app.sync.versioned_scoring import QuestionVersionError, VersionedAnswerKey

PACK_VERSION = "0.1.0"
Q_LINEAR = "sync.linear.001"
Q_RATIOS = "sync.ratios.001"

STUDENT_ID = "student_01"
DEVICE_A = "device_a"
DEVICE_B = "device_b"
SESSION_ID = "session_01"


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
    session_id: str = SESSION_ID,
    question_id: str | None = Q_LINEAR,
    question_version: int | None = 1,
    depends_on: list[str] | None = None,
    include_hash: bool = True,
) -> dict:
    payload = payload or {
        "question_id": question_id,
        "question_version": question_version,
        "selected_choice_id": "A",
        "hint_level": 0,
        "attempt_id": event_id,
    }
    envelope = {
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
        "depends_on_event_ids": depends_on or [],
        "device_occurred_at": "2026-08-07T16:00:00+08:00",
    }
    if include_hash:
        envelope["integrity_hash"] = _integrity(event_type, payload)
    return envelope


@pytest.fixture()
def service() -> SyncService:
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


@pytest.fixture()
def registered(service: SyncService) -> SyncService:
    service.register_device(STUDENT_ID, "device a", device_id=DEVICE_A)
    service.register_device(STUDENT_ID, "device b", device_id=DEVICE_B)
    return service


def _seed_student(service: SyncService, student_id: str = STUDENT_ID) -> None:
    from app.domain.events import compute_integrity_hash, utc_now_iso
    from app.infrastructure.pg import transaction

    now = utc_now_iso()
    event = {
        "event_id": f"evt_seed_{student_id}",
        "student_id": student_id,
        "session_id": "",
        "event_type": "STUDENT_CREATED",
        "payload": {"name": "Test Student", "daily_minutes": 20, "target_score": 1200},
        "policy_version": "policy-0.1.0",
        "content_version": None,
        "occurred_at": now,
        "received_at": now,
        "device_id": None,
        "device_sequence": None,
        "origin": "online",
        "integrity_hash": compute_integrity_hash(
            "STUDENT_CREATED",
            {"name": "Test Student", "daily_minutes": 20, "target_score": 1200},
        ),
    }
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
            (student_id, "Test Student", 20, 1200, now, now),
        )
        service.connection.execute(
            """
            INSERT INTO learning_events (
                tenant_id, event_id, student_id, session_id, event_type, payload_json,
                policy_version, content_version, occurred_at, received_at,
                device_id, device_sequence, origin, integrity_hash
            ) VALUES (
                current_setting('app.tenant_id'), %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                event["event_id"], event["student_id"], event["session_id"],
                event["event_type"], json.dumps(event["payload"]),
                event["policy_version"], event["content_version"],
                event["occurred_at"], event["received_at"],
                event["device_id"], event["device_sequence"],
                event["origin"], event["integrity_hash"],
            ),
        )


def _process(service: SyncService, events: list[dict], device_id: str = DEVICE_A):
    from app.sync.protocol import SyncRequest

    return service.process_batch(
        SyncRequest(
            device_id=device_id,
            student_id=STUDENT_ID,
            events=[SyncEnvelope(**e) for e in events],
        )
    )


from app.sync.protocol import SyncEventEnvelope as SyncEnvelope  # noqa: E402


# ----------------------------------------------------------------------
# Device lifecycle
# ----------------------------------------------------------------------

def test_register_device(service: SyncService) -> None:
    _seed_student(service)
    registration = service.register_device(STUDENT_ID, "laptop")
    assert registration.device_id.startswith("dev_")
    assert registration.status == "active"
    assert registration.student_id == STUDENT_ID


def test_register_unknown_student_rejected(service: SyncService) -> None:
    with pytest.raises(KeyError):
        service.register_device("nobody", "x")


def test_revoke_device(service: SyncService) -> None:
    _seed_student(service)
    registration = service.register_device(STUDENT_ID, "laptop", device_id=DEVICE_A)
    service.revoke_device(registration.device_id, STUDENT_ID)
    with pytest.raises(DeviceRevokedError):
        _process(service, [_envelope(event_id="evt_1", device_id=DEVICE_A)])


from app.sync.service import DeviceRevokedError  # noqa: E402


def test_unregistered_device_rejected(service: SyncService) -> None:
    _seed_student(service)
    with pytest.raises(DeviceNotFoundError):
        _process(service, [_envelope(event_id="evt_1")])


from app.sync.service import DeviceNotFoundError  # noqa: E402


# ----------------------------------------------------------------------
# Idempotent event synchronization
# ----------------------------------------------------------------------

def test_duplicate_event_is_acknowledged_not_reapplied(service: SyncService) -> None:
    _seed_student(service)
    service.register_device(STUDENT_ID, "d", device_id=DEVICE_A)
    first = _process(service, [_envelope(event_id="evt_1")])
    assert first.accepted_event_ids == ["evt_1"]

    second = _process(service, [_envelope(event_id="evt_1")])
    assert second.duplicate_event_ids == ["evt_1"]
    assert second.accepted_event_ids == []

    with transaction(service.connection) as connection:
        count = connection.execute(
            "SELECT COUNT(*) AS total FROM learning_events WHERE event_id = 'evt_1'"
        ).fetchone()["total"]
    assert count == 1


from app.infrastructure.pg import transaction  # noqa: E402


def test_partial_batch_resumed_after_throttle(service: SyncService) -> None:
    _seed_student(service)
    service.register_device(STUDENT_ID, "d", device_id=DEVICE_A)
    first = _process(service, [_envelope(event_id="evt_1"), _envelope(event_id="evt_2", device_sequence=2)])
    assert first.accepted_event_ids == ["evt_1", "evt_2"]

    resume = _process(service, [_envelope(event_id="evt_2", device_sequence=2)])
    assert resume.duplicate_event_ids == ["evt_2"]


# ----------------------------------------------------------------------
# Version-bound answer scoring
# ----------------------------------------------------------------------

def test_version_bound_correct_answer(service: SyncService) -> None:
    _seed_student(service)
    service.register_device(STUDENT_ID, "d", device_id=DEVICE_A)
    response = _process(service, [_envelope(event_id="evt_1")])
    assert response.accepted_event_ids == ["evt_1"]
    with transaction(service.connection) as connection:
        row = connection.execute(
            "SELECT correct, validity FROM answer_attempts WHERE event_id = 'evt_1'"
        ).fetchone()
    assert row["correct"] == 1
    assert row["validity"] == "valid"


def test_version_bound_wrong_answer_maps_misconception(service: SyncService) -> None:
    _seed_student(service)
    service.register_device(STUDENT_ID, "d", device_id=DEVICE_A)
    envelope = _envelope(
        event_id="evt_1",
        payload={
            "question_id": Q_LINEAR,
            "question_version": 1,
            "selected_choice_id": "B",
            "hint_level": 0,
            "attempt_id": "evt_1",
        },
    )
    _process(service, [envelope])
    with transaction(service.connection) as connection:
        evidence = connection.execute(
            "SELECT misconception FROM misconception_evidence WHERE event_id = 'evt_1'"
        ).fetchone()
    assert evidence["misconception"] == "sign_error"


def test_unknown_question_version_rejected(service: SyncService) -> None:
    _seed_student(service)
    service.register_device(STUDENT_ID, "d", device_id=DEVICE_A)
    envelope = _envelope(event_id="evt_1", question_version=99)
    response = _process(service, [envelope])
    assert response.accepted_event_ids == []
    assert response.rejected_events[0].code == "QUESTION_VERSION_UNKNOWN"
    assert response.rejected_events[0].retryable is False


def test_old_attempt_never_scored_with_newer_key(service: SyncService) -> None:
    _seed_student(service)
    service.register_device(STUDENT_ID, "d", device_id=DEVICE_A)
    old = _envelope(event_id="evt_old", question_id=Q_RATIOS, question_version=1)
    response = _process(service, [old])
    assert response.rejected_events[0].code == "QUESTION_VERSION_UNKNOWN"


def test_unknown_pack_version_rejected(service: SyncService) -> None:
    _seed_student(service)
    service.register_device(STUDENT_ID, "d", device_id=DEVICE_A)
    envelope = _envelope(event_id="evt_1")
    envelope["content_pack_version"] = "9.9.9"
    response = _process(service, [envelope])
    assert response.rejected_events[0].code == "QUESTION_VERSION_UNKNOWN"


# ----------------------------------------------------------------------
# Integrity verification
# ----------------------------------------------------------------------

def test_tampered_integrity_hash_rejected(service: SyncService) -> None:
    _seed_student(service)
    service.register_device(STUDENT_ID, "d", device_id=DEVICE_A)
    envelope = _envelope(event_id="evt_1")
    envelope["payload"]["selected_choice_id"] = "C"
    envelope["integrity_hash"] = _integrity("ANSWER_SUBMITTED", {"original": "payload"})
    response = _process(service, [envelope])
    assert response.rejected_events[0].code == "INVALID_SCHEMA"


# ----------------------------------------------------------------------
# Dependencies
# ----------------------------------------------------------------------

def test_missing_dependency_queued_as_retryable(service: SyncService) -> None:
    _seed_student(service)
    service.register_device(STUDENT_ID, "d", device_id=DEVICE_A)
    envelope = _envelope(event_id="evt_2", depends_on=["evt_1"])
    response = _process(service, [envelope])
    assert response.rejected_events[0].code == "MISSING_DEPENDENCY"
    assert response.rejected_events[0].retryable is True


def test_dependency_satisfied_in_same_batch(service: SyncService) -> None:
    _seed_student(service)
    service.register_device(STUDENT_ID, "d", device_id=DEVICE_A)
    presented = _envelope(
        event_id="evt_1",
        event_type="CONTENT_PRESENTED",
        payload={"question_id": Q_LINEAR},
        question_id=None,
        question_version=None,
    )
    submitted = _envelope(event_id="evt_2", device_sequence=2, depends_on=["evt_1"])
    response = _process(service, [presented, submitted])
    assert "evt_1" in response.accepted_event_ids
    assert "evt_2" in response.accepted_event_ids


def test_same_batch_device_sequence_must_strictly_increase(service: SyncService) -> None:
    _seed_student(service)
    service.register_device(STUDENT_ID, "d", device_id=DEVICE_A)
    first = _envelope(event_id="evt_seq_first", device_sequence=1)
    second = _envelope(event_id="evt_seq_second", device_sequence=1)

    response = _process(service, [second, first])

    assert response.accepted_event_ids == ["evt_seq_first"]
    assert [event.event_id for event in response.rejected_events] == ["evt_seq_second"]
    assert response.rejected_events[0].code == "INVALID_SCHEMA"


# ----------------------------------------------------------------------
# Repeated attempts and parallel branches
# ----------------------------------------------------------------------

def test_repeated_attempt_id_non_scoring(service: SyncService) -> None:
    _seed_student(service)
    service.register_device(STUDENT_ID, "d", device_id=DEVICE_A)
    first = _envelope(
        event_id="evt_1",
        payload={"question_id": Q_LINEAR, "question_version": 1,
                 "selected_choice_id": "A", "hint_level": 0, "attempt_id": "att_1"},
    )
    second = _envelope(
        event_id="evt_2",
        device_sequence=2,
        payload={"question_id": Q_LINEAR, "question_version": 1,
                 "selected_choice_id": "B", "hint_level": 0, "attempt_id": "att_1"},
    )
    response = _process(service, [first, second])
    assert response.accepted_event_ids == ["evt_1", "evt_2"]
    with transaction(service.connection) as connection:
        rows = connection.execute(
            "SELECT event_id, attempt_id, validity, weight FROM answer_attempts "
            "WHERE attempt_id = 'att_1' OR attempt_id LIKE 'att_1#%' ORDER BY event_id"
        ).fetchall()
    assert [row["validity"] for row in rows] == ["valid", "non_scoring_duplicate"]
    assert rows[1]["weight"] == 0.0
    assert rows[1]["attempt_id"].startswith("att_1#dup")
    conflict_types = {c.conflict_type for c in response.conflicts}
    assert "ATTEMPT_ALREADY_SCORED" in conflict_types


def test_parallel_branch_same_question_reduced_weight(service: SyncService) -> None:
    _seed_student(service)
    service.register_device(STUDENT_ID, "device a", device_id=DEVICE_A)
    service.register_device(STUDENT_ID, "device b", device_id=DEVICE_B)
    session = SESSION_ID
    device_a = _envelope(event_id="evt_1", device_id=DEVICE_A, question_id=Q_RATIOS, question_version=2)
    device_b = _envelope(event_id="evt_2", device_id=DEVICE_B, device_sequence=1, question_id=Q_RATIOS, question_version=2)
    first = _process(service, [device_a], device_id=DEVICE_A)
    second = _process(service, [device_b], device_id=DEVICE_B)
    assert first.accepted_event_ids == ["evt_1"]
    assert second.accepted_event_ids == ["evt_2"]
    with transaction(service.connection) as connection:
        rows = connection.execute(
            "SELECT event_id, weight FROM answer_attempts WHERE session_id = %s AND content_id = %s ORDER BY event_id",
            (session, Q_RATIOS),
        ).fetchall()
    assert rows[0]["weight"] == pytest.approx(1.0)
    assert rows[1]["weight"] == pytest.approx(0.5)
    assert any(c.conflict_type == "PARALLEL_ATTEMPT_DETECTED" for c in second.conflicts)


# ----------------------------------------------------------------------
# Late events after session summary
# ----------------------------------------------------------------------

def test_late_event_after_completion_revises_summary(service: SyncService) -> None:
    _seed_student(service)
    service.register_device(STUDENT_ID, "d", device_id=DEVICE_A)
    answer = _envelope(event_id="evt_1", device_sequence=1)
    completed = _envelope(
        event_id="evt_2",
        device_sequence=2,
        event_type="SESSION_COMPLETED",
        payload={},
        question_id=None,
        question_version=None,
    )
    late = _envelope(event_id="evt_3", device_sequence=3, question_id=Q_RATIOS, question_version=2)
    _process(service, [answer])
    _process(service, [completed])
    response = _process(service, [late])
    assert response.accepted_event_ids == ["evt_3"]
    assert any(c.conflict_type == "SUMMARY_REVISED" for c in response.conflicts)
    with transaction(service.connection) as connection:
        state = connection.execute(
            "SELECT session_state FROM study_sessions WHERE session_id = %s",
            (SESSION_ID,),
        ).fetchone()
    assert state["session_state"] == "SESSION_COMPLETED"
    with transaction(service.connection) as connection:
        conflicts = connection.execute(
            "SELECT conflict_type FROM sync_conflicts WHERE event_id = 'evt_3'"
        ).fetchall()
    assert [c["conflict_type"] for c in conflicts] == ["SUMMARY_REVISED"]


# ----------------------------------------------------------------------
# Refresh/restart recovery and snapshots
# ----------------------------------------------------------------------

def test_refresh_restart_recovery(service: SyncService) -> None:
    _seed_student(service)
    service.register_device(STUDENT_ID, "d", device_id=DEVICE_A)
    _process(service, [_envelope(event_id="evt_1")])
    restarted_conn = pg.connect()
    restarted_conn.execute("SELECT set_config('app.tenant_id', 'tenant_test', false)")
    restarted_conn.commit()
    restarted = SyncService(restarted_conn)
    snapshot = restarted.build_snapshot(STUDENT_ID)
    restarted_conn.rollback()
    restarted_conn.close()
    assert snapshot.snapshot_version >= 1
    assert snapshot.server_cursor.startswith("cursor_")
    assert snapshot.student["id"] == STUDENT_ID
    linear = [s for s in snapshot.skill_states if s["skill"] == "linear_equations"]
    assert len(linear) == 1
    assert linear[0]["evidence_count"] == 1


def test_snapshot_includes_strategy_memory(service: SyncService) -> None:
    _seed_student(service)
    service.register_device(STUDENT_ID, "d", device_id=DEVICE_A)
    _process(service, [_envelope(event_id="evt_1")])
    snapshot = service.build_snapshot(STUDENT_ID)
    assert "intervention_stats" in snapshot.strategy_memory
    assert "facts" in snapshot.strategy_memory


def test_mastery_projection_never_trusts_client(service: SyncService) -> None:
    _seed_student(service)
    service.register_device(STUDENT_ID, "d", device_id=DEVICE_A)
    envelope = _envelope(
        event_id="evt_1",
        payload={"question_id": Q_LINEAR, "question_version": 1,
                 "selected_choice_id": "A", "hint_level": 0,
                 "attempt_id": "evt_1", "mastery_claimed": 0.99},
    )
    _process(service, [envelope])
    snapshot = service.build_snapshot(STUDENT_ID)
    linear = [s for s in snapshot.skill_states if s["skill"] == "linear_equations"][0]
    assert linear["mastery"] != 0.99


# ----------------------------------------------------------------------
# Payload bounds
# ----------------------------------------------------------------------

def test_batch_over_100_rejected(service: SyncService) -> None:
    _seed_student(service)
    service.register_device(STUDENT_ID, "d", device_id=DEVICE_A)
    events = [_envelope(event_id=f"evt_{i}") for i in range(101)]
    response = _process(service, events)
    assert response.rejected_events[0].code == "PAYLOAD_TOO_LARGE"


# ----------------------------------------------------------------------
# VersionedAnswerKey direct behavior
# ----------------------------------------------------------------------

def test_versioned_answer_key_lists_fixture_packs() -> None:
    key = VersionedAnswerKey()
    assert "0.1.0" in key.list_versions()


def test_versioned_answer_key_version_mismatch() -> None:
    key = VersionedAnswerKey()
    with pytest.raises(QuestionVersionError):
        key.pack(PACK_VERSION).answer_choice_id(Q_LINEAR, 5)


# ----------------------------------------------------------------------
# Projection rebuild from the immutable event log (API_AND_OPERATIONS §7)
# ----------------------------------------------------------------------

def _projection_snapshot(connection) -> dict[str, list[tuple]]:
    def rows(sql: str, params: tuple = ()) -> list[tuple]:
        with transaction(connection):
            return [tuple(r) for r in connection.execute(sql, params).fetchall()]

    return {
        "sessions": rows(
            "SELECT session_id, student_id, session_state FROM study_sessions "
            "WHERE student_id = %s ORDER BY session_id",
            (STUDENT_ID,),
        ),
        "attempts": rows(
            "SELECT attempt_id, event_id, session_id, content_id, version, sequence, "
            "selected_choice_id, correct, hint_level, weight, validity, occurred_at "
            "FROM answer_attempts WHERE student_id = %s ORDER BY event_id",
            (STUDENT_ID,),
        ),
        "skills": rows(
            "SELECT skill, alpha, beta, mastery, confidence, evidence_count, "
            "correct_streak, incorrect_streak, projection_origin "
            "FROM student_skill_states WHERE student_id = %s ORDER BY skill",
            (STUDENT_ID,),
        ),
        "evidence": rows(
            "SELECT event_id, session_id, skill, subskill, misconception, "
            "source_label, confidence_label, state, item_id, item_version "
            "FROM misconception_evidence WHERE student_id = %s ORDER BY event_id",
            (STUDENT_ID,),
        ),
    }


def test_rebuild_projection_from_events_restores_state(service: SyncService) -> None:
    from scripts.rebuild_learner_projections import rebuild_student

    _seed_student(service)
    service.register_device(STUDENT_ID, "d", device_id=DEVICE_A)
    events = [
        _envelope(
            event_id="evt_c_1", event_type="CONTENT_PRESENTED",
            payload={"question_id": Q_LINEAR}, device_sequence=1,
        ),
        _envelope(event_id="evt_a_1", device_sequence=2, depends_on=["evt_c_1"]),
        _envelope(
            event_id="evt_c_2", event_type="CONTENT_PRESENTED",
            payload={"question_id": Q_LINEAR}, device_sequence=3,
            depends_on=["evt_a_1"],
        ),
        _envelope(
            event_id="evt_a_2",
            device_sequence=4,
            payload={
                "question_id": Q_LINEAR,
                "question_version": 1,
                "selected_choice_id": "B",
                "hint_level": 0,
                "attempt_id": "evt_a_2",
            },
            depends_on=["evt_c_2"],
        ),
        _envelope(
            event_id="evt_s_1", event_type="SESSION_COMPLETED",
            payload={"summary": "s"}, device_sequence=5,
            depends_on=["evt_a_2"],
        ),
    ]
    response = _process(service, events)
    assert response.rejected_events == []
    assert len(response.accepted_event_ids) == 5

    original = _projection_snapshot(service.connection)
    assert original["attempts"]
    assert original["evidence"]

    with transaction(service.connection):
        service.connection.execute(
            "DELETE FROM answer_attempts WHERE student_id = %s", (STUDENT_ID,)
        )
        service.connection.execute(
            "DELETE FROM misconception_evidence WHERE student_id = %s", (STUDENT_ID,)
        )
        service.connection.execute(
            "DELETE FROM study_sessions WHERE student_id = %s", (STUDENT_ID,)
        )
        service.connection.execute(
            "UPDATE student_skill_states SET mastery = 0.99, confidence = 0.99 "
            "WHERE student_id = %s",
            (STUDENT_ID,),
        )

    report = rebuild_student(service.connection, STUDENT_ID, service)
    assert report["rejected"] == []
    assert report["events_replayed"] == 5
    assert report["skipped_server_events"] == 1  # STUDENT_CREATED is not a sync event

    assert _projection_snapshot(service.connection) == original
