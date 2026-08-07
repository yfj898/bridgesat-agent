from pathlib import Path

import pytest

from app.domain.events import LearningEvent, LearningEventType
from app.infrastructure import migration_runner
from app.infrastructure.learner_store import LearnerStore
from app.memory.episode_builder import EpisodeBuilder
from app.memory.sqlite_backend import SQLiteMemory


@pytest.fixture()
def env(tmp_path: Path) -> tuple[SQLiteMemory, EpisodeBuilder, str]:
    db = tmp_path / "mem.db"
    migration_runner.apply_migrations(db)
    learner = LearnerStore(db)
    student_id, _ = learner.create_student("Ari", 20, 1200)
    return SQLiteMemory(db), EpisodeBuilder(db), student_id


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


def test_recall_episodes(env: tuple[SQLiteMemory, EpisodeBuilder, str]) -> None:
    sqlite, builder, student_id = env
    _validated_episode(builder, student_id=student_id, session_id="ses-1", outcome_content_id="t-1", event_suffix="1")
    _validated_episode(builder, student_id=student_id, session_id="ses-1", outcome_content_id="t-2", event_suffix="2")

    recalled = sqlite.recall_episodes(
        student_id=student_id, skill="linear_equations", misconception="sign_error"
    )
    assert len(recalled) == 2
    assert all(e.status == "validated" for e in recalled)

    other = sqlite.recall_episodes(
        student_id=student_id, skill="linear_equations", misconception="inverse_operation_error"
    )
    assert other == []


def test_semantic_fact_formation(env: tuple[SQLiteMemory, EpisodeBuilder, str]) -> None:
    sqlite, builder, student_id = env
    _validated_episode(builder, student_id=student_id, session_id="ses-1", outcome_content_id="t-1", event_suffix="1")
    episode = builder.list_validated_episodes(student_id=student_id)[0]

    fact = sqlite.upsert_fact_for_episode(episode)
    assert fact.status == "observation"
    assert fact.evidence_count == 1

    _validated_episode(builder, student_id=student_id, session_id="ses-1", outcome_content_id="t-2", event_suffix="2")
    episode2 = builder.list_validated_episodes(student_id=student_id)[0]
    fact = sqlite.upsert_fact_for_episode(episode2)
    assert fact.status == "inference"
    assert fact.evidence_count == 2

    facts = sqlite.get_facts(student_id)
    assert len(facts) == 1
    assert facts[0].normalized_key == "linear_equations\x00sign_error\x00SHOW_WORKED_EXAMPLE"


def test_fact_promotes_to_stable_across_sessions(
    env: tuple[SQLiteMemory, EpisodeBuilder, str],
) -> None:
    sqlite, builder, student_id = env
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
        sqlite.upsert_fact_for_episode(episode)

    facts = sqlite.get_facts(student_id)
    assert facts[0].status == "stable"
    assert facts[0].evidence_count == 3
    assert facts[0].confidence >= 0.7


def test_intervention_stats_windows(env: tuple[SQLiteMemory, EpisodeBuilder, str]) -> None:
    sqlite, _, student_id = env
    stat = sqlite.record_intervention_outcome(
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

    stat = sqlite.record_intervention_outcome(
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

    same = sqlite.record_intervention_outcome(
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
