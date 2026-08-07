#!/usr/bin/env python3
"""Performance gates eval (EVALUATION_SPEC section 11).

Measures on-device latency budgets on this machine:

- local policy p95 < 150 ms   (decide_next_action, 2000 varied calls)
- FTS5 p95 < 200 ms           (KnowledgeBackend.retrieve, dev+golden queries)
- session restore p95 < 500 ms (SyncService.build_snapshot, seeded student)
- sync throughput             (process_batch, informational)
- memory footprint            (max RSS, informational)
- Mnemis timeout non-blocking (covered by test suite, reported)

Writes reports/performance_eval.json.

Usage:
    python scripts/run_performance_evals.py
"""

from __future__ import annotations

import argparse
import json
import resource
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.policy import decide_next_action, PolicyInput
from app.domain.sessions import SessionState
from app.domain.events import LearningEventType
from app.infrastructure.migration_runner import apply_migrations
from app.knowledge.local_backend import KnowledgeBackend
from app.infrastructure.learner_store import LearnerStore
from app.question_bank import packs_root
from app.sync.protocol import SyncEventEnvelope, SyncRequest
from app.sync.service import SyncService

REPORT_JSON = ROOT / "reports" / "performance_eval.json"

PACK_VERSION = "0.1.0"
KNOWLEDGE_DB = ROOT / "content" / "registry.db"

TARGETS = {
    "local_policy_p95_ms": 150.0,
    "fts5_p95_ms": 200.0,
    "session_restore_p95_ms": 500.0,
}


