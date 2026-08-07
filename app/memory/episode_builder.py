from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.domain.events import LearningEvent
from app.domain.memory import (
    EPISODE_MIN_CONFIDENCE,
    Episode,
    InterventionStat,
    MemoryFact,
    outcome_component_score,
)

from app.infrastructure.database import connect, transaction

from .outbox import OutboxRepository

DEFAULT_FACT_STATUS = "observation"


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class EpisodeBuilder:
    """Forms episodes from a validated sequence of events.

    An episode requires: valid context, one or more observations, an
    intervention actually shown, at least one outcome on a different item,
    content and policy versions, and confidence >= 0.50.
    """

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.outbox = OutboxRepository(database_path)

    def build_candidate(
        self,
        *,
        student_id: str,
        session_id: str,
        skill: str,
        misconception: str | None,
        intervention: str,
        context_event: LearningEvent,
        evidence_events: list[LearningEvent],
        outcome_event: LearningEvent,
        outcome_correct: bool,
        outcome_hint_level: int,
        outcome_content_id: str,
        teaching_content_id: str,
        summary: str,
        episode_id: str | None = None,
    ) -> Episode:
        episode_id = episode_id or f"ep_{uuid.uuid4().hex[:12]}"
        now = utc_now_iso()
        score = outcome_component_score(outcome_correct, outcome_hint_level)
        outcome = {
            "correct": outcome_correct,
            "hint_level": outcome_hint_level,
            "outcome_content_id": outcome_content_id,
            "teaching_content_id": teaching_content_id,
            "different_item": outcome_content_id != teaching_content_id,
        }
        episode = Episode(
            episode_id=episode_id,
            student_id=student_id,
            session_id=session_id,
            skill=skill,
            misconception=misconception,
            intervention=intervention,
            outcome=outcome,
            effectiveness=score,
            evidence_event_ids=[e.event_id for e in evidence_events],
            summary=summary,
            confidence=score,
            status="candidate",
            created_at=now,
            updated_at=now,
        )
        with connect(self.database_path) as connection:
            with transaction(connection):
                self._insert_episode(connection, episode)
        return episode

    def validate(self, episode: Episode) -> Episode:
        """Validate a candidate episode against the contract rules."""
        if episode.status != "candidate":
            return episode
        outcome = episode.outcome
        different_item = outcome.get("different_item", False)
        has_intervention = bool(episode.intervention)
        has_outcome = bool(episode.evidence_event_ids)
        valid = (
            different_item
            and has_intervention
            and has_outcome
            and episode.confidence >= EPISODE_MIN_CONFIDENCE
        )
        status = "validated" if valid else "insufficient_outcome"
        updated = episode.model_copy(
            update={
                "status": status,
                "updated_at": utc_now_iso(),
            }
        )
        with connect(self.database_path) as connection:
            with transaction(connection):
                connection.execute(
                    "UPDATE learning_episodes SET status = ?, updated_at = ? WHERE episode_id = ?",
                    (status, updated.updated_at, episode.episode_id),
                )
                if status == "validated":
                    self.outbox.enqueue(
                        connection,
                        student_id=episode.student_id,
                        aggregate_type="episode",
                        aggregate_id=episode.episode_id,
                        operation="upsert_episode",
                        payload=updated.model_dump(),
                        version=1,
                        now=updated.updated_at,
                    )
        return updated

    def _insert_episode(self, connection: sqlite3.Connection, episode: Episode) -> None:
        connection.execute(
            """
            INSERT INTO learning_episodes (
                episode_id, student_id, session_id, skill, misconception,
                intervention, outcome_json, effectiveness, evidence_event_ids_json,
                summary, confidence, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                episode.episode_id,
                episode.student_id,
                episode.session_id,
                episode.skill,
                episode.misconception,
                episode.intervention,
                sqlite3_json(episode.outcome),
                episode.effectiveness,
                sqlite3_json(episode.evidence_event_ids),
                episode.summary,
                episode.confidence,
                episode.status,
                episode.created_at,
                episode.updated_at,
            ),
        )

    def get_episode(self, episode_id: str) -> Episode | None:
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM learning_episodes WHERE episode_id = ?", (episode_id,)
            ).fetchone()
        if row is None:
            return None
        return _row_to_episode(row)

    def list_validated_episodes(
        self,
        *,
        student_id: str,
        skill: str | None = None,
        misconception: str | None = None,
        limit: int = 20,
    ) -> list[Episode]:
        clauses = ["student_id = ?", "status = 'validated'"]
        params: list[object] = [student_id]
        if skill is not None:
            clauses.append("skill = ?")
            params.append(skill)
        if misconception is not None:
            clauses.append("misconception = ?")
            params.append(misconception)
        where = " AND ".join(clauses)
        params.append(limit)
        with connect(self.database_path) as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM learning_episodes
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [_row_to_episode(row) for row in rows]

    def has_successful_episode(self, *, student_id: str, skill: str, misconception: str | None) -> bool:
        episodes = self.list_validated_episodes(
            student_id=student_id, skill=skill, misconception=misconception
        )
        return any(effectiveness_successful(episode) for episode in episodes)


def effectiveness_successful(episode: Episode) -> bool:
    return episode.effectiveness >= 0.6


def sqlite3_json(value) -> str:
    import json

    return json.dumps(value, sort_keys=True)


def _row_to_episode(row: sqlite3.Row) -> Episode:
    import json

    return Episode(
        episode_id=row["episode_id"],
        student_id=row["student_id"],
        session_id=row["session_id"],
        skill=row["skill"],
        misconception=row["misconception"],
        intervention=row["intervention"],
        outcome=json.loads(row["outcome_json"]),
        effectiveness=row["effectiveness"],
        evidence_event_ids=json.loads(row["evidence_event_ids_json"]),
        summary=row["summary"],
        confidence=row["confidence"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
