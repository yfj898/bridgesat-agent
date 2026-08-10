from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import psycopg

from .models import Skill, Student, StudentCreate


class StudentWriteRejectedError(ValueError):
    """Raised when a learner write cannot target an active tenant student."""


class StudentRepository:
    """Tenant-scoped student store. Caller must have set app.tenant_id."""

    def __init__(self, connection: psycopg.Connection) -> None:
        self.connection = connection

    def create(self, payload: StudentCreate) -> Student:
        student = Student(
            id=str(uuid.uuid4()),
            name=payload.name,
            daily_minutes=payload.daily_minutes,
            target_score=payload.target_score,
            mastery={skill: 0.5 for skill in Skill},
        )
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            """
            INSERT INTO students (
                id, tenant_id, name, daily_minutes, target_score, mastery_json,
                status, created_at, updated_at
            ) VALUES (%s, current_setting('app.tenant_id'), %s, %s, %s, %s, 'active', %s, %s)
            """,
            (
                student.id,
                student.name,
                student.daily_minutes,
                student.target_score,
                json.dumps({key.value: value for key, value in student.mastery.items()}),
                now,
                now,
            ),
        )
        self.connection.commit()
        return student

    def get(self, student_id: str) -> Student | None:
        row = self.connection.execute(
            "SELECT * FROM students WHERE id = %s",
            (student_id,),
        ).fetchone()
        return self._student_from_row(row)

    def get_for_update(self, student_id: str) -> Student | None:
        """Read an active tenant student while retaining its row lock.

        The caller owns the surrounding transaction. This is intentionally
        separate from ``get`` so a read-modify-write operation cannot release
        the row lock before its deterministic computation finishes.
        """
        row = self.connection.execute(
            """
            SELECT *
            FROM students
            WHERE id = %s
              AND tenant_id = current_setting('app.tenant_id', true)
              AND status = 'active'
            FOR UPDATE
            """,
            (student_id,),
        ).fetchone()
        return self._student_from_row(row)

    @staticmethod
    def _student_from_row(row: dict | None) -> Student | None:
        if row is None:
            return None
        mastery_payload = json.loads(row["mastery_json"])
        return Student(
            id=row["id"],
            name=row["name"],
            daily_minutes=row["daily_minutes"],
            target_score=row["target_score"],
            mastery={Skill(key): value for key, value in mastery_payload.items()},
        )

    def update_mastery(
        self,
        student_id: str,
        mastery: dict[Skill, float],
        *,
        commit: bool = True,
    ) -> None:
        """Update an active tenant row.

        Direct callers retain the historical commit-on-success behavior. A
        caller that already owns a transaction must pass ``commit=False`` so
        this method cannot commit unrelated work or release its row lock.
        """
        cursor = self.connection.execute(
            """
            UPDATE students
            SET mastery_json = %s
            WHERE id = %s
              AND tenant_id = current_setting('app.tenant_id', true)
              AND status = 'active'
            """,
            (
                json.dumps({key.value: value for key, value in mastery.items()}),
                student_id,
            ),
        )
        if cursor.rowcount == 0:
            if commit:
                self.connection.rollback()
            raise StudentWriteRejectedError(
                f"Student {student_id} is not active in the current tenant"
            )
        if commit:
            self.connection.commit()