def _percentiles(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    n = len(ordered)

    def pct(p: float) -> float:
        if n == 1:
            return ordered[0]
        index = (n - 1) * p
        lower = int(index)
        frac = index - lower
        if lower + 1 < n:
            return ordered[lower] + frac * (ordered[lower + 1] - ordered[lower])
        return ordered[lower]

    return {
        "samples": n,
        "p50_ms": round(pct(0.5), 2),
        "p95_ms": round(pct(0.95), 2),
        "max_ms": round(ordered[-1], 2),
    }


def bench_local_policy(n: int = 2000) -> dict:
    cases: list[PolicyInput] = []
    for i in range(n):
        cases.append(
            PolicyInput(
                student_id="perf-student",
                session_id="perf-session",
                skill=["linear_equations", "ratios_percentages", "functions_models",
                       "systems_equations"][i % 4],
                difficulty=(i % 3) + 1,
                mastery=round((i % 10) / 10, 1),
                consecutive_errors=i % 4,
                correct_streak=i % 6,
                repeated_misconception=i % 5 == 0,
                active_misconception="sign_error" if i % 5 == 0 else None,
                misconception_observation_count=i % 3,
                misconception_distinct_items=i % 2,
                requires_unmastered_prerequisite=i % 7 == 0,
                minutes_remaining=[1, 2, 5, 12, 20][i % 5],
                hints_used_this_item=i % 4,
                state=SessionState.ANSWER_EVALUATED,
                recalled_successful_episode=i % 9 == 0,
                recalled_episode_ids=["e1", "e2"] if i % 9 == 0 else [],
                recent_correct_without_high_hint=i % 8,
                recent_total=10 + (i % 10),
            )
        )
    samples: list[float] = []
    for case in cases:
        started = time.perf_counter()
        decide_next_action(case)
        samples.append((time.perf_counter() - started) * 1000)
    return _percentiles(samples)


def bench_fts5(n: int = 200) -> dict:
    queries = [
        {"query": "solve linear equation isolate variable", "skill": "linear_equations"},
        {"query": "unit rate ratio parts per minute", "skill": "ratios_percentages"},
        {"query": "linear model taxi fare", "skill": "functions_models"},
        {"query": "system of two equations solve", "skill": "systems_equations"},
        {"query": "quadratic equation roots", "skill": "quadratic_equations"},
        {"query": "integer operations negative numbers", "skill": "integer_operations"},
    ]
    samples: list[float] = []
    backend = KnowledgeBackend(KNOWLEDGE_DB)
    for i in range(n):
        entry = queries[i % len(queries)]
        started = time.perf_counter()
        backend.retrieve(entry["query"], skill=entry["skill"])
        samples.append((time.perf_counter() - started) * 1000)
    return _percentiles(samples)


def _integrity(event_type: str, payload: dict) -> str:
    import hashlib

    digest = hashlib.sha256()
    digest.update(event_type.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    return f"sha256:{digest.hexdigest()}"


def _seed_snapshot_student(db: Path) -> str:
    from app.question_bank import packs_root

    items_path = packs_root() / f"bridgesat-math-{PACK_VERSION}" / "items.jsonl"
    items = [json.loads(line) for line in items_path.open(encoding="utf-8") if line.strip()]
    by_skill: dict[str, list[dict]] = {}
    for item in items:
        by_skill.setdefault(item["target_skill"], []).append(item)

    sync = SyncService(db)
    learner = LearnerStore(db)
    student_id, _ = learner.create_student("Perf Student", 20, 1200)
    device_id = "perf_device"
    sync.register_device(student_id, "perf laptop", device_id=device_id)

    now = "2026-08-07T12:00:00+08:00"
    envelopes: list[SyncEventEnvelope] = []
    sequence = 1

    for session_no in range(1, 13):
        session_id = f"perf_session_{session_no:02d}"
        previous: list[str] = []
        picks = [
            (by_skill["linear_equations"][session_no % 5], True),
            (by_skill["ratios_percentages"][session_no % 5], False),
            (by_skill["functions_models"][session_no % 4], True),
            (by_skill["systems_equations"][session_no % 4], True),
        ]
        for item, correct in picks:
            presented_event_id = f"perf_{session_id}_p_{sequence}"
            envelopes.append(SyncEventEnvelope(
                event_id=presented_event_id,
                student_id=student_id,
                session_id=session_id,
                session_branch_id="branch_perf_device",
                device_id=device_id,
                device_sequence=sequence,
                event_type="CONTENT_PRESENTED",
                payload={"question_id": item["id"]},
                content_pack_version=PACK_VERSION,
                question_id=item["id"],
                question_version=item["version"],
                policy_version="offline-policy-v1",
                depends_on_event_ids=previous,
                device_occurred_at=now,
                integrity_hash=_integrity("CONTENT_PRESENTED", {"question_id": item["id"]}),
            ))
            sequence += 1
            selected = item["answer_choice_id"] if correct else next(
                c["id"] for c in item["choices"] if c["id"] != item["answer_choice_id"]
            )
            answer_payload = {
                "question_id": item["id"],
                "question_version": 1,
                "selected_choice_id": selected,
                "hint_level": 0,
                "attempt_id": f"perf_att_{item['id']}_{session_no}",
            }
            envelopes.append(SyncEventEnvelope(
                event_id=f"perf_{session_id}_a_{sequence}",
                student_id=student_id,
                session_id=session_id,
                session_branch_id="branch_perf_device",
                device_id=device_id,
                device_sequence=sequence,
                event_type="ANSWER_SUBMITTED",
                payload=answer_payload,
                content_pack_version=PACK_VERSION,
                question_id=item["id"],
                question_version=item["version"],
                policy_version="offline-policy-v1",
                depends_on_event_ids=[presented_event_id],
                device_occurred_at=now,
                integrity_hash=_integrity("ANSWER_SUBMITTED", answer_payload),
            ))
            sequence += 1
            previous = [envelopes[-1].event_id]
        session_payload = {"summary": f"perf session {session_no}"}
        envelopes.append(SyncEventEnvelope(
            event_id=f"perf_{session_id}_s_{sequence}",
            student_id=student_id,
            session_id=session_id,
            session_branch_id="branch_perf_device",
            device_id=device_id,
            device_sequence=sequence,
            event_type="SESSION_COMPLETED",
            payload=session_payload,
            content_pack_version=PACK_VERSION,
            policy_version="offline-policy-v1",
            depends_on_event_ids=previous,
            device_occurred_at=now,
            integrity_hash=_integrity("SESSION_COMPLETED", session_payload),
        ))
        sequence += 1

    for start in range(0, len(envelopes), 100):
        response = sync.process_batch(SyncRequest(
            device_id=device_id,
            student_id=student_id,
            events=envelopes[start:start + 100],
        ))
        if response.rejected_events:
            raise RuntimeError(
                f"perf seeding rejected: {[r.code for r in response.rejected_events]}"
            )
    return student_id


def bench_session_restore(n: int = 100) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "perf.db"
        apply_migrations(db)
        student_id = _seed_snapshot_student(db)
        sync = SyncService(db)
        sync.build_snapshot(student_id)  # warm
        samples: list[float] = []
        for _ in range(n):
            started = time.perf_counter()
            sync.build_snapshot(student_id)
            samples.append((time.perf_counter() - started) * 1000)
        return _percentiles(samples)


def bench_sync_throughput(db: Path, student_id: str, device_id: str) -> float:
    sync = SyncService(db)
    items = [
        json.loads(line)
        for line in (packs_root() / f"bridgesat-math-{PACK_VERSION}" / "items.jsonl")
        .open(encoding="utf-8").read().splitlines()
        if line.strip()
    ]
    now = "2026-08-07T13:00:00+08:00"
    envelopes: list[SyncEventEnvelope] = []
    for i, item in enumerate(items[:20]):
        for _ in range(5):
            payload = {
                "question_id": item["id"],
                "question_version": 1,
                "selected_choice_id": item["answer_choice_id"],
                "hint_level": 0,
                "attempt_id": f"perf_tput_att_{i}_{len(envelopes)}",
            }
            envelopes.append(SyncEventEnvelope(
                event_id=f"perf_tput_{i}_{len(envelopes)}",
                student_id=student_id,
                session_id="perf_tput_session",
                session_branch_id="branch_perf_device",
                device_id=device_id,
                device_sequence=len(envelopes) + 1,
                event_type="ANSWER_SUBMITTED",
                payload=payload,
                content_pack_version=PACK_VERSION,
                question_id=item["id"],
                question_version=item["version"],
                policy_version="offline-policy-v1",
                depends_on_event_ids=[],
                device_occurred_at=now,
                integrity_hash=_integrity("ANSWER_SUBMITTED", payload),
            ))
    request = SyncRequest(device_id=device_id, student_id=student_id, events=envelopes)
    started = time.perf_counter()
    response = sync.process_batch(request)
    elapsed = time.perf_counter() - started
    if response.rejected_events:
        raise RuntimeError(f"throughput batch rejected: {[r.code for r in response.rejected_events]}")
    return len(envelopes) / elapsed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", type=Path, default=REPORT_JSON)
    parser.add_argument("--samples-policy", type=int, default=2000)
    parser.add_argument("--samples-fts5", type=int, default=200)
    parser.add_argument("--samples-restore", type=int, default=100)
    args = parser.parse_args()

    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    policy = bench_local_policy(args.samples_policy)
    fts5 = bench_fts5(args.samples_fts5)
    restore = bench_session_restore(args.samples_restore)

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "tput.db"
        apply_migrations(db)
        sync = SyncService(db)
        learner = LearnerStore(db)
        student_id, _ = learner.create_student("Tput Student", 20, 1200)
        sync.register_device(student_id, "perf laptop", device_id="perf_device")
        throughput = bench_sync_throughput(db, student_id, "perf_device")

    rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    results = {
        "local_policy": {**policy,
                         "target_p95_ms": TARGETS["local_policy_p95_ms"],
                         "passed": policy["p95_ms"] < TARGETS["local_policy_p95_ms"]},
        "fts5": {**fts5,
                 "target_p95_ms": TARGETS["fts5_p95_ms"],
                 "passed": fts5["p95_ms"] < TARGETS["fts5_p95_ms"]},
        "session_restore": {**restore,
                            "target_p95_ms": TARGETS["session_restore_p95_ms"],
                            "passed": restore["p95_ms"] < TARGETS["session_restore_p95_ms"]},
    }
    passed = all(v["passed"] for v in results.values())

    summary = {
        "schema_version": "1.0",
        "label": "controlled internal test (on-device latency, this machine)",
        "results": results,
        "sync_throughput_events_per_sec": round(throughput, 1),
        "max_rss_mb": round(rss_after / 1024, 1),
        "mnemis_timeout_nonblocking": "covered by tests: tests/test_memory*",
        "targets": TARGETS,
        "all_gates_passed": passed,
    }

    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({
        "label": summary["label"],
        "all_gates_passed": passed,
        "local_policy_p95_ms": results["local_policy"]["p95_ms"],
        "fts5_p95_ms": results["fts5"]["p95_ms"],
        "session_restore_p95_ms": results["session_restore"]["p95_ms"],
        "sync_throughput_events_per_sec": summary["sync_throughput_events_per_sec"],
        "max_rss_mb": summary["max_rss_mb"],
    }, indent=2))
    return 0 if passed else 2


if __name__ == "__main__":
    sys.exit(main())
