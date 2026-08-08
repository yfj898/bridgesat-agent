from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import psycopg

from .models import Skill, Student, StudentCreate


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

    def update_mastery(self, student_id: str, mastery: dict[Skill, float]) -> None:
        self.connection.execute(
            "UPDATE students SET mastery_json = %s WHERE id = %s",
            (
                json.dumps({key.value: value for key, value in mastery.items()}),
                student_id,
            ),
        )
        self.connection.commit()
