from pathlib import Path

import pytest

from app.domain.events import LearningEvent, LearningEventType
from app.domain.memory import outcome_component_score
from app.infrastructure import pg
from app.infrastructure.learner_store import LearnerStore
from app.infrastructure.migration_runner import migrate_database
from app.memory.episode_builder import EpisodeBuilder, effectiveness_successful


@pytest.fixture()
def env() -> tuple[EpisodeBuilder, str]:
    admin = pg.connect_admin()
    migrate_database(admin)
    admin.close()
    conn = pg.connect()
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", ("tenant_test",))
    conn.commit()
    learner = LearnerStore(conn)
    student_id, _ = learner.create_student("Ari", 20, 1200)
    yield EpisodeBuilder(conn), student_id
    conn.close()
    cleanup = pg.connect_admin()
    try:
        cleanup.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        cleanup.commit()
    finally:
        cleanup.close()


def make_event(
    session_id: str,
    event_type: LearningEventType,
    event_id: str,
    student_id: str,
) -> LearningEvent:
    return LearningEvent(
        event_id=event_id,
        student_id=student_id,
        session_id=session_id,
        event_type=event_type,
        payload={},
        occurred_at="2026-08-06T10:00:00+00:00",
        received_at="2026-08-06T10:00:00+00:00",
    )


def test_outcome_component_scores() -> None:
    assert outcome_component_score(True, 0) == 1.0
    assert outcome_component_score(True, 1) == 0.8
    assert outcome_component_score(True, 2) == 0.5
    assert outcome_component_score(True, 3) == 0.2
    assert outcome_component_score(False, 0) == 0.0


def test_build_and_validate_successful_episode(env: tuple[EpisodeBuilder, str]) -> None:
    builder, student_id = env
    episode = builder.build_candidate(
        student_id=student_id,
        session_id="ses-1",
        skill="linear_equations",
        misconception="sign_error",
        intervention="SHOW_WORKED_EXAMPLE",
        context_event=make_event("ses-1", LearningEventType.CONTENT_PRESENTED, "evt-c", student_id),
        evidence_events=[
            make_event("ses-1", LearningEventType.MISCONCEPTION_IDENTIFIED, "evt-m1", student_id),
            make_event("ses-1", LearningEventType.ANSWER_EVALUATED, "evt-a1", student_id),
        ],
        outcome_event=make_event("ses-1", LearningEventType.ANSWER_EVALUATED, "evt-o", student_id),
        outcome_correct=True,
        outcome_hint_level=0,
        outcome_content_id="transfer-1",
        teaching_content_id="teach-1",
        summary="Worked example resolved sign_error on a distinct transfer item.",
    )
    assert episode.status == "candidate"
    assert effectiveness_successful(episode)

    validated = builder.validate(episode)
    assert validated.status == "validated"
    assert builder.get_episode(episode.episode_id).status == "validated"


def test_episode_on_same_item_is_not_validated(env: tuple[EpisodeBuilder, str]) -> None:
    builder, student_id = env
    episode = builder.build_candidate(
        student_id=student_id,
        session_id="ses-1",
        skill="linear_equations",
        misconception="sign_error",
        intervention="SHOW_WORKED_EXAMPLE",
        context_event=make_event("ses-1", LearningEventType.CONTENT_PRESENTED, "evt-c", student_id),
        evidence_events=[make_event("ses-1", LearningEventType.ANSWER_EVALUATED, "evt-a1", student_id)],
        outcome_event=make_event("ses-1", LearningEventType.ANSWER_EVALUATED, "evt-o", student_id),
        outcome_correct=True,
        outcome_hint_level=0,
        outcome_content_id="teach-1",
        teaching_content_id="teach-1",
        summary="Same item outcome, not a transfer.",
    )
    validated = builder.validate(episode)
    assert validated.status == "insufficient_outcome"


def test_failed_outcome_episode_is_not_validated(env: tuple[EpisodeBuilder, str]) -> None:
    builder, student_id = env
    episode = builder.build_candidate(
        student_id=student_id,
        session_id="ses-1",
        skill="linear_equations",
        misconception="sign_error",
        intervention="SHOW_WORKED_EXAMPLE",
        context_event=make_event("ses-1", LearningEventType.CONTENT_PRESENTED, "evt-c", student_id),
        evidence_events=[make_event("ses-1", LearningEventType.ANSWER_EVALUATED, "evt-a1", student_id)],
        outcome_event=make_event("ses-1", LearningEventType.ANSWER_EVALUATED, "evt-o", student_id),
        outcome_correct=False,
        outcome_hint_level=0,
        outcome_content_id="transfer-1",
        teaching_content_id="teach-1",
        summary="Transfer item still failed.",
    )
    validated = builder.validate(episode)
    assert validated.status == "insufficient_outcome"


def test_list_validated_episodes_filtered(env: tuple[EpisodeBuilder, str]) -> None:
    builder, student_id = env
    first = builder.build_candidate(
        student_id=student_id,
        session_id="ses-1",
        skill="linear_equations",
        misconception="sign_error",
        intervention="SHOW_WORKED_EXAMPLE",
        context_event=make_event("ses-1", LearningEventType.CONTENT_PRESENTED, "evt-c", student_id),
        evidence_events=[make_event("ses-1", LearningEventType.ANSWER_EVALUATED, "evt-a1", student_id)],
        outcome_event=make_event("ses-1", LearningEventType.ANSWER_EVALUATED, "evt-o", student_id),
        outcome_correct=True,
        outcome_hint_level=0,
        outcome_content_id="transfer-1",
        teaching_content_id="teach-1",
        summary="s1",
    )
    builder.validate(first)
    second = builder.build_candidate(
        student_id=student_id,
        session_id="ses-2",
        skill="linear_equations",
        misconception="sign_error",
        intervention="SHOW_WORKED_EXAMPLE",
        context_event=make_event("ses-2", LearningEventType.CONTENT_PRESENTED, "evt-c2", student_id),
        evidence_events=[make_event("ses-2", LearningEventType.ANSWER_EVALUATED, "evt-a2", student_id)],
        outcome_event=make_event("ses-2", LearningEventType.ANSWER_EVALUATED, "evt-o2", student_id),
        outcome_correct=True,
        outcome_hint_level=0,
        outcome_content_id="transfer-2",
        teaching_content_id="teach-2",
        summary="s2",
    )
    builder.validate(second)

    results = builder.list_validated_episodes(student_id=student_id)
    assert len(results) == 2
    sign = builder.list_validated_episodes(
        student_id=student_id, skill="linear_equations", misconception="sign_error"
    )
    assert len(sign) == 2
    other = builder.list_validated_episodes(
        student_id=student_id, skill="linear_equations", misconception="inverse_error"
    )
    assert other == []
    assert builder.has_successful_episode(
        student_id=student_id, skill="linear_equations", misconception="sign_error"
    )
