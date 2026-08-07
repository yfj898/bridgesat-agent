"""FallbackStudentMemory tests (ARCHITECTURE §8.6, plan §10).

Chain: Mnemis within strict timeout -> SQLite -> offline snapshot. The SQLite
route must always produce actions when Mnemis is unavailable or too slow, and
fallback must be measurable.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from app.domain.memory import Episode
from app.infrastructure import migration_runner
from app.infrastructure.learner_store import LearnerStore
from app.memory.episode_builder import EpisodeBuilder
from app.memory.fallback_backend import FallbackStudentMemory
from app.memory.mnemis_backend import MnemisUnavailableError
from app.memory.mnemis_stub import InMemoryMnemisIndex


@pytest.fixture()
def db(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "fallback.db"
    migration_runner.apply_migrations(path)
    learner = LearnerStore(path)
    student_id, _ = learner.create_student("Ari", 20, 1200)
    builder = EpisodeBuilder(path)
    from tests.test_memory_outbox_wiring import _event as make_event

    episode = builder.build_candidate(
        student_id=student_id,
        session_id="ses-1",
        skill="linear_equations",
        misconception="sign_error",
        intervention="SHOW_WORKED_EXAMPLE",
        context_event=make_event("ses-1", "ctx", student_id),
        evidence_events=[make_event("ses-1", "obs", student_id)],
        outcome_event=make_event("ses-1", "out", student_id),
        outcome_correct=True,
        outcome_hint_level=0,
        outcome_content_id="transfer",
        teaching_content_id="taught",
        summary="x",
    )
    builder.validate(episode)
    return path, student_id


class FailingIndex:
    async def recall_similar(self, query, **kwargs):
        raise MnemisUnavailableError("down")

    async def health(self) -> bool:
        return False


class SlowIndex:
    async def recall_similar(self, query, **kwargs):
        await asyncio.sleep(0.5)
        return []

    async def health(self) -> bool:
        return True


def test_mnemis_results_take_priority(db: tuple[Path, str]) -> None:
    path, student_id = db
    mnemis = InMemoryMnemisIndex()
    asyncio.run(
        mnemis.upsert_episode(
            {
                "episode_id": "ep_idx",
                "student_id": student_id,
                "skill": "linear_equations",
                "misconception": "sign_error",
                "confidence": 0.9,
            },
            idempotency_key="k1",
        )
    )
    memory = FallbackStudentMemory(path, mnemis=mnemis)
    result = asyncio.run(
        memory.recall_similar(
            student_id=student_id, skill="linear_equations", misconception="sign_error"
        )
    )
    assert result.route == "mnemis_system1"
    assert [r.episode_id for r in result.hits] == ["ep_idx"]
    metrics = memory.recall_metrics()
    assert metrics["memory_fallback_rate"] == 0.0


def test_mnemis_unavailable_falls_back_to_sqlite(db: tuple[Path, str]) -> None:
    path, student_id = db
    memory = FallbackStudentMemory(path, mnemis=FailingIndex())
    result = asyncio.run(
        memory.recall_similar(
            student_id=student_id, skill="linear_equations", misconception="sign_error"
        )
    )
    assert result.route == "sqlite"
    assert result.hits
    metrics = memory.recall_metrics()
    assert metrics["memory_fallback_rate"] == 1.0
    assert metrics["memory_route_counts"]["sqlite"] == 1
    assert metrics["memory_route_counts"].get("mnemis_system1", 0) == 0


def test_slow_mnemis_does_not_block_sqlite(db: tuple[Path, str]) -> None:
    path, student_id = db
    memory = FallbackStudentMemory(path, mnemis=SlowIndex(), timeout_ms=200)
    started = time.perf_counter()
    result = asyncio.run(
        memory.recall_similar(
            student_id=student_id, skill="linear_equations", misconception="sign_error"
        )
    )
    elapsed = time.perf_counter() - started
    assert result.route == "sqlite"
    assert elapsed < 0.4
    assert memory.recall_metrics()["memory_fallback_rate"] == 1.0


def test_offline_snapshot_is_last_resort(db: tuple[Path, str]) -> None:
    path, student_id = db
    from app.memory.episode_builder import utc_now_iso

    snapshot = [
        Episode(
            episode_id="snap_1",
            student_id=student_id,
            session_id="ses-snap",
            skill="linear_equations",
            misconception="sign_error",
            intervention="SHOW_WORKED_EXAMPLE",
            outcome={"correct": True, "hint_level": 0, "different_item": True},
            effectiveness=1.0,
            evidence_event_ids=["e1"],
            summary="from snapshot",
            confidence=0.8,
            status="validated",
            created_at=utc_now_iso(),
            updated_at=utc_now_iso(),
        )
    ]

    class SnapshotProvider:
        def recall_episodes(self, *, student_id, skill, misconception=None, limit=5):
            return snapshot

    memory = FallbackStudentMemory(path, mnemis=FailingIndex(), offline_snapshot=SnapshotProvider())
    result = asyncio.run(
        memory.recall_similar(student_id=student_id, skill="ratios_percentages")
    )
    assert result.route == "offline_snapshot"
    assert [r.episode_id for r in result.hits] == ["snap_1"]


def test_no_mnemis_configured_uses_sqlite(db: tuple[Path, str]) -> None:
    path, student_id = db
    memory = FallbackStudentMemory(path, mnemis=None)
    result = asyncio.run(
        memory.recall_similar(
            student_id=student_id, skill="linear_equations", misconception="sign_error"
        )
    )
    assert result.route == "sqlite"
    assert result.hits
