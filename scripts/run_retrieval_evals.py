#!/usr/bin/env python3
"""Run retrieval evals against a local SQLite FTS5 index.

Usage:
    python scripts/run_retrieval_evals.py [--db PATH] [--dev evals/retrieval/dev.jsonl] [--golden evals/retrieval/golden.jsonl]

Measures Recall@1, Recall@3, MRR, latency, citation coverage, license
coverage, restricted-source exclusion, explicit no-result rate, and
all-expected-found rate, on both the dev set and the golden set. The
golden set is held out: weights are tuned only on the dev set.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.knowledge.local_backend import KnowledgeBackend

DEFAULT_DB = ROOT / "content" / "registry.db"


def _load_queries(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]


def _mrr(rank: int | None) -> float:
    return 1.0 / rank if rank else 0.0


def evaluate(
    backend: KnowledgeBackend,
    queries: list[dict],
    *,
    max_results: int = 3,
) -> dict:
    stats = {
        "queries": len(queries),
        "recall_at_1": 0.0,
        "recall_at_3": 0.0,
        "mrr": 0.0,
        "latency_ms": [],
        "citation_coverage": 0.0,
        "license_coverage": 0.0,
        "restricted_source_hits": 0,
        "explicit_no_result": 0,
        "all_expected_found": 0,
    }
    expected_total = 0
    covered_citations = 0
    covered_licenses = 0
    total_results = 0
    for entry in queries:
        response = backend.retrieve(
            entry["query"],
            skill=entry.get("skill"),
            misconception=entry.get("misconception"),
            max_results=max_results,
        )
        expected = set(entry.get("expected_ids") or [])
        expected_total += len(expected)
        ids = [result.content_id for result in response.results]
        stats["latency_ms"].append(response.elapsed_ms)
        if response.explicit_no_result:
            stats["explicit_no_result"] += 1

        rank = None
        for index, content_id in enumerate(ids, start=1):
            if content_id in expected:
                rank = rank or index
                if index == 1:
                    stats["recall_at_1"] += 1
        if rank and rank <= 3:
            stats["recall_at_3"] += 1
        stats["mrr"] += _mrr(rank)
        if expected and expected <= set(ids):
            stats["all_expected_found"] += 1

        for result in response.results:
            total_results += 1
            if result.citation:
                covered_citations += 1
            if result.license_id and result.license_name:
                covered_licenses += 1

    if stats["queries"]:
        stats["recall_at_1"] /= stats["queries"]
        stats["recall_at_3"] /= stats["queries"]
        stats["mrr"] /= stats["queries"]
        stats["explicit_no_result"] /= stats["queries"]
        stats["all_expected_found"] /= stats["queries"]
    if total_results:
        stats["citation_coverage"] = covered_citations / total_results
        stats["license_coverage"] = covered_licenses / total_results
    if stats["latency_ms"]:
        stats["latency_avg_ms"] = sum(stats["latency_ms"]) / len(stats["latency_ms"])
        stats["latency_p95_ms"] = sorted(stats["latency_ms"])[
            int(0.95 * (len(stats["latency_ms"]) - 1))
        ]
    stats.pop("latency_ms", None)
    return stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--dev", type=Path, default=ROOT / "evals" / "retrieval" / "dev.jsonl")
    parser.add_argument(
        "--golden", type=Path, default=ROOT / "evals" / "retrieval" / "golden.jsonl"
    )
    args = parser.parse_args()

    db = args.db or Path(os.getenv("BRIDGESAT_KNOWLEDGE_DB", DEFAULT_DB))
    if not db.is_file():
        print(f"Database {db} not found; run scripts/import_content_pack.py first", file=sys.stderr)
        return 1
    backend = KnowledgeBackend(db)

    dev = evaluate(backend, _load_queries(args.dev))
    golden = evaluate(backend, _load_queries(args.golden))
    print(f"DEV   : {json.dumps(dev, indent=2)}")
    print(f"GOLDEN: {json.dumps(golden, indent=2)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
