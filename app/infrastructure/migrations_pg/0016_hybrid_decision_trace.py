"""0016: hybrid decision trace (HYBRID_REASONING_INTEGRATION_PLAN section 22,
phase H7).

H7 (action changing) is enabled only by ``BRIDGESAT_HYBRID_ACTION_RANKING_ENABLED``.
When enabled, a verified model proposal may replace the deterministic action
in the sync response, but only through the bounded two-phase revalidation
path: Phase A commits the deterministic fallback agent event plus an
in-memory decision token, Phase B calls the model after the advisory lock is
released, Phase C opens a short revalidation transaction and only then
persists this additive trace binding the verified action to its source event.

The trace is the auditable decision record: durable action stays the
deterministic fallback (deterministic authority is never rewritten), the
trace documents which verified action was served instead. Writes happen only
when the action-ranking task is enabled; the table itself is inert schema
otherwise and is dropped-equivalent for rollback (H9 No-Go keeps H5 behavior
with this table simply unused).
"""
from __future__ import annotations

import psycopg


def migrate(connection: psycopg.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS hybrid_decision_trace (
            trace_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'tenant_demo',
            student_id TEXT NOT NULL,
            source_event_id TEXT NOT NULL,
            decision_token TEXT NOT NULL,
            fallback_action TEXT NOT NULL,
            verified_action TEXT NOT NULL,
            accepted_checks TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_hybrid_decision_trace_tenant "
        "ON hybrid_decision_trace (tenant_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_hybrid_decision_trace_source "
        "ON hybrid_decision_trace (tenant_id, source_event_id)"
    )
    connection.execute(
        "ALTER TABLE hybrid_decision_trace ENABLE ROW LEVEL SECURITY"
    )
    connection.execute(
        """
        CREATE POLICY tenant_isolation ON hybrid_decision_trace
        USING (tenant_id = current_setting('app.tenant_id', true))
        """
    )