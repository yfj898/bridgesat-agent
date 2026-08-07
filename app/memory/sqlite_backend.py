from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.domain.memory import (
    FACT_PROMOTION_MIN_CONFIDENCE,
    Episode,
    InterventionStat,
    MemoryFact,
)

from .episode_builder import utc_now_iso

FACT_CATEGORY_MISCONCEPTION_INTERVENTION = "misconception_intervention"


class SQLiteMemory:
    """Authoritative SQLite episodic memory: episode recall, semantic facts,
    intervention aggregates. Mnemis (when enabled) indexes from this store."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    # ---------- episode recall ----------

    def recall_episodes(
        self,
        *,
        student_id: str,
        skill: str,
        misconception: str | None = None,
        limit: int = 5,
    ) -> list[Episode]:
        clauses = ["student_id = ?", "status = 'validated'", "skill = ?"]
        params: list[object] = [student_id, skill]
        if misconception is not None:
            clauses.append("misconception = ?")
            params.append(misconception)
        where = " AND ".join(clauses)
        params.append(limit)
        with sqlite3.connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                f"""
                SELECT * FROM learning_episodes
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [_episode_from_row(row) for row in rows]

    # ---------- semantic facts ----------

    def upsert_fact_for_episode(self, episode: Episode) -> MemoryFact:
        """Create or update a semantic fact from a validated episode.

        Normalized key: skill + misconception + intervention. Promotion:
        observation -> inference (2 episodes on distinct items),
        inference -> stable (>=3 episodes across 2 sessions, confidence >= 0.70).
        """
        key = fact_key(episode)
        supporting = self.list_episodes_for_fact(episode.student_id, key)
        sessions = {e.session_id for e in supporting}
        distinct_items = {
            e.outcome.get("outcome_content_id") for e in supporting
        }
        supporting_ids = [e.episode_id for e in supporting]
        now = utc_now_iso()

        if len(supporting_ids) >= 3 and len(sessions) >= 2:
            status = "stable"
            confidence = min(0.9, 0.5 + 0.1 * len(supporting_ids))
        elif len(distinct_items) >= 2:
            status = "inference"
            confidence = 0.6
        else:
            status = "observation"
            confidence = 0.5

        fact_text = (
            f"{episode.skill} {episode.misconception or 'errors'} respond "
            f"positively to {episode.intervention}."
        )
        with sqlite3.connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT * FROM student_memory_facts
                WHERE student_id = ? AND normalized_key = ?
                """,
                (episode.student_id, key),
            ).fetchone()
            if row is None:
                fact = MemoryFact(
                    fact_id=f"fact_{uuid.uuid4().hex[:12]}",
                    student_id=episode.student_id,
                    category=FACT_CATEGORY_MISCONCEPTION_INTERVENTION,
                    normalized_key=key,
                    fact_text=fact_text,
                    confidence=confidence,
                    supporting_episode_ids=supporting_ids,
                    contradicting_episode_ids=[],
                    evidence_count=len(supporting_ids),
                    contradiction_count=0,
                    status=status,
                    first_observed_at=now,
                    last_observed_at=now,
                    version=1,
                )
                connection.execute(
                    """
                    INSERT INTO student_memory_facts (
                        fact_id, student_id, category, normalized_key, fact_text,
                        confidence, supporting_episode_ids_json,
                        contradicting_episode_ids_json, evidence_count,
                        contradiction_count, status, first_observed_at,
                        last_observed_at, version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fact.fact_id,
                        fact.student_id,
                        fact.category,
                        fact.normalized_key,
                        fact.fact_text,
                        fact.confidence,
                        json.dumps(fact.supporting_episode_ids),
                        json.dumps(fact.contradicting_episode_ids),
                        fact.evidence_count,
                        fact.contradiction_count,
                        fact.status,
                        fact.first_observed_at,
                        fact.last_observed_at,
                        fact.version,
                    ),
                )
            else:
                fact = MemoryFact(
                    fact_id=row["fact_id"],
                    student_id=row["student_id"],
                    category=row["category"],
                    normalized_key=row["normalized_key"],
                    fact_text=row["fact_text"],
                    confidence=confidence,
                    supporting_episode_ids=supporting_ids,
                    contradicting_episode_ids=json.loads(
                        row["contradicting_episode_ids_json"] or "[]"
                    ),
                    evidence_count=len(supporting_ids),
                    contradiction_count=row["contradiction_count"],
                    status=status,
                    first_observed_at=row["first_observed_at"],
                    last_observed_at=now,
                    version=row["version"] + 1,
                )
                connection.execute(
                    """
                    UPDATE student_memory_facts
                    SET confidence = ?, supporting_episode_ids_json = ?,
                        evidence_count = ?, status = ?, last_observed_at = ?,
                        version = ?
                    WHERE student_id = ? AND normalized_key = ?
                    """,
                    (
                        fact.confidence,
                        json.dumps(fact.supporting_episode_ids),
                        fact.evidence_count,
                        fact.status,
                        now,
                        fact.version,
                        fact.student_id,
                        fact.normalized_key,
                    ),
                )
        return fact

    def list_episodes_for_fact(self, student_id: str, key: str) -> list[Episode]:
        skill, misconception, intervention = key.split("\x00")
        return self.recall_episodes(
            student_id=student_id,
            skill=skill,
            misconception=misconception or None,
            limit=50,
        )

    def get_facts(self, student_id: str) -> list[MemoryFact]:
        with sqlite3.connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM student_memory_facts WHERE student_id = ?",
                (student_id,),
            ).fetchall()
        return [_fact_from_row(row) for row in rows]

    def get_fact(self, fact_id: str) -> MemoryFact | None:
        with sqlite3.connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM student_memory_facts WHERE fact_id = ?", (fact_id,)
            ).fetchone()
        if row is None:
            return None
        return _fact_from_row(row)

    # ---------- intervention stats ----------

    def record_intervention_outcome(
        self,
        *,
        student_id: str,
        skill: str,
        misconception: str | None,
        intervention: str,
        difficulty_band: str,
        window: str,
        component_score: float,
        weight: float,
    ) -> InterventionStat:
        now = utc_now_iso()
        stat_id = None
        with sqlite3.connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT stat_id FROM intervention_stats
                WHERE student_id = ? AND skill = ? AND misconception IS ? AND intervention = ?
                  AND difficulty_band = ?
                """,
                (student_id, skill, misconception, intervention, difficulty_band),
            ).fetchone()
            if row is None:
                stat_id = f"is_{uuid.uuid4().hex[:12]}"
                connection.execute(
                    """
                    INSERT INTO intervention_stats (
                        stat_id, student_id, skill, misconception, intervention,
                        difficulty_band, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (stat_id, student_id, skill, misconception, intervention, difficulty_band, now),
                )
            else:
                stat_id = row[0]
            connection.execute(
                f"""
                UPDATE intervention_stats
                SET {window}_correct = {window}_correct + ?,
                    {window}_attempts = {window}_attempts + 1,
                    {window}_weight = {window}_weight + ?,
                    updated_at = ?
                WHERE stat_id = ?
                """,
                (component_score, weight, now, stat_id),
            )
        return self.get_intervention_stat(stat_id)

    def get_intervention_stat(self, stat_id: str) -> InterventionStat:
        with sqlite3.connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM intervention_stats WHERE stat_id = ?", (stat_id,)
            ).fetchone()
        if row is None:
            raise KeyError(stat_id)
        return _stat_from_row(row)


