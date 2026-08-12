"""PGMemory on PostgreSQL: episode recall, semantic facts, intervention stats.

Semantic fact formation and promotion rules (observation -> inference ->
stable), episode recall ordering, and intervention windows are preserved
from the original SQLiteMemory tests and run against the PG schema.
"""

from __future__ import annotations

import pytest

from app.domain.events import LearningEvent, LearningEventType
from app.infrastructure import pg
from app.infrastructure.learner_store import LearnerStore
from app.infrastructure.migration_runner import migrate_database
from app.memory.episode_builder import EpisodeBuilder
from app.memory.pg_memory import PGMemory
from tests.pg_test_helpers import cleanup_tenant, unique_tenant_id


@pytest.fixture()
def env() -> tuple[PGMemory, EpisodeBuilder, str]:
    admin = pg.connect_admin()
    migrate_database(admin)
    admin.close()
    tenant_id = unique_tenant_id("task3_pg_memory")
    conn = pg.connect()
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant_id,))
    conn.commit()
    learner = LearnerStore(conn)
    student_id, _ = learner.create_student("Ari", 20, 1200)
    yield PGMemory(conn), EpisodeBuilder(conn), student_id
    conn.close()
    cleanup = pg.connect_admin()
    try:
        cleanup_tenant(cleanup, tenant_id)
    finally:
        cleanup.close()


def make_event(session_id: str, event_id: str, student_id: str) -> LearningEvent:
    return LearningEvent(
        event_id=event_id,
        student_id=student_id,
        session_id=session_id,
        event_type=LearningEventType.ANSWER_EVALUATED,
        payload={},
        occurred_at="2026-08-06T10:00:00+00:00",
        received_at="2026-08-06T10:00:00+00:00",
    )


def _validated_episode(
    builder: EpisodeBuilder,
    *,
    student_id: str,
    session_id: str,
    misconception: str | None = "sign_error",
    intervention: str = "SHOW_WORKED_EXAMPLE",
    outcome_content_id: str,
    event_suffix: str,
) -> None:
    episode = builder.build_candidate(
        student_id=student_id,
        session_id=session_id,
        skill="linear_equations",
        misconception=misconception,
        intervention=intervention,
        context_event=make_event(session_id, f"ctx-{event_suffix}", student_id),
        evidence_events=[make_event(session_id, f"ev-{event_suffix}", student_id)],
        outcome_event=make_event(session_id, f"out-{event_suffix}", student_id),
        outcome_correct=True,
        outcome_hint_level=0,
        outcome_content_id=outcome_content_id,
        teaching_content_id="teach-1",
        summary=f"episode {event_suffix}",
    )
    builder.validate(episode)


def test_recall_episodes_can_scope_to_null_misconception(env) -> None:
    memory, builder, student_id = env
    _validated_episode(
        builder,
        student_id=student_id,
        session_id="ses-generic",
        misconception=None,
        outcome_content_id="t-generic",
        event_suffix="generic",
    )
    _validated_episode(
        builder,
        student_id=student_id,
        session_id="ses-specific",
        misconception="sign_error",
        outcome_content_id="t-specific",
        event_suffix="specific",
    )

    generic = memory.recall_episodes(
        student_id=student_id,
        skill="linear_equations",
        misconception=None,
    )
    all_episodes = memory.recall_episodes(
        student_id=student_id,
        skill="linear_equations",
    )

    assert [episode.misconception for episode in generic] == [None]
    assert {episode.misconception for episode in all_episodes} == {
        None,
        "sign_error",
    }


def test_recall_episodes_empty_before_data(env) -> None:
    memory, _, student_id = env
    assert memory.recall_episodes(student_id=student_id, skill="linear_equations") == []


def test_recall_episodes(env) -> None:
    memory, builder, student_id = env
    _validated_episode(builder, student_id=student_id, session_id="ses-1", outcome_content_id="t-1", event_suffix="1")
    _validated_episode(builder, student_id=student_id, session_id="ses-1", outcome_content_id="t-2", event_suffix="2")

    recalled = memory.recall_episodes(
        student_id=student_id, skill="linear_equations", misconception="sign_error"
    )
    assert len(recalled) == 2
    assert all(e.status == "validated" for e in recalled)
    assert recalled[0].created_at >= recalled[1].created_at

    other = memory.recall_episodes(
        student_id=student_id, skill="linear_equations", misconception="inverse_operation_error"
    )
    assert other == []


