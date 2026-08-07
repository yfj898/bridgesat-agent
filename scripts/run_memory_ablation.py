#!/usr/bin/env python3
"""Memory ablation eval (COMPETITION plan section 10).

Compares five memory routes on golden probes:
  no_memory      - policy default only, no recall
  recent_sqlite  - most recent episode per skill from SQLite
  similar_sqlite - similar episodes (skill + misconception) from SQLite
  mnemis_system1 - Mnemis System-1 recall (stub backend)
  mnemis_dual    - FallbackStudentMemory: Mnemis 800 ms -> SQLite

Metrics: episode recall@3 + MRR, next-action accuracy, intervention
accuracy, fallback success, latency (avg/p95). Writes JSON to stdout and
a Markdown report to evals/memory/REPORT.md.

Usage:
    python scripts/run_memory_ablation.py [--golden evals/memory/golden.jsonl] [--out evals/memory/REPORT.md]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
import statistics
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.domain.events import LearningEvent, LearningEventType
from app.infrastructure import migration_runner
from app.infrastructure.learner_store import LearnerStore
from app.memory.episode_builder import EpisodeBuilder
from app.memory.fallback_backend import FallbackStudentMemory
from app.memory.mnemis_backend import MnemisUnavailableError
from app.memory.mnemis_stub import InMemoryMnemisIndex
from app.memory.sqlite_backend import SQLiteMemory
from app.memory.worker import OutboxWorker

DEFAULT_INTERVENTION = "SHOW_WORKED_EXAMPLE"


class _FailingSlowAdapter:
    """Mnemis adapter that exceeds the 800 ms budget and then fails."""

    async def recall_similar(self, query: dict) -> list[dict]:
        await asyncio.sleep(0.9)
        raise MnemisUnavailableError("mnemis down")

    async def health(self) -> bool:
        return False


def _event(session_id: str, event_id: str, student_id: str) -> LearningEvent:
    return LearningEvent(
        event_id=event_id,
        student_id=student_id,
        session_id=session_id,
        event_type=LearningEventType.ANSWER_EVALUATED,
        payload={},
        occurred_at="2026-08-07T10:00:00+00:00",
        received_at="2026-08-07T10:00:00+00:00",
    )


def _seed_episodes(db: Path, seed: list[dict], student_id: str) -> None:
    builder = EpisodeBuilder(db)
    for item in seed:
        session = item["session_id"]
        episode = builder.build_candidate(
            student_id=student_id,
            session_id=session,
            skill=item["skill"],
            misconception=item["misconception"],
            intervention=item["intervention"],
            context_event=_event(session, f"ctx_{item['episode_id']}", student_id),
            evidence_events=[_event(session, f"obs_{item['episode_id']}", student_id)],
            outcome_event=_event(session, f"out_{item['episode_id']}", student_id),
            outcome_correct=item["correct"],
            outcome_hint_level=0,
            outcome_content_id=f"out_{item['episode_id']}",
            teaching_content_id=f"t_{item['episode_id']}",
            summary="x",
            episode_id=item["episode_id"],
        )
        builder.validate(episode)


def _episode_id_of(hit) -> str | None:
    if hasattr(hit, "episode_id"):
        return hit.episode_id
    if isinstance(hit, dict):
        supporting = hit.get("supporting_episode_ids") or []
        return supporting[0] if supporting else None
    return hit


def _predict(db: Path, hits: list, limit: int = 5) -> dict:
    """Recommendation driven by the top-scoring similar-episode cohort."""
    builder = EpisodeBuilder(db)
    episodes: list[tuple[Episode, float]] = []
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
    db: Path,
    stub: InMemoryMnemisIndex,
    entry: dict,
    id_map: dict[str, str],
    *,
    timeout_ms: int = 800,
) -> dict:
    student_id = id_map[entry["student_id"]]
    probe = entry["probe"]
    expected_ids = set(entry["expected_episode_ids"])
    rows: list[dict] = []
    sqlite = SQLiteMemory(db)

    def record(route: str, hits: list, elapsed_ms: float) -> None:
        rows.append({"route": route, "hits": hits, "elapsed_ms": elapsed_ms})

    start = asyncio.get_running_loop().time()
    record("no_memory", [], 0.0)
    no_memory_ms = 0.0

    start = asyncio.get_running_loop().time()
    recent = sqlite.recall_episodes(student_id=student_id, skill=probe["skill"], limit=1)
    record("recent_sqlite", recent, (asyncio.get_running_loop().time() - start) * 1000)

    start = asyncio.get_running_loop().time()
    similar = sqlite.recall_episodes(
        student_id=student_id,
        skill=probe["skill"],
        misconception=probe["misconception"],
        limit=5,
    )
    record("similar_sqlite", similar, (asyncio.get_running_loop().time() - start) * 1000)

    start = asyncio.get_running_loop().time()
    results = await stub.recall_similar(
        {"student_id": student_id, "skill": probe["skill"], "misconception": probe["misconception"]},
        top_k=5,
    )
    system1_ms = (asyncio.get_running_loop().time() - start) * 1000
    record("mnemis_system1", results, system1_ms)

    fallback = FallbackStudentMemory(db, mnemis=_FailingSlowAdapter(), timeout_ms=timeout_ms)
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
        ids = [
            h.episode_id if hasattr(h, "episode_id") else h.get("supporting_episode_ids", [])[0]
            for h in row["hits"]
        ][:3]
        recall_hit = bool(set(ids) & expected_ids)
        rank = next((i for i, eid in enumerate(ids, start=1) if eid in expected_ids), None)
        pred = _predict(db, row["hits"])
        summary[route] = {
            "elapsed_ms": row["elapsed_ms"],
            "recall_at_3": recall_hit,
            "mrr": (1.0 / rank) if rank else 0.0,
            "next_action_accuracy": pred["content_id"] in entry["expected_content_ids"],
            "intervention_accuracy": pred["intervention"] == entry["expected_intervention"],
        }
    summary["mnemis_dual"]["fallback_success"] = fallback_success
    return {"probe": probe, "routes": summary, "expected": {"episode_ids": sorted(expected_ids)}}


def _aggregate(results: list[dict]) -> dict:
    routes = ["no_memory", "recent_sqlite", "similar_sqlite", "mnemis_system1", "mnemis_dual"]
    out: dict = {"probes": len(results)}
    for route in routes:
        stats = {}
        for metric in ("recall_at_3", "mrr", "next_action_accuracy", "intervention_accuracy"):
            values = [p["routes"][route][metric] for p in results]
            stats[metric] = sum(values) / len(values) if values else 0.0
        latencies = [p["routes"][route]["elapsed_ms"] for p in results]
        stats["latency_avg_ms"] = statistics.fmean(latencies) if latencies else 0.0
        stats["latency_p95_ms"] = statistics.quantiles(latencies, n=20)[18] if len(latencies) >= 20 else (max(latencies) if latencies else 0.0)
        if route == "mnemis_dual":
            stats["fallback_success"] = sum(p["routes"][route]["fallback_success"] for p in results) / len(results)
        out[route] = stats
    return out


def _markdown_report(aggregate: dict, path: Path) -> str:
    lines = [
        "# Memory ablation report",
        "",
        f"- probes: {aggregate['probes']}",
        "- routes: no-memory / recent SQLite / similar SQLite / Mnemis System-1 / Mnemis dual-route",
        "",
        "| Route | Episode recall@3 | Recall MRR | Next-action acc | Intervention acc | Fallback success | Latency avg (ms) | Latency p95 (ms) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for route in ("no_memory", "recent_sqlite", "similar_sqlite", "mnemis_system1", "mnemis_dual"):
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--golden", type=Path, default=ROOT / "evals" / "memory" / "golden.jsonl")
    parser.add_argument("--out", type=Path, default=ROOT / "evals" / "memory" / "REPORT.md")
    args = parser.parse_args()

    entries = [json.loads(line) for line in args.golden.open(encoding="utf-8") if line.strip()]
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "ablation.db"
        migration_runner.apply_migrations(db)
        stub = InMemoryMnemisIndex()
        learner = LearnerStore(db)
        id_map: dict[str, str] = {}
        for student_id in {e["student_id"] for e in entries}:
            actual, _ = learner.create_student(student_id, 20, 1200)
            id_map[student_id] = actual
        for entry in entries:
            _seed_episodes(db, entry["seed"], id_map[entry["student_id"]])
        worker = OutboxWorker(db, index=stub)
        while worker.run_pending() > 0:
            pass

        async def _run_all():
            return [await _probe(db, stub, entry, id_map) for entry in entries]
        results = asyncio.run(_run_all())

    aggregate = _aggregate(results)
    markdown = _markdown_report(aggregate, args.out)
    print(json.dumps({"summary": aggregate, "probes": results}, indent=2, default=str))
    print(f"\nMarkdown report written to {args.out}")
    print(markdown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
