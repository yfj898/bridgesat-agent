#!/usr/bin/env python3
"""Memory ablation eval (COMPETITION plan section 10).

Compares five memory routes on golden probes:
  no_memory      - policy default only, no recall
  recent_postgres  - most recent episode per skill from PostgreSQL
  similar_postgres - similar episodes (skill + misconception) from PostgreSQL
  mnemis_system1 - Mnemis System-1 recall (stub backend)
  mnemis_dual    - FallbackStudentMemory: Mnemis 800 ms -> PG authority

Report route identifiers name the storage they actually exercise.

Metrics: episode recall@3 + MRR, next-action accuracy, intervention
accuracy, fallback success, latency (avg/p95). Writes JSON to stdout and
a Markdown report to evals/memory/REPORT.md.

Usage:
    python scripts/run_memory_ablation.py [--db DSN] [--admin-db DSN]
        [--tenant LABEL]
        [--golden evals/memory/golden.jsonl] [--out evals/memory/REPORT.md]
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import contextmanager
import hashlib
import json
import statistics
import sys
import uuid
from pathlib import Path
from typing import Iterator

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.infrastructure import pg
from app.infrastructure.learner_store import LearnerStore
from app.infrastructure.migration_runner import migrate_database
from app.memory.episode_builder import EpisodeBuilder
from app.memory.fallback_backend import FallbackStudentMemory
from app.memory.mnemis_backend import MnemisUnavailableError
from app.memory.mnemis_stub import InMemoryMnemisIndex
from app.memory.pg_memory import PGMemory
from app.memory.worker import OutboxWorker

DEFAULT_INTERVENTION = "SHOW_WORKED_EXAMPLE"
ROUTES = (
    "no_memory",
    "recent_postgres",
    "similar_postgres",
    "mnemis_system1",
    "mnemis_dual",
)

TENANT_CLEANUP_ORDER = (
    "memory_outbox",
    "student_deletions",
    "student_memory_facts",
    "learning_episodes",
    "intervention_stats",
    "agent_events",
    "misconception_evidence",
    "answer_attempts",
    "session_items",
    "study_sessions",
    "study_plans",
    "student_skill_states",
    "learning_events",
    "session_branches",
    "sync_conflicts",
    "devices",
    "student_tokens",
    "students",
)


class _FailingSlowAdapter:
    """Mnemis adapter that exceeds the 800 ms budget and then fails."""

    async def recall_similar(self, query: dict) -> list[dict]:
        await asyncio.sleep(0.9)
        raise MnemisUnavailableError("mnemis down")

    async def health(self) -> bool:
        return False


def _migrate(admin_target: str, app_target: str) -> None:
    admin = None
    app = None
    try:
        admin = pg.connect_admin(admin_target)
        app = pg.connect(app_target)
        pg.assert_safe_app_role(app)
        pg.assert_matching_database(admin, app)
        migrate_database(admin)
    finally:
        pg.quiet_close(app)
        pg.quiet_close(admin)


@contextmanager
def _tenant_connection(target: str, tenant_id: str) -> Iterator[psycopg.Connection]:
    connection = pg.connect(target)
    try:
        pg.assert_safe_app_role(connection)
        connection.execute(
            "SELECT set_config('app.tenant_id', %s, false)",
            (tenant_id,),
        )
        connection.commit()
        yield connection
    finally:
        pg.quiet_close(connection)


def _cleanup_tenant(admin_target: str, app_target: str, tenant_id: str) -> None:
    admin = None
    app = None
    try:
        admin = pg.connect_admin(admin_target)
        app = pg.connect(app_target)
        pg.assert_safe_app_role(app)
        pg.assert_matching_database(admin, app)
        app.execute(
            "SELECT set_config('app.tenant_id', %s, false)",
            (tenant_id,),
        )
        app.commit()
        for table in TENANT_CLEANUP_ORDER:
            admin.execute(f"DELETE FROM {table} WHERE tenant_id = %s", (tenant_id,))
        admin.commit()
    finally:
        pg.quiet_close(app)
        pg.quiet_close(admin)


def _tenant_namespace(tenant_id: str) -> str:
    return f"ab_{hashlib.sha256(tenant_id.encode('utf-8')).hexdigest()[:16]}"


def _connection_namespace(connection: psycopg.Connection) -> str:
    row = connection.execute(
        "SELECT current_setting('app.tenant_id', false) AS tenant"
    ).fetchone()
    if row is None or not row["tenant"]:
        raise RuntimeError("ablation connection has no app.tenant_id")
    return _tenant_namespace(row["tenant"])


def _namespaced_id(namespace: str, identifier: str) -> str:
    return f"{namespace}_{identifier}"


def _new_tenant(label: str) -> str:
    return f"{label}_{uuid.uuid4().hex}"


def _event(
    session_id: str,
    event_id: str,
    student_id: str,
    *,
    attempt_id: str,
):
    from app.domain.events import LearningEvent, LearningEventType

    return LearningEvent(
        event_id=event_id,
        student_id=student_id,
        session_id=session_id,
        event_type=LearningEventType.ANSWER_EVALUATED,
        payload={"attempt_id": attempt_id},
        occurred_at="2026-08-07T10:00:00+00:00",
        received_at="2026-08-07T10:00:00+00:00",
    )


def _seed_episodes(
    connection: psycopg.Connection,
    seed: list[dict],
    student_id: str,
    *,
    namespace: str | None = None,
) -> None:
    namespace = namespace or _connection_namespace(connection)
    builder = EpisodeBuilder(connection)
    for item in seed:
        base_episode_id = item["episode_id"]
        episode_id = _namespaced_id(namespace, base_episode_id)
        session = _namespaced_id(namespace, item["session_id"])
        episode = builder.build_candidate(
            student_id=student_id,
            session_id=session,
            skill=item["skill"],
            misconception=item["misconception"],
            intervention=item["intervention"],
            context_event=_event(
                session,
                _namespaced_id(namespace, f"ctx_{base_episode_id}"),
                student_id,
                attempt_id=_namespaced_id(namespace, f"attempt_ctx_{base_episode_id}"),
            ),
            evidence_events=[
                _event(
                    session,
                    _namespaced_id(namespace, f"obs_{base_episode_id}"),
                    student_id,
                    attempt_id=_namespaced_id(namespace, f"attempt_obs_{base_episode_id}"),
                )
            ],
            outcome_event=_event(
                session,
                _namespaced_id(namespace, f"out_{base_episode_id}"),
                student_id,
                attempt_id=_namespaced_id(namespace, f"attempt_out_{base_episode_id}"),
            ),
            outcome_correct=item["correct"],
            outcome_hint_level=0,
            outcome_content_id=f"out_{base_episode_id}",
            teaching_content_id=f"t_{base_episode_id}",
            summary="x",
            episode_id=episode_id,
        )
        builder.validate(episode)


def _episode_id_of(hit) -> str | None:
    if hasattr(hit, "episode_id"):
        return hit.episode_id
    if isinstance(hit, dict):
        supporting = hit.get("supporting_episode_ids") or []
        return supporting[0] if supporting else None
    return hit


def _predict(connection: psycopg.Connection, hits: list, limit: int = 5) -> dict:
    """Recommendation driven by the top-scoring similar-episode cohort."""
    builder = EpisodeBuilder(connection)
    episodes = []
    for hit in hits[:limit]:
        episode_id = _episode_id_of(hit)
        if episode_id is None:
            continue
        episode = builder.get_episode(episode_id)
        weight = hit.get("retrieval_score", 1.0) if isinstance(hit, dict) else 1.0
        if episode is not None and episode.status == "validated":
            episodes.append((episode, weight))
    if not episodes:
        return {"intervention": DEFAULT_INTERVENTION, "content_id": None}
    top_score = max(w for _, w in episodes)
    cohort = [e for e, w in episodes if w == top_score]
    votes = Counter(e.intervention for e in cohort)
    intervention = votes.most_common(1)[0][0]
    best = max(cohort, key=lambda e: (e.confidence, e.created_at))
    return {
        "intervention": intervention,
        "content_id": best.outcome.get("outcome_content_id"),
    }


async def _probe(
    connection: psycopg.Connection,
    stub: InMemoryMnemisIndex,
    entry: dict,
    id_map: dict[str, str],
    *,
    timeout_ms: int = 800,
    namespace: str | None = None,
) -> dict:
    student_id = id_map[entry["student_id"]]
    probe = entry["probe"]
    namespace = namespace or _connection_namespace(connection)
    expected_golden_ids = set(entry["expected_episode_ids"])
    expected_ids = {
        _namespaced_id(namespace, episode_id)
        for episode_id in expected_golden_ids
    }
    rows: list[dict] = []
    pg_memory = PGMemory(connection)

    def record(route: str, hits: list, elapsed_ms: float) -> None:
        rows.append({"route": route, "hits": hits, "elapsed_ms": elapsed_ms})

    record("no_memory", [], 0.0)

    start = asyncio.get_running_loop().time()
    recent = pg_memory.recall_episodes(
        student_id=student_id,
        skill=probe["skill"],
        limit=1,
    )
    record("recent_postgres", recent, (asyncio.get_running_loop().time() - start) * 1000)

    start = asyncio.get_running_loop().time()
    similar = pg_memory.recall_episodes(
        student_id=student_id,
        skill=probe["skill"],
        misconception=probe["misconception"],
        limit=5,
    )
    record("similar_postgres", similar, (asyncio.get_running_loop().time() - start) * 1000)

    start = asyncio.get_running_loop().time()
    results = await stub.recall_similar(
        {"student_id": student_id, "skill": probe["skill"],
         "misconception": probe["misconception"]},
        top_k=5,
    )
    system1_ms = (asyncio.get_running_loop().time() - start) * 1000
    record("mnemis_system1", results, system1_ms)

    fallback = FallbackStudentMemory(
        connection,
        mnemis=_FailingSlowAdapter(),
        timeout_ms=timeout_ms,
    )
    start = asyncio.get_running_loop().time()
    result = await fallback.recall_similar(
        student_id=student_id,
        skill=probe["skill"],
        misconception=probe["misconception"],
        limit=5,
    )
    dual_ms = (asyncio.get_running_loop().time() - start) * 1000
    record("mnemis_dual", result.hits, dual_ms)
    fallback_success = result.route != "mnemis_system1"

    summary: dict = {}
    for row in rows:
        route = row["route"]
        ids = []
        for hit in row["hits"]:
            episode_id = _episode_id_of(hit)
            if episode_id is not None:
                ids.append(episode_id)
        ids = ids[:3]
        recall_hit = bool(set(ids) & expected_ids)
        rank = next((i for i, eid in enumerate(ids, start=1) if eid in expected_ids), None)
        pred = _predict(connection, row["hits"])
        summary[route] = {
            "elapsed_ms": row["elapsed_ms"],
            "recall_at_3": recall_hit,
            "mrr": (1.0 / rank) if rank else 0.0,
            "next_action_accuracy": pred["content_id"] in entry["expected_content_ids"],
            "intervention_accuracy": pred["intervention"] == entry["expected_intervention"],
        }
    summary["mnemis_dual"]["fallback_success"] = fallback_success
    return {
        "probe": probe,
        "routes": summary,
        # Reports retain the golden identifiers; only the internal recall
        # comparison uses tenant-namespaced authoritative IDs.
        "expected": {"episode_ids": sorted(expected_golden_ids)},
    }


def _aggregate(results: list[dict]) -> dict:
    out: dict = {"probes": len(results)}
    for route in ROUTES:
        stats = {}
        for metric in ("recall_at_3", "mrr", "next_action_accuracy", "intervention_accuracy"):
            values = [p["routes"][route][metric] for p in results]
            stats[metric] = sum(values) / len(values) if values else 0.0
        latencies = [p["routes"][route]["elapsed_ms"] for p in results]
        stats["latency_avg_ms"] = sum(latencies) / len(latencies) if latencies else 0.0
        stats["latency_p95_ms"] = (
            statistics.quantiles(latencies, n=20)[18]
            if len(latencies) >= 20
            else (max(latencies) if latencies else 0.0)
        )
        if route == "mnemis_dual":
            stats["fallback_success"] = (
                sum(p["routes"][route]["fallback_success"] for p in results) / len(results)
                if results else 0.0
            )
        out[route] = stats
    return out


def _markdown_report(aggregate: dict, path: Path) -> str:
    lines = [
        "# Memory ablation report",
        "",
        f"- probes: {aggregate['probes']}",
        f"- tenant_id: {aggregate.get('tenant_id', 'unknown')}",
        "- routes: no-memory / recent PG / similar PG / Mnemis System-1 / Mnemis dual-route",
        "",
        "| Route | Episode recall@3 | Recall MRR | Next-action acc | Intervention acc | Fallback success | Latency avg (ms) | Latency p95 (ms) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for route in ROUTES:
        s = aggregate[route]
        fallback = f"{s.get('fallback_success', 0):.2f}" if "fallback_success" in s else "-"
        lines.append(
            f"| {route} | {s['recall_at_3']:.2f} | {s['mrr']:.2f} | "
            f"{s['next_action_accuracy']:.2f} | {s['intervention_accuracy']:.2f} | "
            f"{fallback} | {s['latency_avg_ms']:.1f} | {s['latency_p95_ms']:.1f} |"
        )
    lines.append("")
    report = "\n".join(lines)
    path.write_text(report, encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None, help="PostgreSQL application DSN")
    parser.add_argument("--admin-db", default=None, help="PostgreSQL admin DSN")
    parser.add_argument("--tenant", default=None, help="label for an isolated ablation tenant")
    parser.add_argument("--golden", type=Path, default=ROOT / "evals" / "memory" / "golden.jsonl")
    parser.add_argument("--out", type=Path, default=ROOT / "evals" / "memory" / "REPORT.md")
    args = parser.parse_args(argv)

    entries = [json.loads(line) for line in args.golden.open(encoding="utf-8") if line.strip()]
    if not entries:
        parser.error("--golden must contain at least one probe")
    target = args.db or pg.dsn()
    admin_target = args.admin_db or pg.admin_dsn()
    tenant_id = _new_tenant(args.tenant or "ablation")
    namespace = _tenant_namespace(tenant_id)
    migrated = False
    try:
        _migrate(admin_target, target)
        migrated = True
        with _tenant_connection(target, tenant_id) as connection:
            stub = InMemoryMnemisIndex()
            learner = LearnerStore(connection)
            id_map: dict[str, str] = {}
            for student_id in {e["student_id"] for e in entries}:
                actual, _ = learner.create_student(student_id, 20, 1200)
                id_map[student_id] = actual
            for entry in entries:
                _seed_episodes(
                    connection,
                    entry["seed"],
                    id_map[entry["student_id"]],
                    namespace=namespace,
                )
            worker = OutboxWorker(connection, index=stub)
            while worker.run_pending() > 0:
                pass

            async def _run_all():
                return [
                    await _probe(
                        connection,
                        stub,
                        entry,
                        id_map,
                        namespace=namespace,
                    )
                    for entry in entries
                ]

            results = asyncio.run(_run_all())

        aggregate = _aggregate(results)
        aggregate["tenant_id"] = tenant_id
        args.out.parent.mkdir(parents=True, exist_ok=True)
        markdown = _markdown_report(aggregate, args.out)
        print(json.dumps({"summary": aggregate, "probes": results}, indent=2, default=str))
        print(f"\nMarkdown report written to {args.out}")
        print(markdown)
        return 0
    finally:
        if migrated:
            _cleanup_tenant(admin_target, target, tenant_id)


if __name__ == "__main__":
    sys.exit(main())
