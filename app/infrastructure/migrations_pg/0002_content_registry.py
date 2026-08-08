"""0002: content registry (PG). Content is global, NOT tenant-scoped:
published content is shared across tenants and read-only to them."""
from __future__ import annotations

import psycopg


def migrate(connection: psycopg.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS skills (
            skill TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT ''
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_prerequisites (
            skill TEXT NOT NULL,
            prerequisite TEXT NOT NULL,
            PRIMARY KEY (skill, prerequisite)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS content_sources (
            source_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            license TEXT NOT NULL,
            permitted_use TEXT NOT NULL,
            is_trusted BOOLEAN NOT NULL DEFAULT FALSE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS content_items (
            content_id TEXT PRIMARY KEY,
            version INTEGER NOT NULL DEFAULT 1,
            content_type TEXT NOT NULL,
            target_skill TEXT NOT NULL,
            target_subskill TEXT NOT NULL DEFAULT '',
            audience TEXT NOT NULL DEFAULT 'student',
            license_id TEXT NOT NULL DEFAULT '',
            license_name TEXT NOT NULL DEFAULT '',
            source_id TEXT NOT NULL DEFAULT '',
            review_status TEXT NOT NULL DEFAULT 'draft',
            body TEXT NOT NULL DEFAULT ''
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS content_item_versions (
            content_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            versioned_body TEXT NOT NULL,
            versioned_at TEXT NOT NULL,
            PRIMARY KEY (content_id, version)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS content_reviews (
            review_id TEXT PRIMARY KEY,
            content_id TEXT NOT NULL,
            reviewer TEXT NOT NULL,
            decision TEXT NOT NULL,
            reviewed_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS content_packs (
            pack_id TEXT PRIMARY KEY,
            pack_version TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS content_pack_items (
            pack_id TEXT NOT NULL,
            content_id TEXT NOT NULL,
            PRIMARY KEY (pack_id, content_id)
        )
        """
    )
