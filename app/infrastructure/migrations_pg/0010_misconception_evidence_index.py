"""0010: add the misconception evidence aggregation lookup index."""
from __future__ import annotations

import psycopg


def migrate(connection: psycopg.Connection) -> None:
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_misconception_evidence_lookup "
        "ON public.misconception_evidence "
        "(tenant_id, student_id, skill, misconception, item_id)"
    )
