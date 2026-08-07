from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from .models import Skill, Student, StudentCreate


class StudentRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS students (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    daily_minutes INTEGER NOT NULL,
                    target_score INTEGER NOT NULL,
                    mastery_json TEXT NOT NULL
                )
                """
            )

    def create(self, payload: StudentCreate) -> Student:
        student = Student(
            id=str(uuid.uuid4()),
            name=payload.name,
            daily_minutes=payload.daily_minutes,
            target_score=payload.target_score,
            mastery={skill: 0.5 for skill in Skill},
        )
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO students VALUES (?, ?, ?, ?, ?)",
                (
                    student.id,
                    student.name,
                    student.daily_minutes,
                    student.target_score,
                    json.dumps({key.value: value for key, value in student.mastery.items()}),
                ),
            )
        return student

    def get(self, student_id: str) -> Student | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM students WHERE id = ?",
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
        with self._connect() as connection:
            connection.execute(
                "UPDATE students SET mastery_json = ? WHERE id = ?",
                (
                    json.dumps({key.value: value for key, value in mastery.items()}),
                    student_id,
                ),
            )
