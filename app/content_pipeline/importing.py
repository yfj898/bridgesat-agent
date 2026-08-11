"""Import a built content pack into the PostgreSQL content registry.

Writes to the tables created by migration 0002 (content_registry) and aligned
by 0015: ``content_sources``, ``content_items``, ``content_item_versions``,
``content_packs``, ``content_pack_items``. The pack itself is the source of
truth for item JSON; the registry stores it for audit and retrieval.

Publishing requires an admin connection: the app role has SELECT only on the
shared content tables (migration 0015). The whole pack is imported inside one
transaction, and repeat imports are idempotent upserts.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import psycopg

from app.infrastructure import pg


def import_pack(
    connection: psycopg.Connection,
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

    items = []
    for filename in ("items.jsonl", "lessons.jsonl"):
        artifact = pack_dir / filename
        if artifact.is_file():
            for line in artifact.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    items.append(json.loads(line))

    now = datetime.now(timezone.utc).isoformat()
    with pg.transaction(connection):
        source_ids = sorted(
            {
                (item.get("source_lineage") or {}).get("source_id")
                for item in items
                if (item.get("source_lineage") or {}).get("source_id")
            }
        )
        for source_id in source_ids:
            if source_id == "bridgesat_original":
                profile = {
                    "name": "BridgeSAT original authored content",
                    "license": "bridgesat_original",
                    "permitted_use": "student_delivery",
                    "trusted": True,
                    "type": "first_party_authored",
                    "redistribution": True,
                    "rag": True,
                    "attribution": "BridgeSAT original educational content",
                    "maintenance": "active",
                }
            else:
                profile = {
                    "name": source_name,
                    "license": source_license,
                    "permitted_use": "candidate_generation_only",
                    "trusted": False,
                    "type": source_type,
                    "redistribution": False,
                    "rag": False,
                    "attribution": source_attribution,
                    "maintenance": "candidate_generation_only",
                }
            connection.execute(
                """
                INSERT INTO content_sources (
                    source_id, name, license, permitted_use, is_trusted,
                    source_name, source_type, redistribution_allowed,
                    rag_ingestion_allowed, access_method, attribution,
                    maintenance_status, last_verified_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                          'pack_import', %s, %s, %s)
                ON CONFLICT (source_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    license = EXCLUDED.license,
                    permitted_use = EXCLUDED.permitted_use,
                    is_trusted = EXCLUDED.is_trusted,
                    source_name = EXCLUDED.source_name,
                    source_type = EXCLUDED.source_type,
                    redistribution_allowed = EXCLUDED.redistribution_allowed,
                    rag_ingestion_allowed = EXCLUDED.rag_ingestion_allowed,
                    attribution = EXCLUDED.attribution,
                    maintenance_status = EXCLUDED.maintenance_status,
                    last_verified_at = EXCLUDED.last_verified_at
                """,
                (
                    source_id,
                    profile["name"],
                    profile["license"],
                    profile["permitted_use"],
                    profile["trusted"],
                    profile["name"],
                    profile["type"],
                    profile["redistribution"],
                    profile["rag"],
                    profile["attribution"],
                    profile["maintenance"],
                    now,
                ),
            )

        inserted = 0
        for item in items:
            content_id = item["id"]
            version = item.get("version", 1)
            lineage = item.get("source_lineage") or {}
            license_snapshot = item.get("license") or {}
            existing_version = connection.execute(
                """
                SELECT content_hash
                FROM content_item_versions
                WHERE content_id = %s AND version = %s
                """,
                (content_id, version),
            ).fetchone()
            incoming_hash = item.get("content_hash", "")
            if existing_version and existing_version["content_hash"] != incoming_hash:
                raise ValueError(
                    "immutable content version conflict: "
                    f"{content_id}@{version} already has a different hash"
                )
            connection.execute(
                """
                INSERT INTO content_items (
                    content_id, version, content_type, target_skill,
                    target_subskill, audience, license_id, license_name,
                    source_id, review_status, body,
                    schema_version, domain, stable_version, status,
                    license_snapshot_json, source_lineage_json,
                    canonical_body_hash, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (content_id) DO UPDATE SET
                    version = EXCLUDED.version,
                    content_type = EXCLUDED.content_type,
                    target_skill = EXCLUDED.target_skill,
                    target_subskill = EXCLUDED.target_subskill,
                    audience = EXCLUDED.audience,
                    license_id = EXCLUDED.license_id,
                    license_name = EXCLUDED.license_name,
                    source_id = EXCLUDED.source_id,
                    review_status = EXCLUDED.review_status,
                    body = EXCLUDED.body,
                    schema_version = EXCLUDED.schema_version,
                    domain = EXCLUDED.domain,
                    stable_version = EXCLUDED.stable_version,
                    status = EXCLUDED.status,
                    license_snapshot_json = EXCLUDED.license_snapshot_json,
                    source_lineage_json = EXCLUDED.source_lineage_json,
                    canonical_body_hash = EXCLUDED.canonical_body_hash
                """,
                (
                    content_id,
                    version,
                    item.get("content_type", "question"),
                    item.get("target_skill", ""),
                    item.get("target_subskill", ""),
                    item.get("audience", "student"),
                    license_snapshot.get("id", ""),
                    license_snapshot.get("name", ""),
                    lineage.get("source_id", ""),
                    item.get("review_status", "approved"),
                    item.get("prompt") or item.get("body", ""),
                    item.get("schema_version", "v1"),
                    item.get("domain", "math"),
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
                INSERT INTO content_item_versions (
                    content_id, version, item_json, content_hash, created_at,
                    versioned_body, versioned_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (content_id, version) DO NOTHING
                """,
                (
                    content_id,
                    version,
                    json.dumps(item, ensure_ascii=False),
                    item.get("content_hash", ""),
                    now,
                    json.dumps(item, ensure_ascii=False),
                    now,
                ),
            )
            inserted += 1

        connection.execute(
            """
            INSERT INTO content_packs (
                pack_id, pack_version, status, manifest_json, created_at
            ) VALUES (%s, %s, 'published', %s, %s)
            ON CONFLICT (pack_id) DO UPDATE SET
                pack_version = EXCLUDED.pack_version,
                status = EXCLUDED.status,
                manifest_json = EXCLUDED.manifest_json
            """,
            (pack_id, pack_version, json.dumps(manifest, ensure_ascii=False), now),
        )
        for item in items:
            connection.execute(
                """
                INSERT INTO content_pack_items (pack_id, content_id, version)
                VALUES (%s, %s, %s)
                ON CONFLICT (pack_id, content_id) DO UPDATE SET
                    version = EXCLUDED.version
                """,
                (pack_id, item["id"], item.get("version", 1)),
            )
        return inserted


def verify_import(
    connection: psycopg.Connection, pack_id: str = "bridgesat-math"
) -> dict:
    """Return counts proving the registry, versions, and pack membership."""
    items = connection.execute(
        "SELECT COUNT(*) AS total FROM content_items"
    ).fetchone()["total"]
    versions = connection.execute(
        "SELECT COUNT(*) AS total FROM content_item_versions"
    ).fetchone()["total"]
    pack_rows = connection.execute(
        "SELECT COUNT(*) AS total FROM content_pack_items WHERE pack_id = %s",
        (pack_id,),
    ).fetchone()["total"]
    sources = connection.execute(
        "SELECT COUNT(*) AS total FROM content_sources "
        "WHERE source_id IN (%s, %s)",
        ("deepmind_mathematics_dataset", "bridgesat_original"),
    ).fetchone()["total"]
    return {
        "content_items": items,
        "content_item_versions": versions,
        "content_pack_items": pack_rows,
        "source_rows": sources,
    }
