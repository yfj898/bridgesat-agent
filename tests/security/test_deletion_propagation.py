"""Acceptance test 9: deletion removes learner data from SQLite and Mnemis
retrieval.

THREAT_MODEL.md section 5.12 and MEMORY_CONSISTENCY section 11: deletion
first stops new writes, then removes/tombstones SQLite rows and enqueues a
deletion outbox event in one transaction, deletes the Mnemis index data,
and reports completion only after verification that nothing is retrievable.
"""

from __future__ import annotations

import asyncio

import psycopg

from app.domain.memory import Episode
from app.infrastructure.learner_store import LearnerStore
from app.memory.deletion import StudentMemoryDeletionService
from app.memory.episode_builder import EpisodeBuilder
from app.memory.mnemis_stub import InMemoryMnemisIndex
from app.memory.pg_memory import PGMemory
from app.sync.service import SyncService

from tests.security.test_cross_student_isolation import _episode_event

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


def _counts(connection: psycopg.Connection, student_id: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in PERSONAL_TABLES:
        count = connection.execute(
            f"""
            SELECT COUNT(*) AS c
            FROM {table}
            WHERE student_id = %s
              AND tenant_id = current_setting('app.tenant_id', true)
            """,
            (student_id,),
        ).fetchone()["c"]
        counts[table] = count
    return counts


def _populate(
    connection: psycopg.Connection,
    student_id: str,
    session_id: str | None = None,
) -> Episode:
    from tests.security.conftest import seed_student

    seed_student(connection, student_id)
    session_id = session_id or f"ses-del-{student_id}"
    learner = LearnerStore(connection)
    learner.create_session(student_id, session_id)
    builder = EpisodeBuilder(connection)
    episode = builder.build_candidate(
        student_id=student_id,
        session_id=session_id,
        skill="linear_equations",
        misconception="sign_error",
        intervention="SHOW_WORKED_EXAMPLE",
        context_event=_episode_event(session_id, "ctx_1", student_id),
        evidence_events=[_episode_event(session_id, "obs_1", student_id)],
        outcome_event=_episode_event(session_id, "out_1", student_id),
        outcome_correct=True,
        outcome_hint_level=0,
        outcome_content_id="transfer_1",
        teaching_content_id="taught_1",
        summary="x",
    )
    builder.validate(episode)
    sync = SyncService(connection)
    sync.register_device(student_id, "device", device_id=f"dev_del_{student_id}")
    return episode


def test_deletion_propagates_to_sqlite_and_index(
    db: psycopg.Connection, two_students
) -> None:
    (a, _), (b, _) = two_students
    episode = _populate(db, a)
    fact = PGMemory(db).upsert_fact_for_episode(episode)
    index = InMemoryMnemisIndex()
    asyncio.run(
        index.upsert_episode(
            episode.model_dump(), idempotency_key=f"seed:{episode.episode_id}"
        )
    )
    asyncio.run(
        index.upsert_fact(
            fact.model_dump(), idempotency_key=f"seed:{fact.fact_id}"
        )
    )
    assert asyncio.run(index.count_episodes(a)) == 1
    assert asyncio.run(index.count_facts(a)) == 1
    service = StudentMemoryDeletionService(db, index=index)

    service.request_deletion(a)
    assert service.deletion_status(a) == "requested"

    service.execute_sqlite_deletion(a)
    counts = _counts(db, a)
    assert all(count == 0 for count in counts.values())
    assert service.deletion_status(a) == "sqlite_deleted"

    completed = asyncio.run(service.complete_index_deletion(a))
    assert completed is True
    assert service.deletion_status(a) == "verified"
    assert asyncio.run(service.verify_not_retrievable(a)) is True
    assert asyncio.run(index.count_episodes(a)) == 0
    assert asyncio.run(index.count_facts(a)) == 0
    assert asyncio.run(index.count_episodes(b)) == 0
    assert asyncio.run(index.count_facts(b)) == 0


def test_deletion_never_touches_other_students(
    db: psycopg.Connection, two_students
) -> None:
    (a, _), (b, _) = two_students
    _populate(db, a)
    _populate(db, b)
    service = StudentMemoryDeletionService(db)
    service.request_deletion(a)
    service.execute_sqlite_deletion(a)
    counts_b = _counts(db, b)
    assert counts_b["learning_events"] > 0
    assert counts_b["study_sessions"] > 0
    assert counts_b["devices"] > 0
    assert service.deletion_status(b) is None


def test_completion_requires_verification_not_just_removal(
    db: psycopg.Connection, two_students
) -> None:
    (a, _), _ = two_students
    _populate(db, a)
    index = InMemoryMnemisIndex()

    async def _sticky_index():
        # Simulate an index that still holds data after deletion: completion
        # must not be reported.
        class StickyIndex(InMemoryMnemisIndex):
            async def recall_similar(self, query, **kwargs):
                return [{"memory_id": "mem:stale", "supporting_episode_ids": ["ep_stale"]}]

            async def count_episodes(self, student_id):
                return 1

        return StickyIndex()

    service = StudentMemoryDeletionService(db, index=asyncio.run(_sticky_index()))
    service.request_deletion(a)
    service.execute_sqlite_deletion(a)
    completed = asyncio.run(service.complete_index_deletion(a))
    assert completed is False
    assert service.deletion_status(a) in ("index_deletion_pending", "failed")
