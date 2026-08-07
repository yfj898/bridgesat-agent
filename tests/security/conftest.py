"""Shared fixtures for the security acceptance suite.

Maps one-to-one onto THREAT_MODEL.md section 10 acceptance tests:
1. student A cannot read or delete student B data
2. injected document instructions do not alter Agent behavior
3. free text cannot create a stable memory alone
4. repeated forged sync events do not duplicate mastery changes
5. crawler blocks localhost and private-network redirects
6. HTML content cannot execute script in the PWA
7. secrets are absent from repository and built client assets
8. LLM timeout triggers deterministic fallback
9. deletion removes learner data from SQLite and Mnemis retrieval
10. oversized requests and excessive retrieval loops are rejected
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.infrastructure.migration_runner import apply_migrations
from app.infrastructure.learner_store import LearnerStore

PACK_VERSION = "0.1.0"
Q_LINEAR = "sync.linear.001"


def _integrity(event_type: str, payload: dict) -> str:
    digest = hashlib.sha256()
    digest.update(event_type.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return f"sha256:{digest.hexdigest()}"


def envelope(
    *,
    event_id: str,
    student_id: str,
    device_id: str = "device_a",
    device_sequence: int = 1,
    event_type: str = "ANSWER_SUBMITTED",
    payload: dict | None = None,
    session_id: str = "session_01",
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
    event = {
        "event_id": event_id,
        "student_id": student_id,
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
        event["integrity_hash"] = _integrity(event_type, payload)
    return event


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "security.db"
    apply_migrations(path)
    return path


@pytest.fixture()
def two_students(db: Path) -> tuple[tuple[str, str], tuple[str, str]]:
    learner = LearnerStore(db)
    a = learner.create_student("Student A", 20, 1200)
    b = learner.create_student("Student B", 25, 1300)
    return (a[0], a[0]), (b[0], b[0])


def seed_student(db: Path, student_id: str, name: str = "Student") -> None:
    """Insert a student row with a deterministic ID (LearnerStore generates
    random IDs, so fixed-ID tests seed the projection directly). Idempotent:
    an existing student row is left untouched."""
    from app.domain.events import compute_integrity_hash, utc_now_iso
    from app.infrastructure.database import connect, transaction

    now = utc_now_iso()
    payload = {"name": name, "daily_minutes": 20, "target_score": 1200}
    with connect(db) as connection:
        with transaction(connection):
            existing = connection.execute(
                "SELECT 1 FROM students WHERE id = ?", (student_id,)
            ).fetchone()
            if existing:
                return
            connection.execute(
                """
                INSERT INTO students (
                    id, name, daily_minutes, target_score, mastery_json,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, '{}', 'active', ?, ?)
                """,
                (student_id, name, 20, 1200, now, now),
            )
            connection.execute(
                """
                INSERT INTO learning_events (
                    event_id, student_id, session_id, event_type, payload_json,
                    policy_version, content_version, occurred_at, received_at,
                    device_id, device_sequence, origin, integrity_hash
                ) VALUES (?, ?, '', 'STUDENT_CREATED', ?, 'policy-0.1.0', NULL,
                          ?, ?, NULL, NULL, 'online', ?)
                """,
                (
                    f"evt_seed_{student_id}",
                    student_id,
                    "{}",
                    now,
                    now,
                    compute_integrity_hash("STUDENT_CREATED", payload),
                ),
            )