def test_recall_episodes_filters_by_intervention(env) -> None:
    memory, builder, student_id = env
    _validated_episode(builder, student_id=student_id, session_id="ses-1", outcome_content_id="t-1", event_suffix="1")
    _validated_episode(
        builder,
        student_id=student_id,
        session_id="ses-1",
        intervention="SHOW_MICRO_LESSON",
        outcome_content_id="t-2",
        event_suffix="2",
    )

    only_worked = memory.recall_episodes(
        student_id=student_id,
        skill="linear_equations",
        misconception="sign_error",
        intervention="SHOW_WORKED_EXAMPLE",
    )
    assert len(only_worked) == 1
    assert only_worked[0].intervention == "SHOW_WORKED_EXAMPLE"

    only_lesson = memory.recall_episodes(
        student_id=student_id,
        skill="linear_equations",
        misconception="sign_error",
        intervention="SHOW_MICRO_LESSON",
    )
    assert len(only_lesson) == 1
    assert only_lesson[0].intervention == "SHOW_MICRO_LESSON"


def test_fact_supporting_episodes_exclude_other_interventions(env) -> None:
    """Regression: fact support must be scoped to the intervention encoded in
    the normalized key, never episodes from a different intervention."""
    memory, builder, student_id = env
    for i in range(3):
        _validated_episode(
            builder,
            student_id=student_id,
            session_id="ses-a",
            intervention="SHOW_WORKED_EXAMPLE",
            outcome_content_id=f"t-we-{i}",
            event_suffix=f"we-{i}",
        )
        _validated_episode(
            builder,
            student_id=student_id,
            session_id="ses-a",
            intervention="SHOW_MICRO_LESSON",
            outcome_content_id=f"t-ml-{i}",
            event_suffix=f"ml-{i}",
        )

    worked = builder.list_validated_episodes(student_id=student_id)[0]
    assert worked.intervention == "SHOW_MICRO_LESSON"

    fact = memory.upsert_fact_for_episode(worked)
    assert fact.evidence_count == 3
    supporting = memory.list_episodes_for_fact(student_id, fact.normalized_key)
    assert len(supporting) == 3
    assert all(e.intervention == "SHOW_MICRO_LESSON" for e in supporting)


def test_semantic_fact_formation(env) -> None:
    memory, builder, student_id = env
    _validated_episode(builder, student_id=student_id, session_id="ses-1", outcome_content_id="t-1", event_suffix="1")
    episode = builder.list_validated_episodes(student_id=student_id)[0]

    fact = memory.upsert_fact_for_episode(episode)
    assert fact.status == "observation"
    assert fact.evidence_count == 1

    _validated_episode(builder, student_id=student_id, session_id="ses-1", outcome_content_id="t-2", event_suffix="2")
    episode2 = builder.list_validated_episodes(student_id=student_id)[0]
    fact = memory.upsert_fact_for_episode(episode2)
    assert fact.status == "inference"
    assert fact.evidence_count == 2

    facts = memory.get_facts(student_id)
    assert len(facts) == 1
    assert facts[0].normalized_key == "linear_equations\x1fsign_error\x1fSHOW_WORKED_EXAMPLE"

    fetched = memory.get_fact(facts[0].fact_id)
    assert fetched is not None
    assert fetched.fact_id == facts[0].fact_id


def test_fact_promotes_to_stable_across_sessions(env) -> None:
    memory, builder, student_id = env
    for i in range(3):
        session = f"ses-{i + 1}"
        _validated_episode(
            builder,
            student_id=student_id,
            session_id=session,
            outcome_content_id=f"t-{i + 1}",
            event_suffix=str(i + 1),
        )
        episode = builder.list_validated_episodes(student_id=student_id)[0]
        memory.upsert_fact_for_episode(episode)

    facts = memory.get_facts(student_id)
    assert facts[0].status == "stable"
    assert facts[0].evidence_count == 3
    assert facts[0].confidence >= 0.7


