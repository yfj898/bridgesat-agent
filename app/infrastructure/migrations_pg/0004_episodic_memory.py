"""0004: episodic memory (PG). Cross-session learner memory: validated
learning episodes, semantic learner facts, and intervention statistics."""
from __future__ import annotations

import psycopg


def migrate(connection: psycopg.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS learning_episodes (
            episode_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            student_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            skill TEXT NOT NULL,
            misconception TEXT,
            intervention TEXT NOT NULL,
            outcome_json TEXT NOT NULL,
            effectiveness DOUBLE PRECISION NOT NULL,
            evidence_event_ids_json TEXT NOT NULL,
            summary TEXT NOT NULL,
            confidence DOUBLE PRECISION NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS student_memory_facts (
            fact_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            student_id TEXT NOT NULL,
            category TEXT NOT NULL,
            normalized_key TEXT NOT NULL,
            fact_text TEXT NOT NULL,
            confidence DOUBLE PRECISION NOT NULL,
            supporting_episode_ids_json TEXT NOT NULL,
            contradicting_episode_ids_json TEXT NOT NULL,
            evidence_count INTEGER NOT NULL,
            contradiction_count INTEGER NOT NULL,
            status TEXT NOT NULL,
            first_observed_at TEXT NOT NULL,
            last_observed_at TEXT NOT NULL,
            version INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS intervention_stats (
            stat_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            student_id TEXT NOT NULL,
            skill TEXT NOT NULL,
            misconception TEXT,
            intervention TEXT NOT NULL,
            difficulty_band TEXT NOT NULL,
            immediate_correct DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            immediate_attempts INTEGER NOT NULL DEFAULT 0,
            immediate_weight DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            short_term_correct DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            short_term_attempts INTEGER NOT NULL DEFAULT 0,
            short_term_weight DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            delayed_correct DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            delayed_attempts INTEGER NOT NULL DEFAULT 0,
            delayed_weight DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            updated_at TEXT NOT NULL,
            UNIQUE (student_id, skill, misconception, intervention, difficulty_band)
        )
        """
    )
    for table in ("learning_episodes", "student_memory_facts", "intervention_stats"):
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_tenant ON {table} (tenant_id)"
        )
