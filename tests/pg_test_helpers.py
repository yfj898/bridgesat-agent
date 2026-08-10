"""Shared PostgreSQL test cleanup for tenant-scoped fixtures."""

from __future__ import annotations

import uuid

import psycopg


# Delete dependent projections and delivery rows before the student root. The
# current schema has few declared foreign keys, but keeping the order explicit
# makes this safe if those relationships are enforced in the test database.
TENANT_CLEANUP_ORDER = (
    "memory_outbox",
    "student_deletions",
    "student_memory_facts",
    "learning_episodes",
    "intervention_stats",
    "agent_events",
    "misconception_evidence",
    "answer_attempts",
    "sync_conflicts",
    "learning_events",
    "session_branches",
    "session_items",
    "study_sessions",
    "study_plans",
    "student_skill_states",
    "devices",
    "student_tokens",
    "students",
)


def unique_tenant_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def cleanup_tenant(connection: psycopg.Connection, tenant_id: str) -> None:
    """Remove only rows owned by ``tenant_id`` from the shared test schema."""
    for table in TENANT_CLEANUP_ORDER:
        connection.execute(
            f"DELETE FROM {table} WHERE tenant_id = %s",
            (tenant_id,),
        )
    connection.commit()
