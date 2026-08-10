#!/usr/bin/env python3
"""Verify derived memory-index parity against authoritative PostgreSQL.

Usage:
    python scripts/verify_memory_parity.py [--db DSN] [--tenant TENANT_ID]
                                           [--student STUDENT_ID] [--allow-empty]

The check is read-only. It populates a fresh in-memory adapter directly from
tenant-scoped PostgreSQL episodes and facts, compares counts and episode recall,
and reads (but never changes) the memory outbox metrics.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import json
import os
import re
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.infrastructure import pg
from app.infrastructure.migration_runner import SCHEMA_VERSION
from app.memory.mnemis_stub import InMemoryMnemisIndex


def _default_tenant() -> str:
    return os.getenv("BRIDGESAT_DEFAULT_TENANT", "tenant_demo")


TENANT_TABLES = (
    "students",
    "student_tokens",
    "student_skill_states",
    "study_plans",
    "study_sessions",
    "session_items",
    "answer_attempts",
    "learning_events",
    "agent_events",
    "misconception_evidence",
    "learning_episodes",
    "student_memory_facts",
    "intervention_stats",
    "devices",
    "session_branches",
    "sync_conflicts",
    "memory_outbox",
    "student_deletions",
    "legacy_mastery_imports",
)

REQUIRED_COLUMNS = {
    "students": {"id", "tenant_id"},
    "learning_episodes": {
        "tenant_id",
        "episode_id",
        "student_id",
        "session_id",
        "skill",
        "misconception",
        "intervention",
        "status",
        "created_at",
        "updated_at",
        "outcome_json",
        "effectiveness",
        "evidence_event_ids_json",
        "summary",
        "confidence",
    },
    "student_memory_facts": {
        "tenant_id",
        "fact_id",
        "student_id",
        "category",
        "normalized_key",
        "fact_text",
        "confidence",
        "supporting_episode_ids_json",
        "contradicting_episode_ids_json",
        "evidence_count",
        "contradiction_count",
        "status",
        "first_observed_at",
        "last_observed_at",
        "version",
    },
    "memory_outbox": {
        "tenant_id",
        "outbox_id",
        "student_id",
        "aggregate_type",
        "aggregate_id",
        "operation",
        "payload_json",
        "idempotency_key",
        "status",
        "attempt_count",
        "next_attempt_at",
        "last_error",
        "created_at",
        "completed_at",
        "claim_token",
    },
    "legacy_mastery_imports": {
        "import_id",
        "student_id",
        "mastery_json",
        "imported_at",
        "tenant_id",
    },
}


def _strip_outer_parentheses(expression: str) -> str:
    while expression.startswith("(") and expression.endswith(")"):
        depth = 0
        wraps_entire_expression = True
        for index, character in enumerate(expression):
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0 and index != len(expression) - 1:
                    wraps_entire_expression = False
                    break
        if not wraps_entire_expression or depth != 0:
            break
        expression = expression[1:-1]
    return expression


def _normalize_policy_expression(expression: object | None) -> str | None:
    if expression is None:
        return None
    compact = "".join(str(expression).lower().split())
    compact = compact.replace("'app.tenant_id'::text", "'app.tenant_id'")
    compact = re.sub(r"\(('app\.tenant_id')\)", r"\1", compact)
    compact = compact.replace("(tenant_id)", "tenant_id")
    return _strip_outer_parentheses(compact)


def _is_exact_tenant_predicate(expression: object | None) -> bool:
    return _normalize_policy_expression(expression) == (
        "tenant_id=current_setting('app.tenant_id',true)"
    )


def _require_schema(connection: psycopg.Connection) -> None:
    row = connection.execute(
        """
        SELECT to_regclass('public.schema_migrations') AS schema_migrations,
               to_regclass('public.students') AS students,
               to_regclass('public.learning_episodes') AS learning_episodes,
               to_regclass('public.student_memory_facts') AS student_memory_facts,
               to_regclass('public.memory_outbox') AS memory_outbox
        """
    ).fetchone()
    missing = [
        name
        for name in (
            "schema_migrations",
            "students",
            "learning_episodes",
            "student_memory_facts",
            "memory_outbox",
        )
        if row[name] is None
    ]
    if missing:
        raise RuntimeError(
            "PostgreSQL schema is not migrated; missing " + ", ".join(missing)
        )

    version = connection.execute(
        "SELECT MAX(version) AS version FROM schema_migrations"
    ).fetchone()["version"]
    if version != SCHEMA_VERSION:
        raise RuntimeError(
            f"PostgreSQL schema version {version or 0} is not supported; "
            f"expected {SCHEMA_VERSION}"
        )

    missing_columns: dict[str, list[str]] = {}
    for table, required in REQUIRED_COLUMNS.items():
        columns = {
            item["column_name"]
            for item in connection.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = %s
                """,
                (table,),
            ).fetchall()
        }
        absent = sorted(required - columns)
        if absent:
            missing_columns[table] = absent
    if missing_columns:
        details = "; ".join(
            f"{table}: {', '.join(columns)}"
            for table, columns in missing_columns.items()
        )
        raise RuntimeError(f"PostgreSQL schema is missing required columns: {details}")

    role = connection.execute(
        """
        SELECT r.rolsuper,
               r.rolbypassrls,
               EXISTS (
                   SELECT 1
                   FROM pg_class AS c
                   JOIN pg_namespace AS n ON n.oid = c.relnamespace
                   WHERE n.nspname = 'public'
                     AND c.relname = ANY(%s)
                     AND pg_has_role(current_user, c.relowner, 'USAGE')
               ) AS owns_tenant_table
        FROM pg_roles AS r
        WHERE r.rolname = current_user
        """,
        (list(TENANT_TABLES),),
    ).fetchone()
    if (
        role is None
        or role["rolsuper"]
        or role["rolbypassrls"]
        or role["owns_tenant_table"]
    ):
        raise RuntimeError(
            "Parity requires a non-superuser, non-RLS-bypass, non-owner "
            "PostgreSQL application role"
        )

    rls_rows = connection.execute(
        """
        SELECT c.relname,
               c.relrowsecurity,
               p.polname,
               p.polpermissive,
               p.polcmd,
               p.polroles,
               pg_get_expr(p.polqual, p.polrelid) AS using_predicate,
               pg_get_expr(p.polwithcheck, p.polrelid) AS check_predicate
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        LEFT JOIN pg_policy AS p ON p.polrelid = c.oid
        WHERE n.nspname = 'public' AND c.relname = ANY(%s)
        ORDER BY c.relname, p.polname
        """,
        (list(TENANT_TABLES),),
    ).fetchall()
    rls_by_table: dict[str, dict] = {}
    for item in rls_rows:
        table = item["relname"]
        table_state = rls_by_table.setdefault(
            table,
            {"relrowsecurity": item["relrowsecurity"], "policies": []},
        )
        if item["polname"] is not None:
            table_state["policies"].append(item)
    rls_missing = [table for table in TENANT_TABLES if table not in rls_by_table]
    rls_disabled = [
        table for table, item in rls_by_table.items() if not item["relrowsecurity"]
    ]
    policy_missing = [
        table
        for table, item in rls_by_table.items()
        if not any(policy["polname"] == "tenant_isolation" for policy in item["policies"])
    ]
    policy_invalid = [
        table
        for table, item in rls_by_table.items()
        if len(item["policies"]) != 1
        or item["policies"][0]["polname"] != "tenant_isolation"
        or item["policies"][0]["polpermissive"] is not True
        or item["policies"][0]["polcmd"] != "*"
        or list(item["policies"][0]["polroles"] or []) != [0]
        or not _is_exact_tenant_predicate(
            item["policies"][0]["using_predicate"]
        )
        or not _is_exact_tenant_predicate(
            item["policies"][0]["check_predicate"]
            or item["policies"][0]["using_predicate"]
        )
    ]
    if rls_missing or rls_disabled or policy_missing or policy_invalid:
        details = []
        if rls_missing:
            details.append("missing tables: " + ", ".join(rls_missing))
        if rls_disabled:
            details.append("RLS disabled: " + ", ".join(sorted(rls_disabled)))
        if policy_missing:
            details.append("tenant policy missing: " + ", ".join(sorted(policy_missing)))
        if policy_invalid:
            details.append(
                "tenant policy predicate invalid: "
                + ", ".join(sorted(policy_invalid))
            )
        raise RuntimeError(
            "PostgreSQL tenant-isolation precondition failed ("
            + "; ".join(details)
            + ")"
        )


