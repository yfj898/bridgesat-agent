#!/usr/bin/env python3
"""Offline and synchronization eval (EVALUATION_SPEC section 7, plan section 6).

Label: controlled internal test — runs the real SyncService against the
SYNC_PROTOCOL.md scenarios with deterministic fixtures.

Scenarios (EVALUATION_SPEC section 7):

- full offline session;
- refresh recovery;
- server restart;
- duplicate batch upload;
- out-of-order upload;
- late event after summary;
- old known content version;
- unknown content version;
- parallel device branches;
- pending event retention after failure.

Targets:

- offline core-flow completion = 100%
- duplicate scoring incidents = 0
- restart recovery = 100%
- known-version scoring consistency = 100%
- unacknowledged-event loss = 0

Writes reports/offline_sync_eval.json and evals/offline_sync/REPORT.md.

Usage:
    python scripts/run_offline_sync_evals.py [--json reports/offline_sync_eval.json]
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "BRIDGESAT_PACKS_ROOT", str(ROOT / "tests" / "fixtures" / "packs")
)

from app.infrastructure.database import connect
from app.sync.protocol import SyncEventEnvelope, SyncRequest
from app.sync.service import SyncService

REPORT_JSON = ROOT / "reports" / "offline_sync_eval.json"
REPORT_MD = ROOT / "evals" / "offline_sync" / "REPORT.md"

PACK_VERSION = "0.1.0"
Q_LINEAR = "sync.linear.001"
Q_RATIOS = "sync.ratios.001"
STUDENT_ID = "student_01"
DEVICE_A = "device_a"
DEVICE_B = "device_b"
SESSION_ID = "session_01"


def _integrity(event_type: str, payload: dict) -> str:
    digest = hashlib.sha256()
    digest.update(event_type.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return f"sha256:{digest.hexdigest()}"


def _envelope(
    *,
    event_id: str,
    device_id: str = DEVICE_A,
    device_sequence: int = 1,
    event_type: str = "ANSWER_SUBMITTED",
    payload: dict | None = None,
    session_id: str = SESSION_ID,
    question_id: str | None = Q_LINEAR,
    question_version: int | None = 1,
    depends_on: list[str] | None = None,
    include_hash: bool = True,
    pack_version: str = PACK_VERSION,
) -> dict:
    payload = payload or {
        "question_id": question_id,
        "question_version": question_version,
        "selected_choice_id": "A",
        "hint_level": 0,
        "attempt_id": event_id,
    }
    envelope = {
        "event_id": event_id,
        "student_id": STUDENT_ID,
        "session_id": session_id,
        "session_branch_id": "branch_" + device_id,
        "device_id": device_id,
        "device_sequence": device_sequence,
        "event_type": event_type,
        "payload": payload,
        "content_pack_version": pack_version,
        "question_id": question_id,
        "question_version": question_version,
        "policy_version": "offline-policy-v1",
        "depends_on_event_ids": depends_on or [],
        "device_occurred_at": "2026-08-07T16:00:00+08:00",
    }
    if include_hash:
        envelope["integrity_hash"] = _integrity(event_type, payload)
    return envelope


def _seed_student(service: SyncService, student_id: str = STUDENT_ID) -> None:
    from app.domain.events import compute_integrity_hash, utc_now_iso
    from app.infrastructure.database import transaction

    now = utc_now_iso()
    with connect(service.db) as connection:
        with transaction(connection):
            connection.execute(
                """
                INSERT INTO students (
                    id, name, daily_minutes, target_score, mastery_json,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, '{}', 'active', ?, ?)
                """,
                (student_id, "Test Student", 20, 1200, now, now),
            )
            connection.execute(
                """
                INSERT INTO learning_events (
                    event_id, student_id, session_id, event_type, payload_json,
                    policy_version, content_version, occurred_at, received_at,
                    device_id, device_sequence, origin, integrity_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"evt_seed_{student_id}", student_id, "",
                    "STUDENT_CREATED",
                    json.dumps({"name": "Test Student", "daily_minutes": 20, "target_score": 1200}),
                    "policy-0.1.0", None, now, now, None, None, "online",
                    compute_integrity_hash(
                        "STUDENT_CREATED",
                        {"name": "Test Student", "daily_minutes": 20, "target_score": 1200},
                    ),
                ),
            )


