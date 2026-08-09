"""Outbox wiring: validated episodes and evidenced facts must enqueue
delivery rows inside the same transaction that writes them
(MEMORY_CONSISTENCY §4, §6)."""

from __future__ import annotations

import pytest

from app.domain.events import LearningEvent, LearningEventType
from app.infrastructure import pg
from app.infrastructure.learner_store import LearnerStore
from app.infrastructure.migration_runner import migrate_database
from app.memory.episode_builder import EpisodeBuilder
from app.memory.outbox import OutboxRepository
from app.memory.pg_memory import PGMemory


@pytest.fixture()
def env() -> tuple[object, str]:
    admin = pg.connect_admin()
    migrate_database(admin)
    admin.close()
    conn = pg.connect()
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", ("tenant_test",))
    conn.commit()
    learner = LearnerStore(conn)
    student_id, _ = learner.create_student("Ari", 20, 1200)
    yield conn, student_id
    conn.close()
    cleanup = pg.connect_admin()
    try:
        cleanup.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        cleanup.commit()
    finally:
        cleanup.close()


def _event(session_id: str, event_id: str, student_id: str) -> LearningEvent:
    return LearningEvent(
        event_id=event_id,
        student_id=student_id,
        session_id=session_id,
        event_type=LearningEventType.ANSWER_EVALUATED,
        payload={},
        occurred_at="2026-08-06T10:00:00+00:00",
        received_at="2026-08-06T10:00:00+00:00",
    )


def _episode(
    builder: EpisodeBuilder,
    *,
    student_id: str,
    session_id: str,
    episode_id: str,
    valid: bool = True,
) -> None:
    episode = builder.build_candidate(
        student_id=student_id,
        session_id=session_id,
        skill="linear_equations",
        misconception="sign_error",
        intervention="SHOW_WORKED_EXAMPLE",
        context_event=_event(session_id, "ctx", student_id),
        evidence_events=[_event(session_id, "obs", student_id)],
        outcome_event=_event(session_id, "out", student_id),
        outcome_correct=True,
        outcome_hint_level=0,
        outcome_content_id="out" if valid else "same",
        teaching_content_id="same",
        summary="x",
        episode_id=episode_id,
    )
    builder.validate(episode)


def test_validated_episode_enqueues_upsert_in_same_write(
    env: tuple[object, str]
) -> None:
    conn, student_id = env
    builder = EpisodeBuilder(conn)
    repo = OutboxRepository(conn)

    _episode(builder, student_id=student_id, session_id="s1", episode_id="ep_ok")

    rows = repo.list_by_status("pending")
    assert len(rows) == 1
    assert rows[0].aggregate_type == "episode"
    assert rows[0].aggregate_id == "ep_ok"
    assert rows[0].operation == "upsert_episode"
    assert rows[0].student_id == student_id
    assert rows[0].payload["episode_id"] == "ep_ok"
    assert rows[0].payload["status"] == "validated"


def test_insufficient_outcome_episode_enqueues_nothing(
    env: tuple[object, str]
) -> None:
    conn, student_id = env
    builder = EpisodeBuilder(conn)
    repo = OutboxRepository(conn)

    _episode(builder, student_id=student_id, session_id="s1", episode_id="ep_bad", valid=False)

    assert repo.list_by_status("pending") == []
    assert repo.list_by_status("indexed") == []


def test_fact_upsert_enqueues_upsert_fact(env: tuple[object, str]) -> None:
    conn, student_id = env
    builder = EpisodeBuilder(conn)
    memory = PGMemory(conn)
    repo = OutboxRepository(conn)

    _episode(builder, student_id=student_id, session_id="s1", episode_id="ep_f1")
    episode = builder.get_episode("ep_f1")
    assert episode is not None
    fact = memory.upsert_fact_for_episode(episode)

    pending = repo.list_by_status("pending")
    assert len(pending) == 2
    fact_rows = [r for r in pending if r.aggregate_type == "fact"]
    assert len(fact_rows) == 1
    assert fact_rows[0].aggregate_id == fact.fact_id
    assert fact_rows[0].operation == "upsert_fact"
    assert fact_rows[0].payload["fact_id"] == fact.fact_id


def test_fact_version_bump_creates_new_delivery(
    env: tuple[object, str]
) -> None:
    conn, student_id = env
    builder = EpisodeBuilder(conn)
    memory = PGMemory(conn)
    repo = OutboxRepository(conn)

    for session, ep in (("s1", "ep_a"), ("s2", "ep_b")):
        _episode(builder, student_id=student_id, session_id=session, episode_id=ep)
        episode = builder.get_episode(ep)
        assert episode is not None
        memory.upsert_fact_for_episode(episode)

    fact_rows = [r for r in repo.list_by_status("pending") if r.aggregate_type == "fact"]
    assert len(fact_rows) == 2
    assert fact_rows[0].payload["version"] == 1
    assert fact_rows[1].payload["version"] == 2


def test_rollback_of_episode_write_removes_outbox_row(
    env: tuple[object, str]
) -> None:
    conn, student_id = env
    repo = OutboxRepository(conn)
    before = len(repo.list_by_status("pending"))
    try:
        repo.enqueue(
            conn,
            student_id=student_id,
            aggregate_type="episode",
            aggregate_id="ep_rollback",
            operation="upsert_episode",
            payload={"episode_id": "ep_rollback"},
            version=1,
        )
        conn.rollback()
    finally:
        conn.execute(
            "SELECT set_config('app.tenant_id', %s, false)", ("tenant_test",)
        )
    assert len(repo.list_by_status("pending")) == before