def _validated_episode_rows(
    connection: psycopg.Connection, student_id: str
) -> list[dict]:
    return connection.execute(
        """
        SELECT * FROM learning_episodes
        WHERE tenant_id = current_setting('app.tenant_id', true)
          AND student_id = %s
          AND status = 'validated'
        ORDER BY created_at, episode_id
        """,
        (student_id,),
    ).fetchall()


def _authoritative_counts(
    connection: psycopg.Connection, student_id: str
) -> dict[str, int]:
    """Count authoritative PG episodes and semantic facts without a limit."""
    episodes = connection.execute(
        """
        SELECT COUNT(*) AS total FROM learning_episodes
        WHERE tenant_id = current_setting('app.tenant_id', true)
          AND student_id = %s AND status = 'validated'
        """,
        (student_id,),
    ).fetchone()["total"]
    facts = connection.execute(
        """
        SELECT COUNT(*) AS total FROM student_memory_facts
        WHERE tenant_id = current_setting('app.tenant_id', true)
          AND student_id = %s
        """,
        (student_id,),
    ).fetchone()["total"]
    return {"episodes": int(episodes), "facts": int(facts)}


def _payload(row: dict) -> dict:
    payload = dict(row)
    for field in (
        "outcome_json",
        "evidence_event_ids_json",
        "supporting_episode_ids_json",
        "contradicting_episode_ids_json",
    ):
        if field in payload and isinstance(payload[field], str):
            payload[field.removesuffix("_json")] = json.loads(payload[field] or "[]")
    return payload


