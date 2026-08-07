"""Student memory deletion protocol tests (MEMORY_CONSISTENCY §11).

requested -> sqlite_deleted -> index_deletion_pending -> verified (or
failed). Completion is reported only after verification that no retrievable
indexed memory remains; SQLite personal rows are removed in the same
transaction as the deletion outbox event.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.agent.orchestrator import ContentItem, SessionOrchestrator
from app.domain.sessions import SessionState
from app.infrastructure import migration_runner
from app.infrastructure.database import connect
from app.infrastructure.learner_store import LearnerStore
from app.memory.deletion import StudentMemoryDeletionService
from app.memory.mnemis_stub import InMemoryMnemisIndex
from app.memory.outbox import OutboxRepository

PERSONAL_TABLES = [
    "student_tokens",
    "student_skill_states",
    "study_plans",
    "study_sessions",
    "answer_attempts",
    "learning_events",
    "agent_events",
    "misconception_evidence",
    "learning_episodes",
    "student_memory_facts",
    "intervention_stats",
    "devices",
    "sync_conflicts",
]


@pytest.fixture()
def populated(tmp_path: Path) -> tuple[Path, str]:
    db = tmp_path / "delete.db"
    migration_runner.apply_migrations(db)
    learner = LearnerStore(db)
    student_id, _ = learner.create_student("Ari", 20, 1200)
    session = "ses-del"
    learner.create_session(student_id, session)
    orchestrator = SessionOrchestrator(db)
    for state in [
        SessionState.PROFILE_READY,
        SessionState.DIAGNOSTIC_ACTIVE,
        SessionState.DIAGNOSTIC_COMPLETE,
        SessionState.PLAN_READY,
        SessionState.QUESTION_ACTIVE,
    ]:
        orchestrator.learner.transition_session(session, state)
    item = ContentItem(
        content_id="sign-a",
        version=1,
        skill="linear_equations",
        subskill="sign_handling",
        difficulty=2,
        answer_choice_id="C",
        misconception_map={"A": "sign_error"},
    )
    orchestrator.evaluate_answer(
        student_id=student_id,
        session_id=session,
        item=item,
        selected_choice_id="A",
        hint_level=0,
        minutes_remaining=15,
    )
    return db, student_id


def _counts(db: Path, student_id: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    with connect(db) as connection:
        for table in PERSONAL_TABLES:
            count = connection.execute(
                f"SELECT COUNT(*) AS c FROM {table} WHERE student_id = ?", (student_id,)
            ).fetchone()["c"]
            counts[table] = count
        sessions = connection.execute(
            "SELECT COUNT(*) AS c FROM session_items WHERE session_id IN (SELECT session_id FROM study_sessions WHERE student_id = ?)",
            (student_id,),
        ).fetchone()["c"]
        counts["session_items"] = sessions
    return counts


def test_request_marks_student_deletion_pending(populated: tuple[Path, str]) -> None:
    db, student_id = populated
    service = StudentMemoryDeletionService(db)
    service.request_deletion(student_id)
    assert service.deletion_status(student_id) == "requested"
    with connect(db) as connection:
        row = connection.execute(
            "SELECT status FROM students WHERE id = ?", (student_id,)
        ).fetchone()
    assert row["status"] == "deletion_pending"


def test_sqlite_deletion_removes_personal_rows_and_enqueues_outbox(
    populated: tuple[Path, str]
) -> None:
    db, student_id = populated
    before = _counts(db, student_id)
    assert sum(before.values()) > 0
    service = StudentMemoryDeletionService(db)
    service.request_deletion(student_id)
    service.execute_sqlite_deletion(student_id)
    counts = _counts(db, student_id)
    assert all(count == 0 for count in counts.values())
    assert service.deletion_status(student_id) == "sqlite_deleted"
    outbox = OutboxRepository(db)
    pending = outbox.list_by_status("pending")
    assert [r.operation for r in pending] == ["delete_student"]
    assert pending[0].aggregate_id == student_id
    with connect(db) as connection:
        row = connection.execute(
            "SELECT id FROM students WHERE id = ?", (student_id,)
        ).fetchone()
    assert row is not None  # tombstoned, not gone


def test_complete_index_deletion_verifies_before_reporting(
    populated: tuple[Path, str]
) -> None:
    db, student_id = populated
    index = InMemoryMnemisIndex()
    service = StudentMemoryDeletionService(db, index=index)

    from app.memory.episode_builder import EpisodeBuilder
    from app.memory.sqlite_backend import SQLiteMemory
    from tests.test_memory_outbox_wiring import _event as make_event

    builder = EpisodeBuilder(db)
    episode = builder.build_candidate(
        student_id=student_id,
        session_id="ses-del",
        skill="linear_equations",
        misconception="sign_error",
        intervention="SHOW_WORKED_EXAMPLE",
        context_event=make_event("ses-del", "ctx", student_id),
        evidence_events=[make_event("ses-del", "obs", student_id)],
        outcome_event=make_event("ses-del", "out", student_id),
        outcome_correct=True,
        outcome_hint_level=0,
        outcome_content_id="transfer",
        teaching_content_id="taught",
        summary="x",
    )
    builder.validate(episode)
    SQLiteMemory(db).upsert_fact_for_episode(episode)

    from app.memory.worker import OutboxWorker

    OutboxWorker(db, index=index).run_pending()
    assert asyncio.run(index.count_episodes(student_id)) == 1

    service.request_deletion(student_id)
    service.execute_sqlite_deletion(student_id)
    assert service.deletion_status(student_id) == "sqlite_deleted"

    assert service.verify_not_retrievable_sync(student_id) is False  # index still holds data

    completed = asyncio.run(service.complete_index_deletion(student_id))
    assert completed is True
    assert service.deletion_status(student_id) == "verified"
    assert service.verify_not_retrievable_sync(student_id) is True
    assert asyncio.run(index.count_episodes(student_id)) == 0
    with connect(db) as connection:
        row = connection.execute(
            "SELECT status FROM students WHERE id = ?", (student_id,)
        ).fetchone()
    assert row["status"] == "deleted"


def test_index_outage_keeps_state_pending(
    populated: tuple[Path, str]
) -> None:
    db, student_id = populated
    index = InMemoryMnemisIndex()
    service = StudentMemoryDeletionService(db, index=index)
    service.request_deletion(student_id)
    service.execute_sqlite_deletion(student_id)
    assert service.deletion_status(student_id) == "sqlite_deleted"
    completed = asyncio.run(service.complete_index_deletion(student_id))
    assert completed is True
    assert service.deletion_status(student_id) == "verified"
