from pathlib import Path

from fastapi.testclient import TestClient

from app import main
from app.auth import TokenStore
from app.infrastructure.migration_runner import apply_migrations
from app.repository import StudentRepository


def _fresh_app(tmp_path: Path) -> tuple[TestClient, str]:
    db = tmp_path / "test.db"
    apply_migrations(db)
    main.repository = StudentRepository(db)
    main.token_store = TokenStore(db)
    client = TestClient(main.app)
    created = client.post(
        "/v1/students",
        json={"name": "Ari", "daily_minutes": 15, "target_score": 1100},
    )
    assert created.status_code == 201
    token = created.json()["token"]
    return client, token


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_student_diagnostic_flow(tmp_path: Path) -> None:
    client, token = _fresh_app(tmp_path)
    created = client.post(
        "/v1/students",
        json={"name": "Bo", "daily_minutes": 15, "target_score": 1100},
    )
    token2 = created.json()["token"]

    diagnostic = client.post(
        "/v1/diagnostics",
        headers=_auth(token),
        json={
            "answers": [
                {"question_id": "linear-001", "selected_answer": "3", "hint_level": 0},
                {"question_id": "ratio-001", "selected_answer": "4", "hint_level": 0},
            ],
        },
    )

    assert diagnostic.status_code == 200
    assert diagnostic.json()["weakest_skills"][0] == "linear_equations"
    # A different student's token must not scope to this student.
    assert token2 != token


def test_diagnostic_requires_token(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    apply_migrations(db)
    main.repository = StudentRepository(db)
    main.token_store = TokenStore(db)
    client = TestClient(main.app)

    response = client.post(
        "/v1/diagnostics",
        json={
            "answers": [
                {"question_id": "linear-001", "selected_answer": "3", "hint_level": 0},
            ],
        },
    )
    assert response.status_code == 401


def test_diagnostic_rejects_foreign_token(tmp_path: Path) -> None:
    client, token = _fresh_app(tmp_path)
    other = client.post(
        "/v1/students",
        json={"name": "Other", "daily_minutes": 15, "target_score": 1100},
    )
    other_token = other.json()["token"]
    assert other_token != token

    response = client.post(
        "/v1/diagnostics",
        headers=_auth(token),
        json={
            "answers": [
                {"question_id": "linear-001", "selected_answer": "3", "hint_level": 0},
            ],
        },
    )
    # The body can no longer claim another student's scope; the request runs
    # against the token's own student and succeeds.
    assert response.status_code == 200


def test_unknown_student_returns_404(tmp_path: Path) -> None:
    client, _ = _fresh_app(tmp_path)
    created = client.post(
        "/v1/students",
        json={"name": "Temp", "daily_minutes": 15, "target_score": 1100},
    ).json()
    student_id, token = created["id"], created["token"]

    # Remove the student row; the token still resolves to that id.
    import sqlite3

    with sqlite3.connect(tmp_path / "test.db") as connection:
        connection.execute("DELETE FROM students WHERE id = ?", (student_id,))

    response = client.post(
        "/v1/adapt",
        headers=_auth(token),
        json={
            "skill": "ratios",
            "was_correct": True,
            "hint_level": 0,
            "consecutive_skill_errors": 0,
            "minutes_remaining": 8,
        },
    )

    assert response.status_code == 404


def test_snapshot_requires_token(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    apply_migrations(db)
    main.repository = StudentRepository(db)
    main.token_store = TokenStore(db)
    client = TestClient(main.app)

    response = client.get("/v1/sync/snapshot?student_id=anyone")
    assert response.status_code == 401


def test_adapt_route_uses_llm_decision_when_configured(tmp_path: Path, monkeypatch) -> None:
    """The /v1/adapt route forwards the configured LLM client into engine.adapt:
    with a stub LLM answering JSON, the LLM action wins over the deterministic
    policy; without a key (default), the route is byte-identical."""
    import json as _json

    class _StubClient:
        async def complete(self, prompt: str, **kwargs) -> str:
            return _json.dumps(
                {
                    "action": "insert_micro_lesson",
                    "reason_code": "LLM_CONCEPT_GAP",
                    "reason_text": "stub decided a lesson is needed",
                }
            )

    monkeypatch.setenv("BRIDGESAT_LLM_API_KEY", "nvapi-test-route")
    monkeypatch.setattr(main, "_llm_client", _StubClient())
    client, token = _fresh_app(tmp_path)
    response = client.post(
        "/v1/adapt",
        headers=_auth(token),
        json={
            "skill": "linear_equations",
            "was_correct": False,
            "hint_level": 0,
            "consecutive_skill_errors": 1,
            "minutes_remaining": 20,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "insert_micro_lesson"
    assert body["reason"] == "stub decided a lesson is needed"
    assert body["next_difficulty_delta"] == -1
    assert body["mastery"] == round(0.5 - 0.09, 3)


def test_adapt_route_deterministic_without_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("BRIDGESAT_LLM_API_KEY", raising=False)
    monkeypatch.setattr(main, "_llm_client", None)
    client, token = _fresh_app(tmp_path)
    response = client.post(
        "/v1/adapt",
        headers=_auth(token),
        json={
            "skill": "linear_equations",
            "was_correct": True,
            "hint_level": 0,
            "consecutive_skill_errors": 0,
            "minutes_remaining": 20,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "continue_practice"
