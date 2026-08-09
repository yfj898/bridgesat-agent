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

Each scenario runs in its own tenant so student/device fixtures never leak
across scenarios (RLS isolation mirrors the per-database isolation the
SQLite-era eval used).

Writes reports/offline_sync_eval.json and evals/offline_sync/REPORT.md.

Usage:
    python scripts/run_offline_sync_evals.py [--json reports/offline_sync_eval.json]
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "BRIDGESAT_PACKS_ROOT", str(ROOT / "tests" / "fixtures" / "packs")
)

import psycopg

from app.infrastructure import pg
from app.infrastructure.migration_runner import migrate_database
from app.infrastructure.pg import transaction
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


def _new_service(tenant: str) -> tuple[SyncService, psycopg.Connection]:
    conn = pg.connect()
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    conn.commit()
    return SyncService(conn), conn


def _close(connection: psycopg.Connection) -> None:
    connection.rollback()
    connection.close()


def _reset_schema() -> None:
    admin = pg.connect_admin()
    admin.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    admin.commit()
    migrate_database(admin)
    admin.close()


def _scenario(tenant: str, student_id: str) -> tuple[SyncService, psycopg.Connection]:
    _reset_schema()
    service, conn = _new_service(tenant)
    _seed_student(service, student_id)
    return service, conn


def _seed_student(service: SyncService, student_id: str = STUDENT_ID) -> None:
    from app.domain.events import compute_integrity_hash, utc_now_iso

    now = utc_now_iso()
    with transaction(service.connection):
        service.connection.execute(
            """
            INSERT INTO students (
                tenant_id, id, name, daily_minutes, target_score, mastery_json,
                status, created_at, updated_at
            ) VALUES (
                current_setting('app.tenant_id'), %s, %s, %s, %s, '{}', 'active', %s, %s
            )
            """,
            (student_id, "Test Student", 20, 1200, now, now),
        )
        service.connection.execute(
            """
            INSERT INTO learning_events (
                tenant_id, event_id, student_id, session_id, event_type, payload_json,
                policy_version, content_version, occurred_at, received_at,
                device_id, device_sequence, origin, integrity_hash
            ) VALUES (
                current_setting('app.tenant_id'), %s, %s, '', 'STUDENT_CREATED', %s,
                'policy-0.1.0', NULL, %s, %s, NULL, NULL, 'online', %s
            )
            """,
            (
                f"evt_seed_{student_id}",
                student_id,
                json.dumps({"name": "Test Student", "daily_minutes": 20, "target_score": 1200}),
                now,
                now,
                compute_integrity_hash(
                    "STUDENT_CREATED",
                    {"name": "Test Student", "daily_minutes": 20, "target_score": 1200},
                ),
            ),
        )


def _process(
    service: SyncService,
    events: list[dict],
    device_id: str = DEVICE_A,
    student_id: str = STUDENT_ID,
):
    return service.process_batch(
        SyncRequest(
            device_id=device_id,
            student_id=student_id,
            events=[SyncEventEnvelope(**e) for e in events],
        )
    )


def _count(service: SyncService, sql: str, params: tuple = ()) -> int:
    with transaction(service.connection):
        row = service.connection.execute(sql, params).fetchone()
    return row[0] if isinstance(row, tuple) else row["total"]


