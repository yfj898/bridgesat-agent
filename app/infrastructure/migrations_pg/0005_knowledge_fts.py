"""0005: knowledge search index (PG tsvector).

Replaces the SQLite FTS5 virtual table with a regular table carrying a
generated tsvector column and a GIN index. This is the Level-1 fallback
retrieval path; Milvus vector search (Level 2) comes in Plan 2.
"""
from __future__ import annotations

import psycopg


def migrate(connection: psycopg.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge_fts (
            content_id TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            content_type TEXT NOT NULL,
            target_skill TEXT NOT NULL,
            target_subskill TEXT NOT NULL DEFAULT '',
            audience TEXT NOT NULL DEFAULT 'student',
            license_id TEXT NOT NULL DEFAULT '',
            license_name TEXT NOT NULL DEFAULT '',
            source_id TEXT NOT NULL DEFAULT '',
            review_status TEXT NOT NULL DEFAULT 'published',
            body TEXT NOT NULL DEFAULT '',
            body_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', body)) STORED
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_fts_tsv "
        "ON knowledge_fts USING GIN (body_tsv)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge_index_log (
            log_id BIGSERIAL PRIMARY KEY,
            indexed_at TEXT NOT NULL,
            pack_id TEXT NOT NULL,
            pack_version TEXT NOT NULL,
            item_count INTEGER NOT NULL,
            lesson_count INTEGER NOT NULL,
            status TEXT NOT NULL
        )
        """
    )
