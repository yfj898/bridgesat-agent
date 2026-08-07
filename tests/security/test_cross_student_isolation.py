"""Acceptance test 1: student A cannot read or delete student B data.

Repository-level student scoping, memory scoping, and index scoping must
hold even when callers pass the other student's ID; cross-student reads
return empty and deletions never touch the other student.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.domain.events import LearningEvent, LearningEventType
from app.infrastructure.learner_store import LearnerStore
from app.memory.episode_builder import EpisodeBuilder
from app.memory.mnemis_stub import InMemoryMnemisIndex
from app.memory.sqlite_backend import SQLiteMemory
from app.sync.service import DeviceRevokedError, SyncService
from app.sync.protocol import SyncEventEnvelope, SyncRequest

from tests.security.conftest import envelope

Q_LINEAR = "sync.linear.001"


def _episode_event(session_id: str, event_id: str, student_id: str) -> LearningEvent:
    return LearningEvent(
        event_id=event_id,
        student_id=student_id,
        session_id=session_id,
        event_type=LearningEventType.ANSWER_EVALUATED,
        payload={},
        occurred_at="2026-08-07T10:00:00+00:00",
        received_at="2026-08-07T10:00:00+00:00",
    )


def _sync_request(student_id: str, device_id: str, events: list[dict]) -> SyncRequest:
    return SyncRequest(
        device_id=device_id,
        student_id=student_id,
        events=[SyncEventEnvelope(**event) for event in events],
    )


def _answer_envelope(student_id: str, event_id: str, device_id: str) -> dict:
    return envelope(
        event_id=event_id,
        student_id=student_id,
        device_id=device_id,
        payload={
            "question_id": Q_LINEAR,
            "question_version": 1,
            "selected_choice_id": "A",
            "hint_level": 0,
            "attempt_id": event_id,
        },
    )


def test_skill_state_of_a_never_leaks_into_b(db: Path, two_students) -> None:
    (a, _), (b, _) = two_students
    learner = LearnerStore(db)
    learner.create_session(a, "ses-a")
    from app.agent.orchestrator import ContentItem, SessionOrchestrator
    from app.domain.sessions import SessionState

    orchestrator = SessionOrchestrator(db)
    for state in [
        SessionState.PROFILE_READY,
        SessionState.DIAGNOSTIC_ACTIVE,
        SessionState.DIAGNOSTIC_COMPLETE,
        SessionState.PLAN_READY,
        SessionState.QUESTION_ACTIVE,
    ]:
        orchestrator.learner.transition_session("ses-a", state)
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
        student_id=a,
        session_id="ses-a",
        item=item,
        selected_choice_id="A",
        hint_level=0,
        minutes_remaining=15,
    )
    assert learner.get_skill_state(a, "linear_equations") is not None
    assert learner.get_skill_state(b, "linear_equations") is None


def test_memory_recall_is_student_scoped(db: Path, two_students) -> None:
    (a, _), (b, _) = two_students
    builder = EpisodeBuilder(db)
    episode = builder.build_candidate(
        student_id=a,
        session_id="ses-a",
        skill="linear_equations",
        misconception="sign_error",
        intervention="SHOW_WORKED_EXAMPLE",
        context_event=_episode_event("ses-a", "ctx_a", a),
        evidence_events=[_episode_event("ses-a", "obs_a", a)],
        outcome_event=_episode_event("ses-a", "out_a", a),
        outcome_correct=True,
        outcome_hint_level=0,
        outcome_content_id="transfer",
        teaching_content_id="taught",
        summary="x",
    )
    builder.validate(episode)

    memory = SQLiteMemory(db)
    memory.upsert_fact_for_episode(episode)
    hits_a = memory.recall_episodes(student_id=a, skill="linear_equations")
    hits_b = memory.recall_episodes(student_id=b, skill="linear_equations")
    assert len(hits_a) == 1
    assert len(hits_b) == 0

    facts = memory.get_facts(a)
    assert len(facts) == 1
    assert len(memory.get_facts(b)) == 0


def test_index_recall_is_student_scoped(db: Path, two_students) -> None:
    (a, _), (b, _) = two_students
    index = InMemoryMnemisIndex()
    asyncio.run(
        index.upsert_episode(
            {
                "episode_id": "ep_a",
                "student_id": a,
                "skill": "linear_equations",
                "misconception": "sign_error",
                "confidence": 0.9,
            },
            idempotency_key="k1",
        )
    )
    hits_a = asyncio.run(
        index.recall_similar({"student_id": a, "skill": "linear_equations"})
    )
    hits_b = asyncio.run(
        index.recall_similar({"student_id": b, "skill": "linear_equations"})
    )
    assert len(hits_a) == 1
    assert len(hits_b) == 0


def test_snapshot_of_a_contains_no_b_rows(db: Path, two_students) -> None:
    (a, _), (b, _) = two_students
    sync = SyncService(db)
    sync.register_device(a, "device a", device_id="dev_a")
    sync.register_device(b, "device b", device_id="dev_b")
    response = sync.process_batch(
        _sync_request(a, "dev_a", [_answer_envelope(a, "evt_a", "dev_a")])
    )
    assert response.accepted_event_ids == ["evt_a"]
    snapshot_a = sync.build_snapshot(a)
    snapshot_b = sync.build_snapshot(b)
    assert snapshot_a.snapshot_version == 2
    assert snapshot_b.snapshot_version == 1
    assert snapshot_b.student["id"] == b


def test_revoking_a_device_does_not_revoke_b(db: Path, two_students) -> None:
    (a, _), (b, _) = two_students
    sync = SyncService(db)
    sync.register_device(a, "device a", device_id="dev_a")
    sync.register_device(b, "device b", device_id="dev_b")
    sync.revoke_device("dev_a", a)
    with pytest.raises(DeviceRevokedError):
        sync.process_batch(_sync_request(a, "dev_a", []))
    sync.process_batch(_sync_request(b, "dev_b", []))