def test_intervention_stats_windows(env) -> None:
    memory, _, student_id = env
    stat = memory.record_intervention_outcome(
        student_id=student_id,
        skill="linear_equations",
        misconception="sign_error",
        intervention="SHOW_WORKED_EXAMPLE",
        difficulty_band="d2",
        window="immediate",
        component_score=1.0,
        weight=1.0,
    )
    assert stat.immediate_attempts == 1
    assert stat.effectiveness("immediate") == pytest.approx(1.0)
    assert stat.blended_effectiveness() == pytest.approx(1.0)

    stat = memory.record_intervention_outcome(
        student_id=student_id,
        skill="linear_equations",
        misconception="sign_error",
        intervention="SHOW_WORKED_EXAMPLE",
        difficulty_band="d2",
        window="short_term",
        component_score=0.5,
        weight=0.8,
    )
    assert stat.short_term_attempts == 1
    assert stat.effectiveness("short_term") == pytest.approx(0.5 / 0.8)

    same = memory.record_intervention_outcome(
        student_id=student_id,
        skill="linear_equations",
        misconception="sign_error",
        intervention="SHOW_WORKED_EXAMPLE",
        difficulty_band="d2",
        window="immediate",
        component_score=0.0,
        weight=1.0,
    )
    assert same.immediate_attempts == 2
    assert same.effectiveness("immediate") == pytest.approx(0.5)
    assert memory.get_intervention_stat(same.stat_id).stat_id == same.stat_id


def test_recall_excludes_non_validated_episodes(env) -> None:
    memory, builder, student_id = env
    episode = builder.build_candidate(
        student_id=student_id,
        session_id="ses-x",
        skill="linear_equations",
        misconception="sign_error",
        intervention="SHOW_WORKED_EXAMPLE",
        context_event=make_event("ses-x", "ctx-x", student_id),
        evidence_events=[make_event("ses-x", "ev-x", student_id)],
        outcome_event=make_event("ses-x", "out-x", student_id),
        outcome_correct=False,
        outcome_hint_level=2,
        outcome_content_id="t-1",
        teaching_content_id="t-1",
        summary="failed attempt",
    )
    validated = builder.validate(episode)
    assert validated.status == "insufficient_outcome"
    assert memory.recall_episodes(student_id=student_id, skill="linear_equations") == []


def test_episode_write_rejects_deletion_pending_before_insert(env) -> None:
    memory, builder, student_id = env
    connection = memory.connection
    connection.execute(
        "UPDATE students SET status = 'deletion_pending' WHERE id = %s",
        (student_id,),
    )
    connection.commit()

    with pytest.raises(ValueError, match="active"):
        _validated_episode(
            builder,
            student_id=student_id,
            session_id="blocked-session",
            outcome_content_id="blocked-content",
            event_suffix="blocked",
        )

    assert connection.execute(
        "SELECT COUNT(*) AS total FROM learning_episodes WHERE student_id = %s",
        (student_id,),
    ).fetchone()["total"] == 0


def test_fact_write_rejects_deletion_pending_before_fact_or_outbox(env) -> None:
    memory, builder, student_id = env
    _validated_episode(
        builder,
        student_id=student_id,
        session_id="fact-session",
        outcome_content_id="fact-content",
        event_suffix="fact",
    )
    episode = builder.list_validated_episodes(student_id=student_id)[0]
    connection = memory.connection
    before_facts = connection.execute(
        "SELECT COUNT(*) AS total FROM student_memory_facts WHERE student_id = %s",
        (student_id,),
    ).fetchone()["total"]
    before_outbox = connection.execute(
        "SELECT COUNT(*) AS total FROM memory_outbox WHERE student_id = %s",
        (student_id,),
    ).fetchone()["total"]
    connection.execute(
        "UPDATE students SET status = 'deletion_pending' WHERE id = %s",
        (student_id,),
    )
    connection.commit()

    with pytest.raises(ValueError, match="active"):
        memory.upsert_fact_for_episode(episode)

    assert connection.execute(
        "SELECT COUNT(*) AS total FROM student_memory_facts WHERE student_id = %s",
        (student_id,),
    ).fetchone()["total"] == before_facts
    assert connection.execute(
        "SELECT COUNT(*) AS total FROM memory_outbox WHERE student_id = %s",
        (student_id,),
    ).fetchone()["total"] == before_outbox


def test_intervention_write_rejects_deletion_pending(env) -> None:
    memory, _, student_id = env
    connection = memory.connection
    connection.execute(
        "UPDATE students SET status = 'deletion_pending' WHERE id = %s",
        (student_id,),
    )
    connection.commit()

    with pytest.raises(ValueError, match="active"):
        memory.record_intervention_outcome(
            student_id=student_id,
            skill="linear_equations",
            misconception="sign_error",
            intervention="SHOW_WORKED_EXAMPLE",
            difficulty_band="d2",
            window="immediate",
            component_score=1.0,
            weight=1.0,
        )

    assert connection.execute(
        "SELECT COUNT(*) AS total FROM intervention_stats WHERE student_id = %s",
        (student_id,),
    ).fetchone()["total"] == 0
