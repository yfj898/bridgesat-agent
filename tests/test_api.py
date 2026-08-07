from pathlib import Path

from fastapi.testclient import TestClient

from app import main
from app.repository import StudentRepository


def test_student_diagnostic_flow(tmp_path: Path) -> None:
    main.repository = StudentRepository(tmp_path / "test.db")
    client = TestClient(main.app)

    created = client.post(
        "/v1/students",
        json={"name": "Ari", "daily_minutes": 15, "target_score": 1100},
    )
    assert created.status_code == 201
    student_id = created.json()["id"]

    diagnostic = client.post(
        "/v1/diagnostics",
        json={
            "student_id": student_id,
            "answers": [
                {"question_id": "linear-001", "selected_answer": "3", "hint_level": 0},
                {"question_id": "ratio-001", "selected_answer": "4", "hint_level": 0},
            ],
        },
    )

    assert diagnostic.status_code == 200
    assert diagnostic.json()["weakest_skills"][0] == "linear_equations"


def test_unknown_student_returns_404(tmp_path: Path) -> None:
    main.repository = StudentRepository(tmp_path / "test.db")
    client = TestClient(main.app)

    response = client.post(
        "/v1/adapt",
        json={
            "student_id": "missing",
            "skill": "ratios",
            "was_correct": True,
            "hint_level": 0,
            "consecutive_skill_errors": 0,
            "minutes_remaining": 8,
        },
    )

    assert response.status_code == 404