def _run_scenarios() -> list[dict]:
    results: list[dict] = []

    def record(scenario: str, passed: bool, detail: str, target: str) -> None:
        results.append(
            {"scenario": scenario, "passed": passed, "detail": detail, "target": target}
        )

    # 1. full offline session ------------------------------------------------
    service, conn = _scenario("tenant_s1", "student_01")
    service.register_device("student_01", "a", device_id=DEVICE_A)
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
    response = _process(service, session_events, student_id="student_01")
    accepted_all = sorted(response.accepted_event_ids) == sorted(
        [e["event_id"] for e in session_events]
    )
    snapshot = service.build_snapshot("student_01")
    evidence = sum(s["evidence_count"] for s in snapshot.skill_states)
    record(
        "full_offline_session",
        accepted_all and evidence >= 2,
        f"accepted={len(response.accepted_event_ids)}/6 evidence={evidence}",
        "offline core-flow completion = 100%",
    )

    # 2. refresh recovery ------------------------------------------------------
    response2 = _process(service, session_events, student_id="student_01")
    dup_scored = _count(
        service, "SELECT COUNT(*) AS total FROM answer_attempts WHERE validity = 'valid'"
    )
    record(
        "refresh_recovery",
        response2.duplicate_event_ids == [e["event_id"] for e in session_events]
        and dup_scored == 2,
        f"duplicates={len(response2.duplicate_event_ids)} scored_valid={dup_scored}",
        "duplicate scoring incidents = 0",
    )

    # 3. server restart ---------------------------------------------------------
    restarted, restarted_conn = _new_service("tenant_s1")
    snapshot2 = restarted.build_snapshot("student_01")
    record(
        "server_restart",
        snapshot2.snapshot_version >= 5 and len(snapshot2.skill_states) == 2,
        f"version={snapshot2.snapshot_version} skills={len(snapshot2.skill_states)}",
        "restart recovery = 100%",
    )

    # 4. duplicate batch upload --------------------------------------------------
    dup_batch = _process(restarted, [_envelope(event_id="evt_a1", device_sequence=3)], student_id="student_01")
    record(
        "duplicate_batch_upload",
        "evt_a1" in dup_batch.duplicate_event_ids
        and "evt_a1" not in dup_batch.accepted_event_ids,
        f"duplicate={dup_batch.duplicate_event_ids} accepted={dup_batch.accepted_event_ids}",
        "duplicate scoring incidents = 0",
    )
    _close(conn)
    _close(restarted_conn)

    # 5. out-of-order upload -----------------------------------------------------
    service5, conn5 = _scenario("tenant_s5", "student_05")
    service5.register_device("student_05", "a", device_id="device_a5")
    dep_second = _envelope(event_id="evt_2", device_sequence=2, depends_on=["evt_1"])
    dep_second["integrity_hash"] = _integrity(dep_second["event_type"], dep_second["payload"])
    first = _process(service5, [dep_second], device_id="device_a5", student_id="student_05")
    dep_held = bool(first.rejected_events and first.rejected_events[0].code == "MISSING_DEPENDENCY")
    second = _process(service5, [_envelope(event_id="evt_1", device_sequence=1)], device_id="device_a5", student_id="student_05")
    third = _process(service5, [dep_second], device_id="device_a5", student_id="student_05")
    record(
        "out_of_order_upload",
        dep_held and "evt_1" in second.accepted_event_ids and "evt_2" in third.accepted_event_ids,
        f"first={[r.code for r in first.rejected_events]} "
        f"then accepted={third.accepted_event_ids}",
        "known-version scoring consistency = 100%",
    )
    _close(conn5)

    # 6. late event after summary ------------------------------------------------
    service6, conn6 = _scenario("tenant_s6", "student_06")
    service6.register_device("student_06", "a", device_id="device_a6")
    _process(service6, [_envelope(event_id="evt_1", device_sequence=1)], device_id="device_a6", student_id="student_06")
    _process(service6, [_envelope(event_id="evt_2", device_sequence=2,
                                  event_type="SESSION_COMPLETED", payload={},
                                  question_id=None, question_version=None)], device_id="device_a6", student_id="student_06")
    late = _process(service6, [_envelope(event_id="evt_3", device_sequence=3,
                                         question_id=Q_RATIOS, question_version=2)], device_id="device_a6", student_id="student_06")
    state = None
    with transaction(service6.connection):
        state = service6.connection.execute(
            "SELECT session_state FROM study_sessions WHERE session_id = %s",
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
    _close(conn6)

    # 7. old known content version ------------------------------------------------
    service7, conn7 = _scenario("tenant_s7", "student_07")
    service7.register_device("student_07", "a", device_id="device_a7")
    old = _process(service7, [_envelope(event_id="evt_old", question_id=Q_RATIOS,
                                        question_version=1)], device_id="device_a7", student_id="student_07")
    old_rejected = bool(old.rejected_events
                        and old.rejected_events[0].code == "QUESTION_VERSION_UNKNOWN")
    record(
        "old_known_content_version",
        old_rejected and not old.accepted_event_ids,
        f"code={[r.code for r in old.rejected_events]}",
        "known-version scoring consistency = 100%",
    )
    _close(conn7)

    # 8. unknown content version ----------------------------------------------------
    service8, conn8 = _scenario("tenant_s8", "student_08")
    service8.register_device("student_08", "a", device_id="device_a8")
    env = _envelope(event_id="evt_1", pack_version="9.9.9")
    env["integrity_hash"] = _integrity(env["event_type"], env["payload"])
    unknown = _process(service8, [env], device_id="device_a8", student_id="student_08")
    record(
        "unknown_content_version",
        bool(unknown.rejected_events
             and unknown.rejected_events[0].code == "QUESTION_VERSION_UNKNOWN"),
        f"code={[r.code for r in unknown.rejected_events]}",
        "known-version scoring consistency = 100%",
    )
    _close(conn8)

    # 9. parallel device branches ----------------------------------------------------
    service9, conn9 = _scenario("tenant_s9", "student_09")
    service9.register_device("student_09", "a", device_id="device_a9")
    service9.register_device("student_09", "b", device_id="device_b9")
    _process(service9, [_envelope(event_id="evt_1", device_id="device_a9",
                                  question_id=Q_RATIOS, question_version=2)], device_id="device_a9", student_id="student_09")
    par = _process(service9, [_envelope(event_id="evt_2", device_id="device_b9",
                                        device_sequence=1, question_id=Q_RATIOS,
                                        question_version=2)], device_id="device_b9", student_id="student_09")
    weights = []
    with transaction(service9.connection):
        weights = [r["weight"] for r in service9.connection.execute(
            "SELECT weight FROM answer_attempts WHERE session_id = %s "
            "AND content_id = %s ORDER BY event_id", (SESSION_ID, Q_RATIOS)).fetchall()]
    record(
        "parallel_device_branches",
        any(c.conflict_type == "PARALLEL_ATTEMPT_DETECTED" for c in par.conflicts)
        and len(weights) == 2 and weights[1] == 0.5,
        f"weights={weights} conflicts={[c.conflict_type for c in par.conflicts]}",
        "duplicate scoring incidents = 0",
    )
    _close(conn9)

    # 10. pending event retention after failure --------------------------------------
    service10, conn10 = _scenario("tenant_s10", "student_010")
    service10.register_device("student_010", "a", device_id="device_a10")
    tampered = _envelope(event_id="evt_retry")
    tampered["payload"]["selected_choice_id"] = "C"
    tampered["integrity_hash"] = _integrity("ANSWER_SUBMITTED", {"original": "payload"})
    failed = _process(service10, [tampered], device_id="device_a10", student_id="student_010")
    failed_count = _count(service10, "SELECT COUNT(*) AS total FROM learning_events "
                                    "WHERE event_id = 'evt_retry'")
    retried = _process(service10, [_envelope(event_id="evt_retry")], device_id="device_a10", student_id="student_010")
    record(
        "pending_event_retention_after_failure",
        bool(failed.rejected_events and failed.rejected_events[0].code == "INVALID_SCHEMA")
        and failed_count == 0
        and "evt_retry" in retried.accepted_event_ids,
        f"first={[r.code for r in failed.rejected_events]} stored_before_retry={failed_count} "
        f"then accepted={retried.accepted_event_ids}",
        "unacknowledged-event loss = 0",
    )
    _close(conn10)

    return results


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--json", type=Path, default=REPORT_JSON)
    args = parser.parse_args()

    results = _run_scenarios()

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
