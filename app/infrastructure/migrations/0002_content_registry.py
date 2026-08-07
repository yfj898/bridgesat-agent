"""0002: content_registry.

Reviewed skill/source/content registry. Content tables are the authoritative
registry of approved educational content; FTS and packs are derived indexes.
"""

from __future__ import annotations

import sqlite3


def migrate(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS skills (
            skill_id TEXT PRIMARY KEY,
            skill_version INTEGER NOT NULL DEFAULT 1,
            display_name TEXT NOT NULL,
            domain TEXT NOT NULL,
            is_deferred_extension INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS skill_prerequisites (
            skill_id TEXT NOT NULL,
            prerequisite_skill_id TEXT NOT NULL,
            max_hops INTEGER NOT NULL DEFAULT 2,
            evidence_note TEXT NOT NULL,
            reviewer_status TEXT NOT NULL,
            PRIMARY KEY (skill_id, prerequisite_skill_id),
            FOREIGN KEY (skill_id) REFERENCES skills(skill_id),
            FOREIGN KEY (prerequisite_skill_id) REFERENCES skills(skill_id)
        );

        CREATE TABLE IF NOT EXISTS content_sources (
            source_id TEXT PRIMARY KEY,
            source_name TEXT NOT NULL,
            source_type TEXT NOT NULL,
            license TEXT NOT NULL,
            redistribution_allowed INTEGER NOT NULL DEFAULT 0,
            rag_ingestion_allowed INTEGER NOT NULL DEFAULT 0,
            access_method TEXT NOT NULL,
            attribution TEXT NOT NULL,
            maintenance_status TEXT NOT NULL,
            last_verified_at TEXT
        );

        CREATE TABLE IF NOT EXISTS content_items (
            content_id TEXT PRIMARY KEY,
            schema_version TEXT NOT NULL,
            domain TEXT NOT NULL,
            content_type TEXT NOT NULL,
            stable_version INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'draft',
            license_snapshot_json TEXT,
            source_lineage_json TEXT NOT NULL,
            canonical_body_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            withdrawn_at TEXT,
            withdrawn_reason TEXT
        );

        CREATE TABLE IF NOT EXISTS content_item_versions (
            content_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            item_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (content_id, version),
            FOREIGN KEY (content_id) REFERENCES content_items(content_id)
        );

        CREATE TABLE IF NOT EXISTS content_reviews (
            review_id TEXT PRIMARY KEY,
            content_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            reviewer_role TEXT NOT NULL,
            reviewer_id TEXT NOT NULL,
            reviewed_at TEXT NOT NULL,
            conclusion TEXT NOT NULL,
            notes TEXT,
            release_batch TEXT,
            FOREIGN KEY (content_id) REFERENCES content_items(content_id)
        );

        CREATE TABLE IF NOT EXISTS content_packs (
            pack_id TEXT PRIMARY KEY,
            pack_version TEXT NOT NULL,
            status TEXT NOT NULL,
            manifest_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS content_pack_items (
            pack_id TEXT NOT NULL,
            content_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            PRIMARY KEY (pack_id, content_id, version),
            FOREIGN KEY (pack_id) REFERENCES content_packs(pack_id),
            FOREIGN KEY (content_id) REFERENCES content_items(content_id)
        );
        """
    )