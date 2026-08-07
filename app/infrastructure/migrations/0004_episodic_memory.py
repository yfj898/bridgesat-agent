"""0004: episodic_memory.

Cross-session learner memory: validated learning episodes, semantic learner
facts with confidence/contradiction, and intervention-effectiveness aggregates.
"""

from __future__ import annotations

import sqlite3


def migrate(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS learning_episodes (
            episode_id TEXT PRIMARY KEY,
            student_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            skill TEXT NOT NULL,
            misconception TEXT,
            intervention TEXT NOT NULL,
            outcome_json TEXT NOT NULL,
            effectiveness REAL NOT NULL,
            evidence_event_ids_json TEXT NOT NULL,
            summary TEXT NOT NULL,
            confidence REAL NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (student_id) REFERENCES students(id)
        );

        CREATE TABLE IF NOT EXISTS student_memory_facts (
            fact_id TEXT PRIMARY KEY,
            student_id TEXT NOT NULL,
            category TEXT NOT NULL,
            normalized_key TEXT NOT NULL,
            fact_text TEXT NOT NULL,
            confidence REAL NOT NULL,
            supporting_episode_ids_json TEXT NOT NULL,
            contradicting_episode_ids_json TEXT NOT NULL,
            evidence_count INTEGER NOT NULL,
            contradiction_count INTEGER NOT NULL,
            status TEXT NOT NULL,
            first_observed_at TEXT NOT NULL,
            last_observed_at TEXT NOT NULL,
            version INTEGER NOT NULL,
            FOREIGN KEY (student_id) REFERENCES students(id)
        );

        CREATE TABLE IF NOT EXISTS intervention_stats (
            stat_id TEXT PRIMARY KEY,
            student_id TEXT NOT NULL,
            skill TEXT NOT NULL,
            misconception TEXT,
            intervention TEXT NOT NULL,
            difficulty_band TEXT NOT NULL,
            immediate_correct REAL NOT NULL DEFAULT 0.0,
            immediate_attempts INTEGER NOT NULL DEFAULT 0,
            immediate_weight REAL NOT NULL DEFAULT 0.0,
            short_term_correct REAL NOT NULL DEFAULT 0.0,
            short_term_attempts INTEGER NOT NULL DEFAULT 0,
            short_term_weight REAL NOT NULL DEFAULT 0.0,
            delayed_correct REAL NOT NULL DEFAULT 0.0,
            delayed_attempts INTEGER NOT NULL DEFAULT 0,
            delayed_weight REAL NOT NULL DEFAULT 0.0,
            updated_at TEXT NOT NULL,
            UNIQUE (student_id, skill, misconception, intervention, difficulty_band),
            FOREIGN KEY (student_id) REFERENCES students(id)
        );
        """
    )