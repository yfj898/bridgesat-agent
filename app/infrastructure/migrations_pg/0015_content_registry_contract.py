"""0015: content registry contract aligned with the governed content pipeline.

The SQLite-era importer wrote a richer registry (source flags, license
snapshots, lineage, hashes, withdrawal fields, manifest JSON, review roles).
This migration backfills the PostgreSQL registry columns with deterministic
defaults, then makes shared content read-only to the app role: publishing
belongs to the admin connection used by the importer, while runtime retrieval
keeps SELECT.

REVOKE INSERT/UPDATE/DELETE on the shared content tables and the knowledge
index from ``bridgesat_app``; SELECT remains for governed retrieval.
"""
from __future__ import annotations

import psycopg

SHARED_TABLES = (
    "skills",
    "skill_prerequisites",
    "content_sources",
    "content_items",
    "content_item_versions",
    "content_reviews",
    "content_packs",
    "content_pack_items",
    "knowledge_fts",
)


def migrate(connection: psycopg.Connection) -> None:
    connection.execute(
        """
        ALTER TABLE content_sources
        ADD COLUMN IF NOT EXISTS source_name TEXT,
        ADD COLUMN IF NOT EXISTS source_type TEXT,
        ADD COLUMN IF NOT EXISTS redistribution_allowed BOOLEAN NOT NULL DEFAULT FALSE,
        ADD COLUMN IF NOT EXISTS rag_ingestion_allowed BOOLEAN NOT NULL DEFAULT FALSE,
        ADD COLUMN IF NOT EXISTS access_method TEXT NOT NULL DEFAULT 'pack_import',
        ADD COLUMN IF NOT EXISTS attribution TEXT NOT NULL DEFAULT '',
        ADD COLUMN IF NOT EXISTS maintenance_status TEXT NOT NULL DEFAULT 'candidate_generation_only',
        ADD COLUMN IF NOT EXISTS last_verified_at TEXT
        """
    )
    connection.execute(
        """
        ALTER TABLE content_items
        ADD COLUMN IF NOT EXISTS schema_version TEXT NOT NULL DEFAULT 'v1',
        ADD COLUMN IF NOT EXISTS domain TEXT NOT NULL DEFAULT 'math',
        ADD COLUMN IF NOT EXISTS stable_version INTEGER NOT NULL DEFAULT 1,
        ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'draft',
        ADD COLUMN IF NOT EXISTS license_snapshot_json TEXT,
        ADD COLUMN IF NOT EXISTS source_lineage_json TEXT NOT NULL DEFAULT '{}',
        ADD COLUMN IF NOT EXISTS canonical_body_hash TEXT NOT NULL DEFAULT '',
        ADD COLUMN IF NOT EXISTS created_at TEXT,
        ADD COLUMN IF NOT EXISTS withdrawn_at TEXT,
        ADD COLUMN IF NOT EXISTS withdrawn_reason TEXT
        """
    )
    connection.execute(
        """
        ALTER TABLE content_item_versions
        ADD COLUMN IF NOT EXISTS item_json TEXT,
        ADD COLUMN IF NOT EXISTS content_hash TEXT NOT NULL DEFAULT '',
        ADD COLUMN IF NOT EXISTS created_at TEXT
        """
    )
    connection.execute(
        """
        ALTER TABLE content_reviews
        ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1,
        ADD COLUMN IF NOT EXISTS reviewer_role TEXT NOT NULL DEFAULT '',
        ADD COLUMN IF NOT EXISTS reviewer_id TEXT NOT NULL DEFAULT '',
        ADD COLUMN IF NOT EXISTS conclusion TEXT NOT NULL DEFAULT '',
        ADD COLUMN IF NOT EXISTS notes TEXT,
        ADD COLUMN IF NOT EXISTS release_batch TEXT
        """
    )
    connection.execute(
        """
        ALTER TABLE content_packs
        ADD COLUMN IF NOT EXISTS manifest_json TEXT NOT NULL DEFAULT '{}'
        """
    )
    connection.execute(
        """
        ALTER TABLE content_pack_items
        ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1
        """
    )
    for table in SHARED_TABLES:
        connection.execute(
            f"REVOKE INSERT, UPDATE, DELETE ON TABLE {table} FROM bridgesat_app"
        )