async def _populate_index_and_check_recall(
    connection: psycopg.Connection,
    index: InMemoryMnemisIndex,
    student_id: str,
) -> bool:
    episode_rows = _validated_episode_rows(connection, student_id)
    fact_rows = connection.execute(
        """
        SELECT * FROM student_memory_facts
        WHERE tenant_id = current_setting('app.tenant_id', true)
          AND student_id = %s
        ORDER BY fact_id
        """,
        (student_id,),
    ).fetchall()

    for row in episode_rows:
        await index.upsert_episode(
            _payload(row),
            f"parity:episode:{student_id}:{row['episode_id']}",
        )
    for row in fact_rows:
        await index.upsert_fact(
            _payload(row),
            f"parity:fact:{student_id}:{row['fact_id']}",
        )

    top_k = max(1, len(episode_rows))
    for row in episode_rows:
        hits = await index.recall_similar(
            {
                "student_id": student_id,
                "skill": row["skill"],
                "misconception": row["misconception"],
            },
            top_k=top_k,
            min_confidence=0.0,
        )
        recalled_ids = {
            supporting[0]
            for hit in hits
            if (supporting := hit.get("supporting_episode_ids"))
        }
        if row["episode_id"] not in recalled_ids:
            return False
    return True


async def _index_counts(index: InMemoryMnemisIndex, student_id: str) -> dict[str, int]:
    episodes = await index.count_episodes(student_id)
    facts = await index.count_facts(student_id)
    return {"episodes": episodes, "facts": facts}


def _outbox_metrics(connection: psycopg.Connection) -> dict[str, object]:
    timestamp = datetime.now(UTC)
    pending = connection.execute(
        """
        SELECT COUNT(*) AS total FROM memory_outbox
        WHERE tenant_id = current_setting('app.tenant_id', true)
          AND status = 'pending'
        """
    ).fetchone()["total"]
    dead = connection.execute(
        """
        SELECT COUNT(*) AS total FROM memory_outbox
        WHERE tenant_id = current_setting('app.tenant_id', true)
          AND status = 'dead_letter'
        """
    ).fetchone()["total"]
    oldest = connection.execute(
        """
        SELECT created_at FROM memory_outbox
        WHERE tenant_id = current_setting('app.tenant_id', true)
          AND status = 'pending'
        ORDER BY created_at ASC
        LIMIT 1
        """
    ).fetchone()
    oldest_age = None
    if oldest is not None:
        created = datetime.fromisoformat(oldest["created_at"])
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        oldest_age = max(0.0, (timestamp - created).total_seconds())
    return {
        "outbox_pending_count": pending,
        "outbox_dead_letter_count": dead,
        "outbox_oldest_age_seconds": oldest_age,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None, help="PostgreSQL application DSN")
    parser.add_argument("--tenant", default=_default_tenant())
    parser.add_argument("--student", default=None)
    parser.add_argument("--allow-empty", action="store_true")
    args = parser.parse_args()

    target = args.db or pg.dsn()
    connection = pg.connect(target)
    try:
        # pg.connect validates the role with a catalog SELECT; start parity's
        # transaction only after clearing that validation transaction.
        connection.rollback()
        connection.execute("BEGIN READ ONLY")
        pg.assert_safe_app_role(connection)
        connection.execute(
            "SELECT set_config('app.tenant_id', %s, true)",
            (args.tenant,),
        )
        try:
            _require_schema(connection)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if args.student:
            selected = connection.execute(
                """
                SELECT id FROM students
                WHERE tenant_id = current_setting('app.tenant_id', true)
                  AND id = %s
                """,
                (args.student,),
            ).fetchone()
            if selected is None:
                print(
                    f"Student {args.student} is not present in the selected tenant",
                    file=sys.stderr,
                )
                return 2
            students = [args.student]
        else:
            students = [
                row["id"]
                for row in connection.execute(
                    """
                    SELECT id FROM students
                    WHERE tenant_id = current_setting('app.tenant_id', true)
                    ORDER BY id
                    """
                ).fetchall()
            ]
        if not students and not args.allow_empty:
            print(
                "No students selected; pass --student or use --allow-empty",
                file=sys.stderr,
            )
            return 2

        index = InMemoryMnemisIndex()
        rows = []
        ok = True
        for student_id in students:
            authoritative = _authoritative_counts(connection, student_id)
            recall_ok = asyncio.run(
                _populate_index_and_check_recall(connection, index, student_id)
            )
            indexed = asyncio.run(_index_counts(index, student_id))
            match = authoritative == indexed and recall_ok
            ok = ok and match
            rows.append(
                {
                    "student_id": student_id,
                    # Keep the historical report key for downstream report
                    # readers; its value is authoritative PG state.
                    "sqlite": authoritative,
                    "indexed": indexed,
                    "parity": "ok" if match else "MISMATCH",
                }
            )

        report = {
            "parity": "ok" if ok else "MISMATCH",
            "students": rows,
            "outbox": _outbox_metrics(connection),
        }
        print(json.dumps(report, indent=2))
        return 0 if ok else 1
    finally:
        pg.quiet_close(connection)


if __name__ == "__main__":
    sys.exit(main())
