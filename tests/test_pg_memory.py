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


@pytest.fixture()
def env() -> tuple[PGMemory, EpisodeBuilder, str]:
    admin = pg.connect_admin()
    migrate_database(admin)
    admin.close()
    conn = pg.connect()
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", ("tenant_test",))
    conn.commit()
    learner = LearnerStore(conn)
    student_id, _ = learner.create_student("Ari", 20, 1200)
    yield PGMemory(conn), EpisodeBuilder(conn), student_id
    conn.close()
    cleanup = pg.connect_admin()
    try:
        cleanup.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        cleanup.commit()
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
    misconception: str = "sign_error",
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