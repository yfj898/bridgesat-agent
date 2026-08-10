#!/usr/bin/env python3
"""Seed a ready-to-demo student with a full practice history (idempotent).

Creates in the configured PostgreSQL database:

- demo student with a profile;
- diagnostic results and mastery plan;
- a registered demo device (for the offline-sync demo);
- one practice session with correct and misconception answers, scored by
  version-bound keys;
- long-term memory episodes for the demo student (via the outbox worker).

Idempotent: if the demo student already exists, exits 0 without touching data.

Usage:
    python scripts/seed_demo.py [--db DSN] [--admin-db DSN]
                                [--tenant TENANT_ID]
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.engine import build_plan, score_diagnostic
from app.auth import TokenStore
from app.infrastructure import pg
from app.infrastructure.learner_store import LearnerStore
from app.infrastructure.migration_runner import migrate_database
from app.memory import MemoryMode, build_mnemis_index, memory_mode
from app.models import DiagnosticAnswer
from app.repository import StudentRepository
from app.sync.protocol import SyncEventEnvelope, SyncRequest
from app.sync.service import SyncService
from app.memory.episode_builder import EpisodeBuilder
from app.memory.worker import OutboxWorker

PACK_VERSION = "0.1.0"
DEMO_EVENT_START = datetime(2026, 8, 7, 10, 0, tzinfo=timezone(timedelta(hours=8)))


@dataclass(frozen=True)
class DemoIdentifiers:
    device_id: str
    session_id: str
    branch_id: str
    event_prefix: str
    attempt_prefix: str


def _default_tenant() -> str:
    return os.getenv("BRIDGESAT_DEFAULT_TENANT", "tenant_demo")


def _demo_identifiers(tenant_id: str) -> DemoIdentifiers:
    """Build stable, tenant-qualified IDs for globally unique PG keys."""
    slug = re.sub(r"[^a-z0-9]+", "_", tenant_id.lower()).strip("_")[:8]
    slug = slug or "tenant"
    tenant_hash = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:16]
    namespace = f"{slug}_{tenant_hash}"
    return DemoIdentifiers(
        device_id=f"demo_device_{namespace}",
        session_id=f"demo_session_01_{namespace}",
        branch_id=f"branch_demo_device_{namespace}",
        event_prefix=f"demo_{namespace}",
        attempt_prefix=f"demo_att_{namespace}",
    )


def _integrity(event_type: str, payload: dict) -> str:
    digest = hashlib.sha256()
    digest.update(event_type.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return f"sha256:{digest.hexdigest()}"


def _demo_answers() -> list[DiagnosticAnswer]:
    """Deterministic diagnostic answers: weak on ratios, strong elsewhere."""
    from app.question_bank import load_questions

    questions = load_questions()
    by_skill: dict[str, list] = {}
    for question in questions:
        by_skill.setdefault(question.skill, []).append(question)

    answers: list[DiagnosticAnswer] = []
    weak_skill = "ratios_percentages"
    for skill, group in by_skill.items():
        for question in group[:2]:
            if skill == weak_skill:
                wrong = [c for c in question.choices if c != question.answer][0]
                answers.append(DiagnosticAnswer(question_id=question.id,
                                                selected_answer=wrong))
            else:
                answers.append(DiagnosticAnswer(question_id=question.id,
                                                selected_answer=question.answer))
    return answers


def _practice_events(
    student_id: str, identifiers: DemoIdentifiers | None = None
) -> list[SyncEventEnvelope]:
    """One offline practice session matching the demo narrative.

    Session 1 shows two consecutive `sign_error` answers on linear equations
    (the sign-error distractor choice), then a same-misconception transfer
    item answered correctly without hints, followed by correct answers in the
    other covered skills. EpisodeBuilder later encodes this as a
    `linear_equations`/`sign_error` episode with the SHOW_WORKED_EXAMPLE
    intervention (see setup_*_episode in main()).
    """
    from app.question_bank import packs_root

    identifiers = identifiers or _demo_identifiers(_default_tenant())
    items_path = packs_root() / f"bridgesat-math-{PACK_VERSION}" / "items.jsonl"
    items = [json.loads(line) for line in items_path.open(encoding="utf-8") if line.strip()]
    by_skill: dict[str, list] = {}
    for item in items:
        by_skill.setdefault(item["target_skill"], []).append(item)

    linear = by_skill["linear_equations"]
    wrong_pick = [c for c in linear[0]["choices"] if c["id"] == "B"][0]
    picks = [
        (linear[0], wrong_pick),          # linear: sign_error (distractor B)
        (linear[1], wrong_pick),          # linear: sign_error again (same B)
        (linear[2], None),                # linear: transfer item, correct, no hint
        (by_skill["functions_models"][0], None),
        (by_skill["systems_equations"][0], None),
        (by_skill["ratios_percentages"][0], None),
    ]

    events: list[SyncEventEnvelope] = []
    sequence = 1

    def append(event_type: str, payload: dict, *, question_id=None, version=None,
               depends_on: list[str] | None = None) -> None:
        nonlocal sequence
        envelope = {
            "event_id": f"{identifiers.event_prefix}_{event_type}_{sequence}",
            "student_id": student_id,
            "session_id": identifiers.session_id,
            "session_branch_id": identifiers.branch_id,
            "device_id": identifiers.device_id,
            "device_sequence": sequence,
            "event_type": event_type,
            "payload": payload,
            "content_pack_version": PACK_VERSION,
            "question_id": question_id,
            "question_version": version,
            "policy_version": "offline-policy-v1",
            "depends_on_event_ids": depends_on or [],
            "device_occurred_at": (
                DEMO_EVENT_START + timedelta(hours=sequence)
            ).isoformat(),
            "integrity_hash": _integrity(event_type, payload),
        }
        events.append(SyncEventEnvelope(**envelope))
        sequence += 1

    previous_id: str | None = None
    for item, forced_choice in picks:
        append(
            "CONTENT_PRESENTED",
            {"question_id": item["id"]},
            depends_on=[previous_id] if previous_id else None,
        )
        presented_id = events[-1].event_id
        selected = forced_choice["id"] if forced_choice else item["answer_choice_id"]
        append(
            "ANSWER_SUBMITTED",
            {
                "question_id": item["id"],
                "question_version": 1,
                "selected_choice_id": selected,
                "hint_level": 0,
                "attempt_id": f"{identifiers.attempt_prefix}_{item['id']}",
            },
            question_id=item["id"],
            version=1,
            depends_on=[presented_id],
        )
        previous_id = events[-1].event_id
    append("SESSION_COMPLETED", {"summary": "demo practice session"},
           depends_on=[previous_id] if previous_id else None)
    return events


def _demo_namespace_state(
    connection: psycopg.Connection, identifiers: DemoIdentifiers
) -> tuple[set[str], bool]:
    """Find any existing rows that could belong to this demo namespace."""
    student_ids: set[str] = set()
    has_rows = False

    queries = (
        (
            "SELECT student_id FROM devices "
            "WHERE tenant_id = current_setting('app.tenant_id', true) "
            "AND device_id = %s",
            (identifiers.device_id,),
        ),
        (
            "SELECT student_id FROM study_sessions "
            "WHERE tenant_id = current_setting('app.tenant_id', true) "
            "AND session_id = %s",
            (identifiers.session_id,),
        ),
        (
            "SELECT DISTINCT student_id FROM answer_attempts "
            "WHERE tenant_id = current_setting('app.tenant_id', true) "
            "AND session_id = %s",
            (identifiers.session_id,),
        ),
        (
            "SELECT DISTINCT student_id FROM learning_events "
            "WHERE tenant_id = current_setting('app.tenant_id', true) "
            "AND event_id LIKE %s",
            (f"{identifiers.event_prefix}_%",),
        ),
        (
            "SELECT DISTINCT student_id FROM learning_episodes "
            "WHERE tenant_id = current_setting('app.tenant_id', true) "
            "AND session_id = %s",
            (identifiers.session_id,),
        ),
        (
            "SELECT id AS student_id FROM students "
            "WHERE tenant_id = current_setting('app.tenant_id', true) "
            "AND name = %s",
            ("Demo Student",),
        ),
    )
    for query, params in queries:
        rows = connection.execute(query, params).fetchall()
        if rows:
            has_rows = True
            student_ids.update(row["student_id"] for row in rows)
    return student_ids, has_rows


def _validate_complete_seed(
    connection: psycopg.Connection,
    identifiers: DemoIdentifiers,
    student_id: str,
) -> tuple[bool, str]:
    device = connection.execute(
        "SELECT student_id, status FROM devices "
        "WHERE tenant_id = current_setting('app.tenant_id', true) "
        "AND device_id = %s",
        (identifiers.device_id,),
    ).fetchone()
    if device is None:
        return False, "demo device is missing"
    if device["student_id"] != student_id:
        return False, "demo device belongs to another student"
    if device["status"] != "active":
        return False, "demo device is not active"

    session = connection.execute(
        "SELECT student_id, session_state FROM study_sessions "
        "WHERE tenant_id = current_setting('app.tenant_id', true) "
        "AND session_id = %s",
        (identifiers.session_id,),
    ).fetchone()
    if session is None or session["student_id"] != student_id:
        return False, "demo session is missing or owned by another student"
    if session["session_state"] != "SESSION_COMPLETED":
        return False, f"demo session state is {session['session_state']!r}"

    expected_events = _practice_events(student_id, identifiers)
    expected_event_ids = {event.event_id for event in expected_events}
    actual_event_ids = {
        row["event_id"]
        for row in connection.execute(
            "SELECT event_id FROM learning_events "
            "WHERE tenant_id = current_setting('app.tenant_id', true) "
            "AND event_id LIKE %s",
            (f"{identifiers.event_prefix}_%",),
        ).fetchall()
    }
    if actual_event_ids != expected_event_ids:
        return False, "expected demo learning events are incomplete or unexpected"

    expected_attempt_ids = {
        event.payload["attempt_id"]
        for event in expected_events
        if event.event_type == "ANSWER_SUBMITTED"
    }
    actual_attempt_ids = {
        row["attempt_id"]
        for row in connection.execute(
            "SELECT attempt_id FROM answer_attempts "
            "WHERE tenant_id = current_setting('app.tenant_id', true) "
            "AND session_id = %s",
            (identifiers.session_id,),
        ).fetchall()
    }
    if actual_attempt_ids != expected_attempt_ids:
        return False, "expected demo answer attempts are incomplete or unexpected"

    token = connection.execute(
        "SELECT 1 FROM student_tokens "
        "WHERE tenant_id = current_setting('app.tenant_id', true) "
        "AND student_id = %s LIMIT 1",
        (student_id,),
    ).fetchone()
    if token is None:
        return False, "demo bearer token is missing"

    episode = connection.execute(
        """
        SELECT 1 FROM learning_episodes
        WHERE tenant_id = current_setting('app.tenant_id', true)
          AND student_id = %s AND session_id = %s
          AND skill = 'linear_equations'
          AND misconception = 'sign_error'
          AND intervention = 'SHOW_WORKED_EXAMPLE'
          AND status = 'validated'
        LIMIT 1
        """,
        (student_id, identifiers.session_id),
    ).fetchone()
    if episode is None:
        return False, "validated demo memory episode is missing"
    return True, ""


def _configured_index(connection: psycopg.Connection):
    if memory_mode() != MemoryMode.ENHANCED:
        return None
    return build_mnemis_index(connection)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None, help="PostgreSQL DSN")
    parser.add_argument("--admin-db", default=None, help="PostgreSQL admin DSN")
    parser.add_argument(
        "--tenant", default=_default_tenant(), help="tenant to seed (default tenant_demo)"
    )
    args = parser.parse_args(argv)
    target = args.db or pg.dsn()
    admin_target = args.admin_db or pg.admin_dsn()
    identifiers = _demo_identifiers(args.tenant)

    admin = None
    connection = None
    try:
        admin = pg.connect_admin(admin_target)
        connection = pg.connect(target)
        pg.assert_safe_app_role(connection)
        try:
            pg.assert_matching_database(admin, connection)
        except RuntimeError as exc:
            print(f"Refusing to run: {exc}", file=sys.stderr)
            return 2
        admin.rollback()
        connection.rollback()
        migrate_database(admin)
        connection.execute(
            "SELECT set_config('app.tenant_id', %s, false)", (args.tenant,)
        )
        connection.commit()

        learner = LearnerStore(connection)
        namespace_students, has_namespace_state = _demo_namespace_state(
            connection, identifiers
        )
        if has_namespace_state:
            if len(namespace_students) != 1:
                print(
                    "Demo seed namespace is inconsistent: expected exactly one "
                    "student; refusing to modify it.",
                    file=sys.stderr,
                )
                return 2
            existing_student_id = next(iter(namespace_students))
            try:
                complete, reason = _validate_complete_seed(
                    connection, identifiers, existing_student_id
                )
            except Exception as exc:
                print(
                    f"Unable to validate demo seed namespace ({exc}); refusing "
                    "to modify it.",
                    file=sys.stderr,
                )
                return 2
            if complete:
                print(
                    f"Demo device {identifiers.device_id} already seeded; nothing to do."
                )
                return 0
            print(
                f"Demo seed namespace is partial or inconsistent ({reason}); "
                "refusing to modify it.",
                file=sys.stderr,
            )
            return 2

        student_id, _ = learner.create_student("Demo Student", 20, 1200)

        answers = _demo_answers()
        repository = StudentRepository(connection)
        student = repository.get(student_id)
        if student is None:
            print(f"Student {student_id} not found after creation", file=sys.stderr)
            return 1
        diagnostic = score_diagnostic(student, answers)
        repository.update_mastery(student.id, diagnostic.mastery)
        plan = build_plan(diagnostic.weakest_skills, student.daily_minutes)

        sync = SyncService(connection)
        sync.register_device(
            student_id, "demo laptop", device_id=identifiers.device_id
        )
        response = sync.process_batch(
            SyncRequest(
                device_id=identifiers.device_id,
                student_id=student_id,
                events=_practice_events(student_id, identifiers),
            )
        )
        if response.rejected_events:
            print(
                "Demo sync rejected events: "
                f"{[rejection.code for rejection in response.rejected_events]}",
                file=sys.stderr,
            )
            return 2

        from app.domain.events import LearningEvent, LearningEventType, utc_now_iso

        now = utc_now_iso()

        def _event(event_id: str, event_type: LearningEventType, payload: dict,
                   session_id: str = identifiers.session_id) -> LearningEvent:
            return LearningEvent(
                event_id=f"{identifiers.event_prefix}_{event_id}",
                student_id=student_id,
                session_id=session_id,
                event_type=event_type,
                payload=payload,
                occurred_at=now,
                received_at=now,
                origin="online",
            ).with_integrity()

        context = _event("ctx_1", LearningEventType.INTERVENTION_SELECTED,
                         {"intervention": "SHOW_WORKED_EXAMPLE"})
        observation = _event("obs_1", LearningEventType.MISCONCEPTION_IDENTIFIED,
                             {"skill": "linear_equations", "misconception": "sign_error"})
        outcome = _event("out_1", LearningEventType.ANSWER_SUBMITTED,
                         {"question_id": "math.linear_equations.003", "correct": True})

        builder = EpisodeBuilder(connection)
        episode = builder.build_candidate(
            student_id=student_id,
            session_id=identifiers.session_id,
            skill="linear_equations",
            misconception="sign_error",
            intervention="SHOW_WORKED_EXAMPLE",
            context_event=context,
            evidence_events=[observation],
            outcome_event=outcome,
            outcome_correct=True,
            outcome_hint_level=0,
            outcome_content_id="math.linear_equations.003",
            teaching_content_id="math.linear_equations.001",
            summary="worked example resolved sign_error on a transfer item",
        )
        validated = builder.validate(episode)
        if validated.status != "validated":
            print(
                f"Demo memory episode was not validated: {validated.status}",
                file=sys.stderr,
            )
            return 2

        index = _configured_index(connection)
        worker = OutboxWorker(connection, index=index)
        drained = 0
        worker_failures: dict[str, str] = {}
        worker_failed = 0
        while True:
            claimed = worker.run_pending(student_id=student_id)
            worker_failed += worker.failed_total
            worker_failures.update(worker.last_errors)
            if claimed == 0:
                break
            drained += 1
        if worker_failed > 0:
            print(
                f"Demo memory delivery failed for {worker_failed} attempts: "
                f"{worker_failures}",
                file=sys.stderr,
            )
            return 2

        token = TokenStore(connection).issue(student_id)

        complete, reason = _validate_complete_seed(connection, identifiers, student_id)
        if not complete:
            print(
                f"Demo seed validation failed ({reason}); refusing to claim success.",
                file=sys.stderr,
            )
            return 2

        print(f"Seeded demo student {student_id}")
        print(f"  token: {token}")
        print(f"  mastery: {json.dumps(diagnostic.mastery, sort_keys=True)}")
        print(f"  weakest: {diagnostic.weakest_skills}")
        print(f"  plan: {[p.activity for p in plan]}")
        print(f"  sync accepted: {len(response.accepted_event_ids)} events")
        print(f"  sync rejected: {[r.code for r in response.rejected_events]}")
        print(f"  memory episode: {validated.episode_id} status={validated.status}")
        if index is None:
            print("  memory outbox left pending (local mode)")
        else:
            print(f"  memory outbox drained: {drained} batches")
        return 0
    finally:
        pg.quiet_close(connection)
        pg.quiet_close(admin)


if __name__ == "__main__":
    sys.exit(main())
