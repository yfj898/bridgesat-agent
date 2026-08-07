"""Import a built content pack into the content registry.

Writes to the tables created by migration 0002 (content_registry):
``content_sources``, ``content_items``, ``content_item_versions``,
``content_packs``, ``content_pack_items``. The pack itself is the source of
truth for item JSON; the registry stores it for audit and retrieval.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.infrastructure.database import connect, transaction
from app.infrastructure.migration_runner import apply_migrations


def import_pack(
    database_path: Path,
    pack_dir: Path,
    *,
    source_name: str = "DeepMind Mathematics Dataset (concept source only)",
    source_type: str = "candidate_generator",
    source_license: str = "Apache-2.0",
    source_attribution: str = "github.com/google-deepmind/mathematics_dataset (Apache-2.0)",
) -> int:
    """Import a pack directory into the content registry; returns item count."""
    manifest = json.loads((pack_dir / "manifest.json").read_text(encoding="utf-8"))
    pack_id = manifest["pack_id"]
    pack_version = manifest["pack_version"]

    apply_migrations(database_path)
    items = []
    items_path = pack_dir / "items.jsonl"
    if items_path.is_file():
        for line in items_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                items.append(json.loads(line))

    now = datetime.now(timezone.utc).isoformat()
    with connect(database_path) as connection:
        with transaction(connection):
            connection.execute(
                """
                INSERT OR IGNORE INTO content_sources (
                    source_id, source_name, source_type, license,
                    redistribution_allowed, rag_ingestion_allowed, access_method,
                    attribution, maintenance_status, last_verified_at
                ) VALUES (?, ?, ?, ?, 0, 0, 'pack_import', ?, 'candidate_generation_only', ?)
                """,
                (
                    "deepmind_mathematics_dataset",
                    source_name,
                    source_type,
                    source_license,
                    source_attribution,
                    now,
                ),
            )

            inserted = 0
            for item in items:
                content_id = item["id"]
                version = item.get("version", 1)
                lineage = item.get("source_lineage") or {}
                license_snapshot = item.get("license") or {}
                connection.execute(
                    """
                    INSERT OR REPLACE INTO content_items (
                        content_id, schema_version, domain, content_type,
                        stable_version, status, license_snapshot_json,
                        source_lineage_json, canonical_body_hash, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        content_id,
                        item.get("schema_version", "v1"),
                        item.get("domain", "math"),
                        item.get("content_type", "question"),
                        version,
                        item.get("review_status", "approved"),
                        json.dumps(license_snapshot, ensure_ascii=False),
                        json.dumps(lineage, ensure_ascii=False),
                        item.get("content_hash", ""),
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO content_item_versions (
                        content_id, version, item_json, content_hash, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        content_id,
                        version,
                        json.dumps(item, ensure_ascii=False),
                        item.get("content_hash", ""),
                        now,
                    ),
                )
                inserted += 1

            connection.execute(
                """
                INSERT OR REPLACE INTO content_packs (
                    pack_id, pack_version, status, manifest_json, created_at
                ) VALUES (?, ?, 'published', ?, ?)
                """,
                (pack_id, pack_version, json.dumps(manifest, ensure_ascii=False), now),
            )
            for item in items:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO content_pack_items (pack_id, content_id, version)
                    VALUES (?, ?, ?)
                    """,
                    (pack_id, item["id"], item.get("version", 1)),
                )
            return inserted


def verify_import(database_path: Path, pack_id: str = "bridgesat-math") -> dict:
    """Return counts proving the registry, versions, and pack membership."""
    with connect(database_path) as connection:
        items = connection.execute("SELECT COUNT(*) FROM content_items").fetchone()[0]
        versions = connection.execute("SELECT COUNT(*) FROM content_item_versions").fetchone()[0]
        pack_rows = connection.execute(
            "SELECT COUNT(*) FROM content_pack_items WHERE pack_id = ?", (pack_id,)
        ).fetchone()[0]
        sources = connection.execute(
            "SELECT COUNT(*) FROM content_sources WHERE source_id = ?",
            ("deepmind_mathematics_dataset",),
        ).fetchone()[0]
    return {
        "content_items": items,
        "content_item_versions": versions,
        "content_pack_items": pack_rows,
        "deepmind_source_rows": sources,
    }