def fact_key(episode: Episode) -> str:
    return "\x00".join([episode.skill, episode.misconception or "", episode.intervention])


def _episode_from_row(row: sqlite3.Row) -> Episode:
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


def _fact_from_row(row: sqlite3.Row) -> MemoryFact:
    return MemoryFact(
        fact_id=row["fact_id"],
        student_id=row["student_id"],
        category=row["category"],
        normalized_key=row["normalized_key"],
        fact_text=row["fact_text"],
        confidence=row["confidence"],
        supporting_episode_ids=json.loads(row["supporting_episode_ids_json"] or "[]"),
        contradicting_episode_ids=json.loads(row["contradicting_episode_ids_json"] or "[]"),
        evidence_count=row["evidence_count"],
        contradiction_count=row["contradiction_count"],
        status=row["status"],
        first_observed_at=row["first_observed_at"],
        last_observed_at=row["last_observed_at"],
        version=row["version"],
    )


def _stat_from_row(row: sqlite3.Row) -> InterventionStat:
    return InterventionStat(
        stat_id=row["stat_id"],
        student_id=row["student_id"],
        skill=row["skill"],
        misconception=row["misconception"],
        intervention=row["intervention"],
        difficulty_band=row["difficulty_band"],
        immediate_correct=row["immediate_correct"],
        immediate_attempts=row["immediate_attempts"],
        immediate_weight=row["immediate_weight"],
        short_term_correct=row["short_term_correct"],
        short_term_attempts=row["short_term_attempts"],
        short_term_weight=row["short_term_weight"],
        delayed_correct=row["delayed_correct"],
        delayed_attempts=row["delayed_attempts"],
        delayed_weight=row["delayed_weight"],
        updated_at=row["updated_at"],
    )
