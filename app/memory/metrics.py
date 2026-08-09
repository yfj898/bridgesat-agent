"""Memory consistency monitoring (MEMORY_CONSISTENCY §13).

One snapshot of the nine required metrics: outbox health, index success and
latency, PostgreSQL vs indexed episode counts, deletion pending count, and
the memory fallback rate. PostgreSQL counts come from the authoritative
store; index counts come from the configured index when it exposes them.
"""

from __future__ import annotations

from typing import Any

import psycopg

from .outbox import OutboxRepository


def memory_consistency_metrics(
    connection: psycopg.Connection,
    index: Any | None = None,
    fallback: Any | None = None,
) -> dict:
    outbox = OutboxRepository(connection).consistency_metrics()
    sqlite_episodes = connection.execute(
        "SELECT COUNT(*) AS c FROM learning_episodes WHERE status = 'validated'"
    ).fetchone()["c"]
    deletion_pending = connection.execute(
        """
        SELECT COUNT(*) AS c FROM student_deletions
        WHERE state IN ('requested', 'sqlite_deleted', 'index_deletion_pending')
        """
    ).fetchone()["c"]
    indexed_outbox_rows = connection.execute(
        "SELECT COUNT(*) AS c FROM memory_outbox WHERE status = 'indexed'"
    ).fetchone()["c"]

    indexed_episodes = None
    if index is not None:
        indexed_episodes = _index_episode_count(index)

    success_rate = None
    if (
        outbox["outbox_pending_count"]
        or outbox["outbox_dead_letter_count"]
        or indexed_outbox_rows
    ):
        success_rate = 1.0 if outbox["outbox_dead_letter_count"] == 0 else 0.0

    fallback_metrics = getattr(fallback, "recall_metrics", lambda: None)()
    return {
        "outbox_pending_count": outbox["outbox_pending_count"],
        "outbox_oldest_age_seconds": outbox["outbox_oldest_age_seconds"],
        "outbox_dead_letter_count": outbox["outbox_dead_letter_count"],
        "memory_index_success_rate": success_rate,
        "memory_index_latency_ms": (
            fallback_metrics.get("memory_latency_ms") if fallback_metrics else None
        ),
        "sqlite_episode_count": sqlite_episodes,
        "indexed_episode_count": indexed_episodes,
        "deletion_pending_count": deletion_pending,
        "memory_fallback_rate": (
            fallback_metrics.get("memory_fallback_rate") if fallback_metrics else None
        ),
    }


def _index_episode_count(index: Any) -> int | None:
    count = getattr(index, "total_episode_count", None)
    if count is None:
        return None
    import asyncio

    return asyncio.run(count())