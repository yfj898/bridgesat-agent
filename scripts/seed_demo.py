#!/usr/bin/env python3
"""Seed a ready-to-demo student with a full practice history (idempotent).

Creates in ``data/bridgesat.db``:

- demo student with a profile;
- diagnostic results and mastery plan;
- a registered demo device (for the offline-sync demo);
- one practice session with correct and misconception answers, scored by
  version-bound keys;
- long-term memory episodes for the demo student (via the outbox worker).

Idempotent: if the demo student already exists, exits 0 without touching data.

Usage:
    python scripts/seed_demo.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.engine import build_plan, score_diagnostic
from app.infrastructure.learner_store import LearnerStore
from app.infrastructure.migration_runner import apply_migrations
from app.models import DiagnosticAnswer
from app.repository import StudentRepository
from app.sync.protocol import SyncEventEnvelope, SyncRequest
from app.sync.service import SyncService

DEMO_DEVICE_ID = "demo_device"
DEMO_SESSION_ID = "demo_session_01"
PACK_VERSION = "0.1.0"
DATABASE_PATH = ROOT / "data" / "bridgesat.db"


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


def _practice_events(student_id: str) -> list[SyncEventEnvelope]:
    """One offline practice session: 4 correct, 2 misconception answers."""
    from app.question_bank import packs_root

    items_path = packs_root() / f"bridgesat-math-{PACK_VERSION}" / "items.jsonl"
    items = [json.loads(line) for line in items_path.open(encoding="utf-8") if line.strip()]
    by_skill: dict[str, list] = {}
    for item in items:
        by_skill.setdefault(item["target_skill"], []).append(item)

    picks = [
        (by_skill["linear_equations"][0], True),
        (by_skill["ratios_percentages"][0], False),
        (by_skill["linear_equations"][1], True),
        (by_skill["ratios_percentages"][1], False),
        (by_skill["functions_models"][0], True),
        (by_skill["systems_equations"][0], True),
    ]

    events: list[SyncEventEnvelope] = []
    sequence = 1

    def append(event_type: str, payload: dict, *, question_id=None, version=None,
               depends_on: list[str] | None = None) -> None:
        nonlocal sequence
        envelope = {
            "event_id": f"demo_{event_type}_{sequence}",
            "student_id": student_id,
            "session_id": DEMO_SESSION_ID,
            "session_branch_id": "branch_demo_device",
            "device_id": DEMO_DEVICE_ID,
            "device_sequence": sequence,
            "event_type": event_type,
            "payload": payload,
            "content_pack_version": PACK_VERSION,
            "question_id": question_id,
            "question_version": version,
            "policy_version": "offline-policy-v1",
            "depends_on_event_ids": depends_on or [],
            "device_occurred_at": f"2026-08-07T1{sequence}:00:00+08:00",
            "integrity_hash": _integrity(event_type, payload),
        }
        events.append(SyncEventEnvelope(**envelope))
        sequence += 1

    previous_id: str | None = None
    for item, correct in picks:
        append(
            "CONTENT_PRESENTED",
            {"question_id": item["id"]},
            depends_on=[previous_id] if previous_id else None,
        )
        presented_id = events[-1].event_id
        selected = item["answer_choice_id"] if correct else [
            c["id"] for c in item["choices"] if c["id"] != item["answer_choice_id"]
        ][0]
        append(
            "ANSWER_SUBMITTED",
            {
                "question_id": item["id"],
                "question_version": 1,
                "selected_choice_id": selected,
                "hint_level": 0,
                "attempt_id": f"demo_att_{item['id']}",
            },
            question_id=item["id"],
            version=1,
            depends_on=[presented_id],
        )
        previous_id = events[-1].event_id
    append("SESSION_COMPLETED", {"summary": "demo practice session"},
           depends_on=[previous_id] if previous_id else None)
    return events


def main() -> int:
    apply_migrations(DATABASE_PATH)
    learner = LearnerStore(DATABASE_PATH)

    from app.infrastructure.database import connect

    with connect(DATABASE_PATH) as connection:
        existing = connection.execute(
            "SELECT 1 FROM devices WHERE device_id = ?", (DEMO_DEVICE_ID,)
        ).fetchone()
        attempts = connection.execute(
            "SELECT COUNT(*) AS total FROM answer_attempts "
            "WHERE session_id = ?", (DEMO_SESSION_ID,),
        ).fetchone()["total"]
    if existing is not None and attempts > 0:
        print(f"Demo device {DEMO_DEVICE_ID} already seeded; nothing to do.")
        return 0

    if existing is None:
        student_id, _ = learner.create_student("Demo Student", 20, 1200)
    else:
        with connect(DATABASE_PATH) as connection:
            student_id = connection.execute(
                "SELECT student_id FROM devices WHERE device_id = ?",
                (DEMO_DEVICE_ID,),
            ).fetchone()["student_id"]

    answers = _demo_answers()
    student = StudentRepository(DATABASE_PATH).get(student_id)
    if student is None:
        print(f"Student {student_id} not found after creation", file=sys.stderr)
        return 1
    diagnostic = score_diagnostic(student, answers)
    repository = StudentRepository(DATABASE_PATH)
    repository.update_mastery(student.id, diagnostic.mastery)
    plan = build_plan(diagnostic.weakest_skills, student.daily_minutes)

    sync = SyncService(DATABASE_PATH)
    if existing is None:
        sync.register_device(student_id, "demo laptop", device_id=DEMO_DEVICE_ID)
    response = sync.process_batch(
        SyncRequest(
            device_id=DEMO_DEVICE_ID,
            student_id=student_id,
            events=_practice_events(student_id),
        )
    )

    from app.domain.events import LearningEvent, LearningEventType, utc_now_iso
    from app.memory.episode_builder import EpisodeBuilder

    now = utc_now_iso()

    def _event(event_id: str, event_type: LearningEventType, payload: dict,
               session_id: str = DEMO_SESSION_ID) -> LearningEvent:
        return LearningEvent(
            event_id=event_id,
            student_id=student_id,
            session_id=session_id,
            event_type=event_type,
            payload=payload,
            occurred_at=now,
            received_at=now,
            origin="online",
        ).with_integrity()

    context = _event("demo_ctx_1", LearningEventType.INTERVENTION_SELECTED,
                     {"intervention": "SHOW_WORKED_EXAMPLE"})
    observation = _event("demo_obs_1", LearningEventType.MISCONCEPTION_IDENTIFIED,
                         {"skill": "linear_equations", "misconception": "sign_error"})
    outcome = _event("demo_out_1", LearningEventType.ANSWER_SUBMITTED,
                     {"question_id": "math.linear_equations.004", "correct": True})

    builder = EpisodeBuilder(DATABASE_PATH)
    episode = builder.build_candidate(
        student_id=student_id,
        session_id=DEMO_SESSION_ID,
        skill="linear_equations",
        misconception="sign_error",
        intervention="SHOW_WORKED_EXAMPLE",
        context_event=context,
        evidence_events=[observation],
        outcome_event=outcome,
        outcome_correct=True,
        outcome_hint_level=0,
        outcome_content_id="math.linear_equations.004",
        teaching_content_id="math.linear_equations.001",
        summary="worked example resolved sign_error on a transfer item",
    )
    validated = builder.validate(episode)

    from app.memory.worker import OutboxWorker
    worker = OutboxWorker(DATABASE_PATH)
    drained = 0
    while worker.run_pending() > 0:
        drained += 1

    print(f"Seeded demo student {student_id}")
    print(f"  mastery: {json.dumps(diagnostic.mastery, sort_keys=True)}")
    print(f"  weakest: {diagnostic.weakest_skills}")
    print(f"  plan: {[p.activity for p in plan]}")
    print(f"  sync accepted: {len(response.accepted_event_ids)} events")
    print(f"  sync rejected: {[r.code for r in response.rejected_events]}")
    print(f"  memory episode: {validated.episode_id} status={validated.status}")
    print(f"  memory outbox drained: {drained} batches")
    return 0


if __name__ == "__main__":
    sys.exit(main())
