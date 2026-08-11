"""PG content registry importer tests: isolated database, idempotence, and
the CLI accepting DSNs instead of a SQLite path."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

from app.content_pipeline.contracts import SCHEMA_VERSION as PACK_SCHEMA_VERSION
from app.content_pipeline.contracts import content_hash
from app.content_pipeline.importing import import_pack, verify_import
from app.content_pipeline.packaging import PACK_VERSION, build_pack
from app.infrastructure import pg
from app.infrastructure.migration_runner import migrate_database

ROOT = Path(__file__).resolve().parents[1]


def _minimal_approved_item(content_id: str, skill: str) -> dict:
    item = {
        "id": content_id,
        "version": 1,
        "schema_version": PACK_SCHEMA_VERSION,
        "domain": "math",
        "content_type": "question",
        "target_skill": skill,
        "target_subskill": "isolate_variables",
        "required_prerequisites": ["integer_operations"],
        "difficulty": 1,
        "prompt": "If 2x + 3 = 7, what is x?",
        "choices": [
            {"id": "A", "text": "2"},
            {"id": "B", "text": "-2"},
            {"id": "C", "text": "8"},
            {"id": "D", "text": "3"},
        ],
        "answer_choice_id": "A",
        "misconception_map": {"B": "sign_error", "C": "inverse_operation_error", "D": "arithmetic_error"},
        "hints": [
            {"level": 1, "text": "Subtract 3."},
            {"level": 2, "text": "Divide by 2."},
            {"level": 3, "text": "x = 2."},
        ],
        "worked_explanation": "2x = 4, x = 2.",
        "estimated_seconds": 60,
        "source_lineage": {"source_id": "deepmind_mathematics_dataset", "lineage_id": "x", "role": "concept_source_only"},
        "license": {"id": "bridgesat_original", "name": "BridgeSAT original"},
        "review_status": "approved",
        "reviewers": {r: r for r in ("educational", "answer", "license", "accessibility")},
        "release_batch": "b1",
        "content_hash": "",
        "author_metadata": {"kind": "expression", "expression": "2*2 + 3", "expected": "7"},
    }
    item["content_hash"] = content_hash(item)
    return item


def _minimal_approved_lesson() -> dict:
    lesson = {
        "id": "math.linear_equations.worked_example.001",
        "version": 2,
        "schema_version": PACK_SCHEMA_VERSION,
        "domain": "math",
        "content_type": "worked_example",
        "target_skill": "linear_equations",
        "target_subskill": "isolate_variables",
        "target_misconceptions": ["sign_error"],
        "required_prerequisites": ["integer_operations"],
        "difficulty": 1,
        "title": "Sign check",
        "body": "Apply the same inverse operation to both sides and check the sign.",
        "estimated_seconds": 90,
        "source_lineage": {
            "source_id": "bridgesat_original",
            "lineage_id": "bridgesat-original:test-lesson",
            "role": "concept_source_only",
        },
        "license": {"id": "bridgesat_original", "name": "BridgeSAT original"},
        "review_status": "approved",
        "reviewers": {r: r for r in ("educational", "answer", "license", "accessibility")},
        "release_batch": "b1",
        "content_hash": "",
    }
    lesson["content_hash"] = content_hash(lesson)
    return lesson


@pytest.fixture()
def built_pack(tmp_path: Path) -> Path:
    item = _minimal_approved_item("math.linear_equations.003", "linear_equations")
    build_pack([item], [_minimal_approved_lesson()], out_dir=tmp_path / "packs")
    return tmp_path / "packs" / f"bridgesat-math-{PACK_VERSION}"


def test_pg_import_pack_writes_registry_and_is_idempotent(
    isolated_pg_database, built_pack: Path
) -> None:
    admin = pg.connect_admin(isolated_pg_database.admin_dsn)
    try:
        migrate_database(admin)
        imported = import_pack(admin, built_pack)
        assert imported == 2
        summary = verify_import(admin, pack_id="bridgesat-math")
        assert summary == {
            "content_items": 2,
            "content_item_versions": 2,
            "content_pack_items": 2,
            "source_rows": 2,
        }

        again = import_pack(admin, built_pack)
        assert again == 2
        assert verify_import(admin, pack_id="bridgesat-math") == summary
    finally:
        admin.close()


def test_pg_import_rejects_changed_body_for_existing_content_version(
    isolated_pg_database, built_pack: Path, tmp_path: Path
) -> None:
    admin = pg.connect_admin(isolated_pg_database.admin_dsn)
    try:
        migrate_database(admin)
        import_pack(admin, built_pack)

        changed = _minimal_approved_item(
            "math.linear_equations.003", "linear_equations"
        )
        changed["prompt"] = "A changed prompt must use a new version."
        changed["content_hash"] = content_hash(changed)
        changed_pack_root = tmp_path / "changed-packs"
        build_pack([changed], [], out_dir=changed_pack_root)

        with pytest.raises(ValueError, match="immutable content version conflict"):
            import_pack(
                admin,
                changed_pack_root / f"bridgesat-math-{PACK_VERSION}",
            )
    finally:
        admin.close()


def test_pg_import_pack_records_governance_metadata(
    isolated_pg_database, built_pack: Path
) -> None:
    admin = pg.connect_admin(isolated_pg_database.admin_dsn)
    try:
        migrate_database(admin)
        import_pack(admin, built_pack)

        source = admin.execute(
            "SELECT source_name, source_type, redistribution_allowed, "
            "rag_ingestion_allowed, access_method, attribution, "
            "maintenance_status, last_verified_at "
            "FROM content_sources WHERE source_id = %s",
            ("deepmind_mathematics_dataset",),
        ).fetchone()
        assert source is not None
        assert source["source_name"] == "DeepMind Mathematics Dataset (concept source only)"
        assert source["maintenance_status"] == "candidate_generation_only"

        item = admin.execute(
            "SELECT schema_version, domain, stable_version, status, "
            "license_snapshot_json, source_lineage_json, canonical_body_hash, "
            "created_at, withdrawn_at, withdrawn_reason "
            "FROM content_items WHERE content_id = %s",
            ("math.linear_equations.003",),
        ).fetchone()
        assert item is not None
        assert item["status"] == "approved"
        assert item["withdrawn_at"] is None

        version = admin.execute(
            "SELECT item_json, content_hash, created_at "
            "FROM content_item_versions WHERE content_id = %s",
            ("math.linear_equations.003",),
        ).fetchone()
        assert version is not None
        assert "prompt" in version["item_json"]

        pack = admin.execute(
            "SELECT status, manifest_json FROM content_packs WHERE pack_id = %s",
            ("bridgesat-math",),
        ).fetchone()
        assert pack["status"] == "published"
        assert "item_hashes" in pack["manifest_json"]

        membership = admin.execute(
            "SELECT version FROM content_pack_items "
            "WHERE pack_id = %s AND content_id = %s",
            ("bridgesat-math", "math.linear_equations.003"),
        ).fetchone()
        assert membership["version"] == 1
        lesson = admin.execute(
            "SELECT content_type, body FROM content_items WHERE content_id = %s",
            ("math.linear_equations.worked_example.001",),
        ).fetchone()
        assert lesson["content_type"] == "worked_example"
        assert lesson["body"].startswith("Apply the same inverse operation")
    finally:
        admin.close()


def test_pg_import_cli_accepts_dsn_and_avoids_sqlite_artifacts(
    isolated_pg_database, built_pack: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRIDGESAT_ADMIN_DB", isolated_pg_database.admin_dsn)
    monkeypatch.setenv("BRIDGESAT_DB", isolated_pg_database.app_dsn)
    monkeypatch.setenv("BRIDGESAT_REGISTRY_DB", "/nonexistent/must/not/be/used.db")
    env = dict(os.environ)
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/import_content_pack.py"),
            "--admin-db",
            isolated_pg_database.admin_dsn,
            "--db",
            isolated_pg_database.app_dsn,
            "--pack",
            str(built_pack),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    stderr = proc.stderr
    assert "PosixPath" not in stderr
    assert "object has no attribute 'encode'" not in stderr
    assert "INSERT OR REPLACE" not in stderr

    app = isolated_pg_database.connect_app()
    try:
        app.execute("SELECT set_config('app.tenant_id', %s, false)", (isolated_pg_database.tenant_id,))
        app.commit()
        row = app.execute(
            "SELECT COUNT(*) AS total FROM content_items"
        ).fetchone()
        assert row["total"] == 2
    finally:
        app.close()


def test_app_role_cannot_publish_shared_content(
    isolated_pg_database,
) -> None:
    """The app role may read shared content but never publish it."""
    admin = pg.connect_admin(isolated_pg_database.admin_dsn)
    try:
        migrate_database(admin)
        app = isolated_pg_database.connect_app()
        try:
            app.execute("SELECT set_config('app.tenant_id', %s, false)", (isolated_pg_database.tenant_id,))
            app.commit()
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                app.execute(
                    "INSERT INTO content_items (content_id, content_type) "
                    "VALUES (%s, %s)",
                    ("math.illegal.001", "question"),
                )
            app.rollback()
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                app.execute(
                    "INSERT INTO knowledge_fts (content_id, content_type, body) "
                    "VALUES (%s, %s, %s)",
                    ("math.illegal.001", "question", "body"),
                )
            app.rollback()
            select = app.execute(
                "SELECT COUNT(*) AS total FROM content_items"
            ).fetchone()
            assert select["total"] == 0
        finally:
            app.close()
    finally:
        admin.close()
