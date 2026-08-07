"""Script-level smoke tests: rebuild, parity, and dead-letter replay
(MEMORY_CONSISTENCY §12, §13, §14)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from app.domain.events import LearningEvent, LearningEventType
from app.infrastructure import migration_runner
from app.infrastructure.database import connect
from app.infrastructure.learner_store import LearnerStore
from app.memory.episode_builder import EpisodeBuilder
from app.memory.mnemis_stub import InMemoryMnemisIndex
from app.memory.outbox import OutboxRepository
from app.memory.sqlite_backend import SQLiteMemory
from app.memory.worker import OutboxWorker
from scripts.rebuild_memory_index import rebuild_student
from scripts.replay_dead_letter import replay
from scripts.verify_memory_parity import _sqlite_counts

from tests.test_memory_outbox_wiring import _event


class FailingIndex(InMemoryMnemisIndex):
    async def upsert_episode(self, episode):  # noqa: ANN001
        raise RuntimeError("mnemis down")


def _seed(env: tuple[Path, str]) -> tuple[EpisodeBuilder, SQLiteMemory]:
    db, student_id = env
    builder = EpisodeBuilder(db)
    memory = SQLiteMemory(db)
    for session, ep in (("s1", "ep_1"), ("s2", "ep_2")):
        episode = builder.build_candidate(
            student_id=student_id,
            session_id=session,
            skill="linear_equations",
            misconception="sign_error",
            intervention="SHOW_WORKED_EXAMPLE",
            context_event=_event(session, "ctx", student_id),
            evidence_events=[_event(session, "obs", student_id)],
            outcome_event=_event(session, "out", student_id),
            outcome_correct=True,
            outcome_hint_level=0,
            outcome_content_id=f"out_{ep}",
            teaching_content_id="same",
            summary="x",
            episode_id=ep,
        )
        builder.validate(episode)
        episode = builder.get_episode(ep)
        assert episode is not None
        memory.upsert_fact_for_episode(episode)
    return builder, memory


def _count_indexed(db: Path, index: InMemoryMnemisIndex, student_id: str) -> tuple[int, int]:
    return asyncio.run(index.count_episodes(student_id)), asyncio.run(index.count_facts(student_id))


@pytest.fixture()
def env(tmp_path: Path) -> tuple[Path, str]:
    db = tmp_path / "scripts.db"
    migration_runner.apply_migrations(db)
    learner = LearnerStore(db)
    student_id, _ = learner.create_student("Ari", 20, 1200)
    return db, student_id


def test_parity_after_rebuild(env: tuple[Path, str]) -> None:
    db, student_id = env
    _seed(env)
    with connect(db) as connection:
        sqlite = _sqlite_counts(connection, student_id)
    assert sqlite == {"episodes": 2, "facts": 1}

    index = InMemoryMnemisIndex()
    report = rebuild_student(db, student_id, index)
    assert report["episodes_enqueued"] == 2
    assert report["facts_enqueued"] == 1
    assert _count_indexed(db, index, student_id) == (2, 1)

    with connect(db) as connection:
        sqlite_after = _sqlite_counts(connection, student_id)
    assert sqlite_after == {"episodes": 2, "facts": 1}


def test_rebuild_is_idempotent(env: tuple[Path, str]) -> None:
    db, student_id = env
    _seed(env)
    first = InMemoryMnemisIndex()
    rebuild_student(db, student_id, first)
    assert _count_indexed(db, first, student_id) == (2, 1)

    second = InMemoryMnemisIndex()
    rebuild_student(db, student_id, second)
    assert _count_indexed(db, second, student_id) == (2, 1)


def test_dead_letter_replay(env: tuple[Path, str]) -> None:
    db, student_id = env
    _seed(env)

    worker = OutboxWorker(db, index=FailingIndex())
    for _ in range(6):
        with connect(db) as connection:
            connection.execute(
                "UPDATE memory_outbox SET next_attempt_at = '2020-01-01T00:00:00+00:00'"
            )
        worker.run_pending()
    assert len(OutboxRepository(db).list_by_status("dead_letter")) == 2
    assert OutboxRepository(db).list_by_status("pending") == []

    report = replay(db, InMemoryMnemisIndex(), 3)
    assert report["reset_rows"] == 2
    assert report["processed"] == 2
    assert len(OutboxRepository(db).list_by_status("indexed")) == 4


def test_replay_is_noop_without_dead_letters(env: tuple[Path, str]) -> None:
    db, student_id = env
    _seed(env)
    OutboxWorker(db, index=InMemoryMnemisIndex()).run_pending()
    report = replay(db, InMemoryMnemisIndex(), 1)
    assert report == {"reset_rows": 0, "processed": 0}
