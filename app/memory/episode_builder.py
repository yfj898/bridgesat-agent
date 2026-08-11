from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import psycopg

from app.domain.events import LearningEvent
from app.domain.memory import (
    EPISODE_MIN_CONFIDENCE,
    Episode,
    outcome_component_score,
)

from .outbox import OutboxRepository, ensure_active_student, student_advisory_lock

DEFAULT_FACT_STATUS = "observation"


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class EpisodeBuilder:
    """Forms episodes from a validated sequence of events.

    An episode requires: valid context, one or more observations, an
    intervention actually shown, at least one outcome on a different item,
    content and policy versions, and confidence >= 0.50.
    """

    def __init__(self, connection: psycopg.Connection) -> None:
        self.connection = connection
        self.outbox = OutboxRepository(connection)

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
        commit: bool = True,
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
        self._insert_episode(self.connection, episode, commit=commit)
        return episode

    def validate(self, episode: Episode, *, commit: bool = True) -> Episode:
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
        with student_advisory_lock(self.connection, episode.student_id):
            try:
                ensure_active_student(self.connection, episode.student_id)
                self.connection.execute(
                    "UPDATE learning_episodes SET status = %s, updated_at = %s "
                    "WHERE episode_id = %s AND student_id = %s",
                    (
                        status,
                        updated.updated_at,
                        episode.episode_id,
                        episode.student_id,
                    ),
                )
                if status == "validated":
                    self.outbox.enqueue(
                        self.connection,
                        student_id=episode.student_id,
                        aggregate_type="episode",
                        aggregate_id=episode.episode_id,
                        operation="upsert_episode",
                        payload=updated.model_dump(),
                        version=1,
                        now=updated.updated_at,
                    )
                if commit:
                    self.connection.commit()
            except BaseException:
                if commit:
                    self.connection.rollback()
                raise
        return updated

    def _insert_episode(
        self,
        connection: psycopg.Connection,
        episode: Episode,
        *,
        commit: bool = True,
    ) -> None:
        with student_advisory_lock(connection, episode.student_id):
            try:
                ensure_active_student(connection, episode.student_id)
                connection.execute(
                    """
                    INSERT INTO learning_episodes (
                        tenant_id, episode_id, student_id, session_id, skill, misconception,
                        intervention, outcome_json, effectiveness, evidence_event_ids_json,
                        summary, confidence, status, created_at, updated_at
                    ) VALUES (
                        current_setting('app.tenant_id'), %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        episode.episode_id,
                        episode.student_id,
                        episode.session_id,
                        episode.skill,
                        episode.misconception,
                        episode.intervention,
                        _json(episode.outcome),
                        episode.effectiveness,
                        _json(episode.evidence_event_ids),
                        episode.summary,
                        episode.confidence,
                        episode.status,
                        episode.created_at,
                        episode.updated_at,
                    ),
                )
                if commit:
                    connection.commit()
            except BaseException:
                if commit:
                    connection.rollback()
                raise

    def start_runtime_candidate(
        self,
        *,
        student_id: str,
        session_id: str,
        skill: str,
        misconception: str,
        intervention: str,
        teaching_content_id: str,
        trigger_content_id: str,
        evidence_event_id: str,
        episode_id: str | None = None,
        commit: bool = True,
    ) -> Episode:
        """Record an intervention now and wait for a distinct transfer item."""
        now = utc_now_iso()
        episode = Episode(
            episode_id=episode_id or f"ep_{uuid.uuid4().hex[:12]}",
            student_id=student_id,
            session_id=session_id,
            skill=skill,
            misconception=misconception,
            intervention=intervention,
            outcome={
                "correct": False,
                "hint_level": 0,
                "outcome_content_id": None,
                "teaching_content_id": teaching_content_id,
                "trigger_content_id": trigger_content_id,
                "different_item": False,
                "pending_transfer": True,
            },
            effectiveness=0.0,
            evidence_event_ids=[evidence_event_id],
            summary=(
                f"{intervention} selected for {misconception}; awaiting a distinct "
                "transfer item."
            ),
            confidence=0.0,
            status="candidate",
            created_at=now,
            updated_at=now,
        )
        self._insert_episode(self.connection, episode, commit=commit)
        return episode

    def complete_runtime_candidate(
        self,
        *,
        student_id: str,
        session_id: str,
        skill: str,
        outcome_event_id: str,
        outcome_content_id: str,
        outcome_correct: bool,
        outcome_hint_level: int,
        commit: bool = True,
    ) -> Episode | None:
        """Complete the latest pending intervention with a real transfer answer."""
        rows = self.connection.execute(
            """
            SELECT *
            FROM learning_episodes
            WHERE student_id = %s
              AND tenant_id = current_setting('app.tenant_id', true)
              AND session_id = %s
              AND skill = %s
              AND status = 'candidate'
            ORDER BY created_at DESC
            """,
            (student_id, session_id, skill),
        ).fetchall()
        candidate = next(
            (
                _row_to_episode(row)
                for row in rows
                if json.loads(row["outcome_json"]).get("trigger_content_id")
                != outcome_content_id
            ),
            None,
        )
        if candidate is None:
            return None

        score = outcome_component_score(outcome_correct, outcome_hint_level)
        outcome = {
            "correct": outcome_correct,
            "hint_level": outcome_hint_level,
            "outcome_content_id": outcome_content_id,
            "teaching_content_id": candidate.outcome.get("teaching_content_id"),
            "trigger_content_id": candidate.outcome.get("trigger_content_id"),
            "different_item": True,
            "pending_transfer": False,
        }
        updated = candidate.model_copy(
            update={
                "outcome": outcome,
                "effectiveness": score,
                "evidence_event_ids": [
                    *candidate.evidence_event_ids,
                    outcome_event_id,
                ],
                "summary": (
                    f"{candidate.intervention} for {candidate.misconception or 'the error'} "
                    f"was followed by a {'successful' if outcome_correct else 'failed'} "
                    "distinct transfer item."
                ),
                "confidence": score,
                "updated_at": utc_now_iso(),
            }
        )
        self.connection.execute(
            """
            UPDATE learning_episodes
            SET outcome_json = %s, effectiveness = %s,
                evidence_event_ids_json = %s, summary = %s,
                confidence = %s, updated_at = %s
            WHERE episode_id = %s
              AND student_id = %s
              AND tenant_id = current_setting('app.tenant_id', true)
            """,
            (
                _json(updated.outcome),
                updated.effectiveness,
                _json(updated.evidence_event_ids),
                updated.summary,
                updated.confidence,
                updated.updated_at,
                updated.episode_id,
                updated.student_id,
            ),
        )
        return self.validate(updated, commit=commit)

    def get_episode(self, episode_id: str) -> Episode | None:
        row = self.connection.execute(
            "SELECT * FROM learning_episodes WHERE episode_id = %s", (episode_id,)
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
        clauses = ["student_id = %s", "status = 'validated'"]
        params: list[object] = [student_id]
        if skill is not None:
            clauses.append("skill = %s")
            params.append(skill)
        if misconception is not None:
            clauses.append("misconception = %s")
            params.append(misconception)
        where = " AND ".join(clauses)
        params.append(limit)
        rows = self.connection.execute(
            f"""
            SELECT * FROM learning_episodes
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT %s
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


def _json(value) -> str:
    return json.dumps(value, sort_keys=True)


def _row_to_episode(row: dict) -> Episode:
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