def _process(service: SyncService, events: list[dict], device_id: str = DEVICE_A):
    return service.process_batch(
        SyncRequest(
            device_id=device_id,
            student_id=STUDENT_ID,
            events=[SyncEventEnvelope(**e) for e in events],
        )
    )


def _count(service: SyncService, sql: str) -> int:
    with connect(service.db) as connection:
        return connection.execute(sql).fetchone()[0]


def _run_scenarios(tmp: Path) -> list[dict]:
    results: list[dict] = []

    def record(scenario: str, passed: bool, detail: str, target: str) -> None:
        results.append(
            {"scenario": scenario, "passed": passed, "detail": detail, "target": target}
        )

    # 1. full offline session ------------------------------------------------
    db = tmp / "full_offline.db"
    service = SyncService(db)
    _seed_student(service)
    service.register_device(STUDENT_ID, "a", device_id=DEVICE_A)
    session_events = [
        _envelope(event_id="evt_start", event_type="DIAGNOSTIC_STARTED",
                  payload={"started_at": "2026-08-07T16:00:00+08:00"},
                  question_id=None, question_version=None),
        _envelope(event_id="evt_p1", device_sequence=2, event_type="CONTENT_PRESENTED",
                  payload={"question_id": Q_LINEAR}, question_id=None, question_version=None),
        _envelope(event_id="evt_a1", device_sequence=3, question_id=Q_LINEAR, question_version=1),
        _envelope(event_id="evt_p2", device_sequence=4, event_type="CONTENT_PRESENTED",
                  payload={"question_id": Q_RATIOS}, question_id=None, question_version=None),
        _envelope(event_id="evt_a2", device_sequence=5, question_id=Q_RATIOS, question_version=2),
        _envelope(event_id="evt_done", device_sequence=6, event_type="SESSION_COMPLETED",
                  payload={}, question_id=None, question_version=None),
    ]
    response = _process(service, session_events)
    accepted_all = sorted(response.accepted_event_ids) == sorted(
        [e["event_id"] for e in session_events]
    )
    snapshot = service.build_snapshot(STUDENT_ID)
    evidence = sum(s["evidence_count"] for s in snapshot.skill_states)
    record(
        "full_offline_session",
        accepted_all and evidence >= 2,
        f"accepted={len(response.accepted_event_ids)}/6 evidence={evidence}",
        "offline core-flow completion = 100%",
    )

    # 2. refresh recovery ------------------------------------------------------
    response2 = _process(service, session_events)
    dup_scored = _count(
        service, "SELECT COUNT(*) FROM answer_attempts WHERE validity = 'valid'"
    )
    record(
        "refresh_recovery",
        response2.duplicate_event_ids == [e["event_id"] for e in session_events]
        and dup_scored == 2,
        f"duplicates={len(response2.duplicate_event_ids)} scored_valid={dup_scored}",
        "duplicate scoring incidents = 0",
    )

    # 3. server restart ---------------------------------------------------------
    restarted = SyncService(db)
    snapshot2 = restarted.build_snapshot(STUDENT_ID)
    record(
        "server_restart",
        snapshot2.snapshot_version >= 5 and len(snapshot2.skill_states) == 2,
        f"version={snapshot2.snapshot_version} skills={len(snapshot2.skill_states)}",
        "restart recovery = 100%",
    )

    # 4. duplicate batch upload --------------------------------------------------
    dup_batch = _process(restarted, [_envelope(event_id="evt_a1", device_sequence=3)])
    record(
        "duplicate_batch_upload",
        "evt_a1" in dup_batch.duplicate_event_ids
        and "evt_a1" not in dup_batch.accepted_event_ids,
        f"duplicate={dup_batch.duplicate_event_ids} accepted={dup_batch.accepted_event_ids}",
        "duplicate scoring incidents = 0",
    )

    # 5. out-of-order upload -----------------------------------------------------
    db5 = tmp / "out_of_order.db"
    service5 = SyncService(db5)
    _seed_student(service5)
    service5.register_device(STUDENT_ID, "a", device_id=DEVICE_A)
    dep_second = _envelope(event_id="evt_2", device_sequence=2, depends_on=["evt_1"])
    dep_second["integrity_hash"] = _integrity(dep_second["event_type"], dep_second["payload"])
    first = _process(service5, [dep_second])
    dep_held = bool(first.rejected_events and first.rejected_events[0].code == "MISSING_DEPENDENCY")
    second = _process(service5, [_envelope(event_id="evt_1", device_sequence=1)])
    third = _process(service5, [dep_second])
    record(
        "out_of_order_upload",
        dep_held and "evt_1" in second.accepted_event_ids and "evt_2" in third.accepted_event_ids,
        f"first={[r.code for r in first.rejected_events]} "
        f"then accepted={third.accepted_event_ids}",
        "known-version scoring consistency = 100%",
    )

    # 6. late event after summary ------------------------------------------------
    db6 = tmp / "late.db"
    service6 = SyncService(db6)
    _seed_student(service6)
    service6.register_device(STUDENT_ID, "a", device_id=DEVICE_A)
    _process(service6, [_envelope(event_id="evt_1", device_sequence=1)])
    _process(service6, [_envelope(event_id="evt_2", device_sequence=2,
                                  event_type="SESSION_COMPLETED", payload={},
                                  question_id=None, question_version=None)])
    late = _process(service6, [_envelope(event_id="evt_3", device_sequence=3,
                                         question_id=Q_RATIOS, question_version=2)])
    state = None
    with connect(service6.db) as connection:
        state = connection.execute(
            "SELECT session_state FROM study_sessions WHERE session_id = ?",
            (SESSION_ID,),
        ).fetchone()["session_state"]
    record(
        "late_event_after_summary",
        "evt_3" in late.accepted_event_ids
        and any(c.conflict_type == "SUMMARY_REVISED" for c in late.conflicts)
        and state == "SESSION_COMPLETED",
        f"accepted={late.accepted_event_ids} conflicts={[c.conflict_type for c in late.conflicts]} "
        f"state={state}",
        "unacknowledged-event loss = 0",
    )

    # 7. old known content version ------------------------------------------------
    db7 = tmp / "old_version.db"
    service7 = SyncService(db7)
    _seed_student(service7)
    service7.register_device(STUDENT_ID, "a", device_id=DEVICE_A)
    old = _process(service7, [_envelope(event_id="evt_old", question_id=Q_RATIOS,
                                        question_version=1)])
    old_rejected = bool(old.rejected_events
                        and old.rejected_events[0].code == "QUESTION_VERSION_UNKNOWN")
    record(
        "old_known_content_version",
        old_rejected and not old.accepted_event_ids,
        f"code={[r.code for r in old.rejected_events]}",
        "known-version scoring consistency = 100%",
    )

    # 8. unknown content version ----------------------------------------------------
    db8 = tmp / "unknown_version.db"
    service8 = SyncService(db8)
    _seed_student(service8)
    service8.register_device(STUDENT_ID, "a", device_id=DEVICE_A)
    env = _envelope(event_id="evt_1", pack_version="9.9.9")
    env["integrity_hash"] = _integrity(env["event_type"], env["payload"])
    unknown = _process(service8, [env])
    record(
        "unknown_content_version",
        bool(unknown.rejected_events
             and unknown.rejected_events[0].code == "QUESTION_VERSION_UNKNOWN"),
        f"code={[r.code for r in unknown.rejected_events]}",
        "known-version scoring consistency = 100%",
    )

    # 9. parallel device branches ----------------------------------------------------
    db9 = tmp / "parallel.db"
    service9 = SyncService(db9)
    _seed_student(service9)
    service9.register_device(STUDENT_ID, "a", device_id=DEVICE_A)
    service9.register_device(STUDENT_ID, "b", device_id=DEVICE_B)
    _process(service9, [_envelope(event_id="evt_1", device_id=DEVICE_A,
                                  question_id=Q_RATIOS, question_version=2)])
    par = _process(service9, [_envelope(event_id="evt_2", device_id=DEVICE_B,
                                        device_sequence=1, question_id=Q_RATIOS,
                                        question_version=2)], device_id=DEVICE_B)
    weights = []
    with connect(service9.db) as connection:
        weights = [r["weight"] for r in connection.execute(
            "SELECT weight FROM answer_attempts WHERE session_id = ? "
            "AND content_id = ? ORDER BY event_id", (SESSION_ID, Q_RATIOS)).fetchall()]
    record(
        "parallel_device_branches",
        any(c.conflict_type == "PARALLEL_ATTEMPT_DETECTED" for c in par.conflicts)
        and len(weights) == 2 and weights[1] == 0.5,
        f"weights={weights} conflicts={[c.conflict_type for c in par.conflicts]}",
        "duplicate scoring incidents = 0",
    )

    # 10. pending event retention after failure --------------------------------------
    db10 = tmp / "retention.db"
    service10 = SyncService(db10)
    _seed_student(service10)
    service10.register_device(STUDENT_ID, "a", device_id=DEVICE_A)
    tampered = _envelope(event_id="evt_retry")
    tampered["payload"]["selected_choice_id"] = "C"
    tampered["integrity_hash"] = _integrity("ANSWER_SUBMITTED", {"original": "payload"})
    failed = _process(service10, [tampered])
    failed_count = _count(service10, "SELECT COUNT(*) FROM learning_events "
                                    "WHERE event_id = 'evt_retry'")
    retried = _process(service10, [_envelope(event_id="evt_retry")])
    record(
        "pending_event_retention_after_failure",
        bool(failed.rejected_events and failed.rejected_events[0].code == "INVALID_SCHEMA")
        and failed_count == 0
        and "evt_retry" in retried.accepted_event_ids,
        f"first={[r.code for r in failed.rejected_events]} stored_before_retry={failed_count} "
        f"then accepted={retried.accepted_event_ids}",
        "unacknowledged-event loss = 0",
    )

    return results


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--json", type=Path, default=REPORT_JSON)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        results = _run_scenarios(Path(tmp))

    passed = sum(1 for r in results if r["passed"])
    targets = {
        "offline_core_flow_completion": "100%",
        "duplicate_scoring_incidents": "0",
        "restart_recovery": "100%",
        "known_version_scoring_consistency": "100%",
        "unacknowledged_event_loss": "0",
    }
    summary = {
        "schema_version": "1.0",
        "label": "controlled internal test",
        "scenario_count": len(results),
        "passed": passed,
        "pass_rate": round(passed / len(results), 4) if results else 0.0,
        "targets": targets,
        "results": results,
    }

    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    REPORT_MD.parent.mkdir(parents=True, exist_ok=True)
    REPORT_MD.write_text(
        f"""# Offline and synchronization eval report

- label: {summary['label']}
- scenarios: {summary['scenario_count']}
- pass rate: {summary['pass_rate']:.0%}

| Scenario | Target | Result |
|---|---|---|
""" + "\n".join(
            f"| {r['scenario']} | {r['target']} | {'PASS' if r['passed'] else 'FAIL'} "
            f"({r['detail']}) |"
            for r in results
        ) + "\n",
        encoding="utf-8",
    )

    print(json.dumps({
        "scenarios": summary["scenario_count"],
        "pass_rate": summary["pass_rate"],
    }, indent=2))
    for r in results:
        if not r["passed"]:
            print(f"  FAIL {r['scenario']}: {r['detail']}")
    return 0 if summary["pass_rate"] == 1.0 else 2


if __name__ == "__main__":
    sys.exit(main())
