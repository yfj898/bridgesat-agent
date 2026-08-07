"""Memory ablation smoke test: the five routes run end-to-end and the
aggregate tells the intended story (MEMORY_CONSISTENCY §10 ablation)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.infrastructure import migration_runner
from app.infrastructure.learner_store import LearnerStore
from app.memory.mnemis_stub import InMemoryMnemisIndex
from app.memory.worker import OutboxWorker
from scripts.run_memory_ablation import _aggregate, _probe, _seed_episodes

GOLDEN = Path(__file__).resolve().parents[1] / "evals" / "memory" / "golden.jsonl"


@pytest.fixture()
def env(tmp_path: Path) -> tuple[Path, InMemoryMnemisIndex, dict[str, str], list[dict]]:
    entries = [json.loads(line) for line in GOLDEN.open(encoding="utf-8") if line.strip()]
    db = tmp_path / "ablation.db"
    migration_runner.apply_migrations(db)
    learner = LearnerStore(db)
    id_map: dict[str, str] = {}
    for student_id in {e["student_id"] for e in entries}:
        actual, _ = learner.create_student(student_id, 20, 1200)
        id_map[student_id] = actual
    for entry in entries:
        _seed_episodes(db, entry["seed"], id_map[entry["student_id"]])
    stub = InMemoryMnemisIndex()
    worker = OutboxWorker(db, index=stub)
    while worker.run_pending() > 0:
        pass
    return db, stub, id_map, entries


def _run_probes(env: tuple, timeout_ms: int = 150) -> list[dict]:
    db, stub, id_map, entries = env

    async def _run_all() -> list[dict]:
        return [await _probe(db, stub, e, id_map, timeout_ms=timeout_ms) for e in entries]

    return asyncio.run(_run_all())


def test_all_routes_evaluated(env: tuple) -> None:
    results = _run_probes(env)
    for result in results:
        assert set(result["routes"]) == {
            "no_memory",
            "recent_sqlite",
            "similar_sqlite",
            "mnemis_system1",
            "mnemis_dual",
        }


def test_memory_routes_recall_every_probe(env: tuple) -> None:
    aggregate = _aggregate(_run_probes(env))
    for route in ("similar_sqlite", "mnemis_system1", "mnemis_dual"):
        assert aggregate[route]["recall_at_3"] == 1.0
        assert aggregate[route]["intervention_accuracy"] == 1.0
        assert aggregate[route]["next_action_accuracy"] == 1.0


def test_baselines_are_weak(env: tuple) -> None:
    aggregate = _aggregate(_run_probes(env))
    assert aggregate["no_memory"]["recall_at_3"] == 0.0
    assert aggregate["no_memory"]["next_action_accuracy"] == 0.0
    assert aggregate["recent_sqlite"]["recall_at_3"] < 1.0


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
