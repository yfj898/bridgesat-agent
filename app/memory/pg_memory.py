"""Authoritative PostgreSQL episodic memory: episode recall, semantic facts,
intervention aggregates. Mnemis (when enabled) indexes from this store.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import psycopg

from app.domain.memory import (
    Episode,
    InterventionStat,
    MemoryFact,
)
from app.infrastructure import pg

from .episode_builder import utc_now_iso
from .outbox import (
    OutboxRepository,
    ensure_active_student,
    student_advisory_lock,
)

FACT_CATEGORY_MISCONCEPTION_INTERVENTION = "misconception_intervention"

# Field separator for normalized fact keys. SQLite tolerated NUL bytes, but
# PostgreSQL text columns cannot contain NUL, so use the ASCII unit
# separator (0x1F) which cannot appear in skills, misconceptions, or
# intervention names.
KEY_SEPARATOR = "\x1f"


class PGMemory:
    """Authoritative PostgreSQL episodic memory: episode recall, semantic
    facts, intervention aggregates. Writes and the memory outbox commit in
    one transaction; the outbox delivers to Mnemis (when enabled)."""

    def __init__(self, connection: psycopg.Connection) -> None:
        self.connection = connection
        self.outbox = OutboxRepository(connection)

    # ---------- episode recall ----------

    def validated_episode_ids(self, student_id: str) -> set[str]:
        """Tenant-scoped, validated episode IDs for one student.

        Mnemis results are accepted only when their supporting episode IDs
        are a non-empty subset of this set, so foreign evidence can never
        reach policy.
        """
        rows = self.connection.execute(
            """
            SELECT episode_id
            FROM learning_episodes
            WHERE student_id = %s
              AND tenant_id = current_setting('app.tenant_id', true)
              AND status = 'validated'
            """,
            (student_id,),
        ).fetchall()
        return {row["episode_id"] for row in rows}

    def recall_episodes(
        self,
        *,
        student_id: str,
        skill: str,
        misconception: str | None = None,
        limit: int = 5,
    ) -> list[Any]:
        clauses = ["student_id = %s", "status = 'validated'", "skill = %s"]
        params: list[object] = [student_id, skill]
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
        return [_episode_from_row(row) for row in rows]

    # ---------- semantic facts ----------

    def upsert_fact_for_episode(self, episode: Any) -> MemoryFact:
        """Create or update a fact while serializing with deletion/writes."""
        with student_advisory_lock(self.connection, episode.student_id):
            try:
                ensure_active_student(self.connection, episode.student_id)
                return self._upsert_fact_for_episode(episode)
            except BaseException:
                self.connection.rollback()
                raise

    def _upsert_fact_for_episode(self, episode: Any) -> MemoryFact:
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
        row = self.connection.execute(
            """
            SELECT * FROM student_memory_facts
            WHERE student_id = %s AND normalized_key = %s
            FOR UPDATE
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
            self.connection.execute(
                """
                INSERT INTO student_memory_facts (
                    tenant_id, fact_id, student_id, category, normalized_key, fact_text,
                    confidence, supporting_episode_ids_json,
                    contradicting_episode_ids_json, evidence_count,
                    contradiction_count, status, first_observed_at,
                    last_observed_at, version
                ) VALUES (
                    current_setting('app.tenant_id'), %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s
                )
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
            self.connection.execute(
                """
                UPDATE student_memory_facts
                SET confidence = %s, supporting_episode_ids_json = %s,
                    evidence_count = %s, status = %s, last_observed_at = %s,
                    version = %s
                WHERE student_id = %s AND normalized_key = %s
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
        self.outbox.enqueue(
            self.connection,
            student_id=episode.student_id,
            aggregate_type="fact",
            aggregate_id=fact.fact_id,
            operation="upsert_fact",
            payload=fact.model_dump(),
            version=fact.version,
            now=now,
        )
        self.connection.commit()
        return fact

    def list_episodes_for_fact(self, student_id: str, key: str) -> list[Any]:
        skill, misconception, intervention = key.split(KEY_SEPARATOR)
        return self.recall_episodes(
            student_id=student_id,
            skill=skill,
            misconception=misconception or None,
            limit=50,
        )

    def get_facts(self, student_id: str) -> list[MemoryFact]:
        rows = self.connection.execute(
            "SELECT * FROM student_memory_facts WHERE student_id = %s",
            (student_id,),
        ).fetchall()
        return [_fact_from_row(row) for row in rows]

    def get_fact(self, fact_id: str) -> MemoryFact | None:
        row = self.connection.execute(
            "SELECT * FROM student_memory_facts WHERE fact_id = %s", (fact_id,)
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
        with student_advisory_lock(self.connection, student_id):
            try:
                ensure_active_student(self.connection, student_id)
                return self._record_intervention_outcome(
                    student_id=student_id,
                    skill=skill,
                    misconception=misconception,
                    intervention=intervention,
                    difficulty_band=difficulty_band,
                    window=window,
                    component_score=component_score,
                    weight=weight,
                )
            except BaseException:
                self.connection.rollback()
                raise

    def _record_intervention_outcome(
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
        row = self.connection.execute(
            """
            SELECT stat_id FROM intervention_stats
            WHERE student_id = %s AND skill = %s AND misconception IS NOT DISTINCT FROM %s
              AND intervention = %s AND difficulty_band = %s
            FOR UPDATE
            """,
            (student_id, skill, misconception, intervention, difficulty_band),
        ).fetchone()
        if row is None:
            stat_id = f"is_{uuid.uuid4().hex[:12]}"
            self.connection.execute(
                """
                INSERT INTO intervention_stats (
                    tenant_id, stat_id, student_id, skill, misconception, intervention,
                    difficulty_band, updated_at
                ) VALUES (
                    current_setting('app.tenant_id'), %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (stat_id, student_id, skill, misconception, intervention, difficulty_band, now),
            )
        else:
            stat_id = row["stat_id"]
        self.connection.execute(
            f"""
            UPDATE intervention_stats
            SET {window}_correct = {window}_correct + %s,
                {window}_attempts = {window}_attempts + 1,
                {window}_weight = {window}_weight + %s,
                updated_at = %s
            WHERE stat_id = %s
            """,
            (component_score, weight, now, stat_id),
        )
        self.connection.commit()
        return self.get_intervention_stat(stat_id)

    def get_intervention_stat(self, stat_id: str) -> InterventionStat:
        row = self.connection.execute(
            "SELECT * FROM intervention_stats WHERE stat_id = %s", (stat_id,)
        ).fetchone()
        if row is None:
            raise KeyError(stat_id)
        return _stat_from_row(row)


def fact_key(episode: Any) -> str:
    return KEY_SEPARATOR.join([episode.skill, episode.misconception or "", episode.intervention])


def _episode_from_row(row: dict) -> Any:
    from app.domain.memory import Episode

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


def _fact_from_row(row: dict) -> MemoryFact:
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


def _stat_from_row(row: dict) -> InterventionStat:
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
