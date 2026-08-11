"""Memory ablation smoke test: the five routes run end-to-end and the
aggregate tells the intended story (MEMORY_CONSISTENCY §10 ablation)."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import sys
from pathlib import Path

import psycopg
import pytest

from app.infrastructure import pg
from app.infrastructure.learner_store import LearnerStore
from app.infrastructure.migration_runner import migrate_database
from app.memory.mnemis_stub import InMemoryMnemisIndex
from app.memory.pg_memory import PGMemory
from app.memory.worker import OutboxWorker
from tests.pg_test_helpers import cleanup_tenant, unique_tenant_id

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import run_memory_ablation as ablation

_aggregate = ablation._aggregate
_probe = ablation._probe
_seed_episodes = ablation._seed_episodes

GOLDEN = Path(__file__).resolve().parents[1] / "evals" / "memory" / "golden.jsonl"


@pytest.fixture()
def env() -> tuple[
    psycopg.Connection, InMemoryMnemisIndex, dict[str, str], list[dict]
]:
    entries = [json.loads(line) for line in GOLDEN.open(encoding="utf-8") if line.strip()]
    admin = pg.connect_admin()
    try:
        migrate_database(admin)
    finally:
        try:
            admin.rollback()
        finally:
            admin.close()
    tenant_id = unique_tenant_id("task3_ablation")
    connection = None
    try:
        connection = pg.connect()
        connection.execute(
            "SELECT set_config('app.tenant_id', %s, false)",
            (tenant_id,),
        )
        connection.commit()
        learner = LearnerStore(connection)
        id_map: dict[str, str] = {}
        for student_id in {e["student_id"] for e in entries}:
            actual, _ = learner.create_student(student_id, 20, 1200)
            id_map[student_id] = actual
        namespace = ablation._tenant_namespace(tenant_id)
        for entry in entries:
            _seed_episodes(
                connection,
                entry["seed"],
                id_map[entry["student_id"]],
                namespace=namespace,
            )
        stub = InMemoryMnemisIndex()
        worker = OutboxWorker(connection, index=stub)
        while worker.run_pending() > 0:
            pass
        yield connection, stub, id_map, entries
    finally:
        if connection is not None:
            try:
                connection.rollback()
            finally:
                connection.close()
        cleanup = pg.connect_admin()
        try:
            cleanup_tenant(cleanup, tenant_id)
        finally:
             cleanup.close()


def test_ablation_runtime_is_connection_based() -> None:
    source = inspect.getsource(ablation)
    assert "SQLiteMemory" not in source
    assert "sqlite3" not in source
    assert "FallbackStudentMemory(db" not in source


def test_seed_ids_are_tenant_namespaced_but_probe_reports_golden_ids(env: tuple) -> None:
    connection, _, _, entries = env
    tenant_id = connection.execute(
        "SELECT current_setting('app.tenant_id') AS tenant"
    ).fetchone()["tenant"]
    namespace = f"ab_{hashlib.sha256(tenant_id.encode('utf-8')).hexdigest()[:16]}"

    episode_ids = {
        row["episode_id"]
        for row in connection.execute(
            "SELECT episode_id FROM learning_episodes"
        ).fetchall()
    }
    assert f"{namespace}_a_e1" in episode_ids
    assert "a_e1" not in episode_ids

    episode_rows = connection.execute(
        "SELECT session_id, evidence_event_ids_json FROM learning_episodes"
    ).fetchall()
    assert episode_rows
    assert all(namespace in row["session_id"] for row in episode_rows)
    assert all(namespace in row["evidence_event_ids_json"] for row in episode_rows)

    result = _run_probes(env)[0]
    assert result["expected"]["episode_ids"] == entries[0]["expected_episode_ids"]


def _run_probes(env: tuple, timeout_ms: int = 150) -> list[dict]:
    connection, stub, id_map, entries = env

    async def _run_all() -> list[dict]:
        return [
            await _probe(connection, stub, e, id_map, timeout_ms=timeout_ms)
            for e in entries
        ]

    return asyncio.run(_run_all())


def test_all_routes_evaluated(env: tuple) -> None:
    results = _run_probes(env)
    for result in results:
        assert set(result["routes"]) == {
            "no_memory",
            "recent_postgres",
            "similar_postgres",
            "mnemis_system1",
            "mnemis_dual",
        }


def test_memory_routes_recall_every_probe(env: tuple) -> None:
    aggregate = _aggregate(_run_probes(env))
    for route in ("similar_postgres", "mnemis_system1", "mnemis_dual"):
        assert aggregate[route]["recall_at_3"] == 1.0
        assert aggregate[route]["intervention_accuracy"] == 1.0
        assert aggregate[route]["next_action_accuracy"] == 1.0


def test_baselines_are_weak(env: tuple) -> None:
    aggregate = _aggregate(_run_probes(env))
    assert aggregate["no_memory"]["recall_at_3"] == 0.0
    assert aggregate["no_memory"]["next_action_accuracy"] == 0.0
    assert aggregate["recent_postgres"]["recall_at_3"] < 1.0


def test_dual_route_falls_back_within_budget(env: tuple) -> None:
    results = _run_probes(env, timeout_ms=150)
    for result in results:
        assert result["routes"]["mnemis_dual"]["fallback_success"] is True
        assert result["routes"]["mnemis_dual"]["elapsed_ms"] < 1000


def test_markdown_report_written(env: tuple, tmp_path: Path) -> None:
    from scripts.run_memory_ablation import _markdown_report

    aggregate = _aggregate(_run_probes(env))
    out = tmp_path / "REPORT.md"
    report = _markdown_report(aggregate, out)
    assert out.is_file()
    assert "Mnemis dual-route" in report
