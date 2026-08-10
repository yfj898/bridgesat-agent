"""Acceptance test 8: LLM timeout triggers deterministic PG fallback.

Mnemis/LLM calls have a strict timeout; on timeout or unavailability the PG
route returns the same bounded actions, and Mnemis failure never blocks the
learning loop. The route choice is recorded so fallback is measurable.
"""

from __future__ import annotations

import asyncio
import time

import psycopg

from app.memory.episode_builder import EpisodeBuilder
from app.memory.fallback_backend import FallbackStudentMemory
from app.memory.mnemis_backend import MnemisUnavailableError


class TimeoutThenFailAdapter:
    async def recall_similar(self, query: dict) -> list[dict]:
        await asyncio.sleep(2.0)  # materially exceeds the 800 ms budget
        raise MnemisUnavailableError("mnemis timed out")

    async def health(self) -> bool:
        return False


def _episode_event(session_id: str, event_id: str, student_id: str):
    from app.domain.events import LearningEvent, LearningEventType

    return LearningEvent(
        event_id=event_id,
        student_id=student_id,
        session_id=session_id,
        event_type=LearningEventType.ANSWER_EVALUATED,
        payload={},
        occurred_at="2026-08-07T10:00:00+00:00",
        received_at="2026-08-07T10:00:00+00:00",
    )


def _seed_episode(connection: psycopg.Connection, student_id: str) -> None:
    builder = EpisodeBuilder(connection)
    episode = builder.build_candidate(
        student_id=student_id,
        session_id="ses-1",
        skill="linear_equations",
        misconception="sign_error",
        intervention="SHOW_WORKED_EXAMPLE",
        context_event=_episode_event("ses-1", "ctx_1", student_id),
        evidence_events=[_episode_event("ses-1", "obs_1", student_id)],
        outcome_event=_episode_event("ses-1", "out_1", student_id),
        outcome_correct=True,
        outcome_hint_level=0,
        outcome_content_id="transfer_1",
        teaching_content_id="taught_1",
        summary="x",
    )
    builder.validate(episode)


def test_mnemis_timeout_returns_pg_hits_within_budget(db: psycopg.Connection, two_students) -> None:
    (student_id, _), _ = two_students
    _seed_episode(db, student_id)
    memory = FallbackStudentMemory(
        db, mnemis=TimeoutThenFailAdapter(), timeout_ms=800
    )
    started = time.perf_counter()
    result = asyncio.run(
        memory.recall_similar(
            student_id=student_id, skill="linear_equations", misconception="sign_error"
        )
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    assert result.route == "pg"
    assert len(result.hits) == 1
    assert result.hits[0].episode_id.startswith("ep_")
    assert result.hits[0].retrieval_route == "pg"
    # The adapter must hit the 800 ms timeout, while the authoritative PG
    # fallback remains prompt instead of waiting for the adapter's full sleep.
    assert 700 <= elapsed_ms < 1200, f"fallback took {elapsed_ms:.0f} ms"


def test_mnemis_unavailable_never_raises_to_caller(db: psycopg.Connection, two_students) -> None:
    (student_id, _), _ = two_students
    _seed_episode(db, student_id)
    memory = FallbackStudentMemory(db, mnemis=TimeoutThenFailAdapter(), timeout_ms=800)
    result = asyncio.run(
        memory.recall_similar(
            student_id=student_id, skill="linear_equations", misconception="sign_error"
        )
    )
    metrics = memory.recall_metrics()
    assert result.route == "pg"
    assert metrics["memory_fallback_rate"] == 1.0
    assert metrics["memory_route_counts"] == {"pg": 1}


def test_pg_route_drives_the_same_bounded_action(db: psycopg.Connection, two_students) -> None:
    (student_id, _), _ = two_students
    _seed_episode(db, student_id)
    memory = FallbackStudentMemory(db, mnemis=TimeoutThenFailAdapter(), timeout_ms=800)
    result = asyncio.run(
        memory.recall_similar(
            student_id=student_id, skill="linear_equations", misconception="sign_error"
        )
    )
    from app.agent.policy import PolicyInput, decide_next_action
    from app.domain.memory import BoundedAction
    from app.domain.sessions import SessionState

    outcome = decide_next_action(
        PolicyInput(
            student_id=student_id,
            session_id="ses-2",
            skill="linear_equations",
            subskill="sign_handling",
            active_misconception="sign_error",
            misconception_observation_count=1,
            recalled_successful_episode=bool(result.hits),
            recalled_episode_ids=[h.episode_id for h in result.hits],
            minutes_remaining=15,
            state=SessionState.ANSWER_EVALUATED,
        )
    )
    assert outcome.decision.action == BoundedAction.SHOW_WORKED_EXAMPLE.value
    assert outcome.decision.reason_code == "RECALLED_SUCCESSFUL_EPISODE"
    assert outcome.decision.episode_ids == [h.episode_id for h in result.hits]
