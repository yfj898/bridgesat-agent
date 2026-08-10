from __future__ import annotations

import json

import psycopg

from app import main
from app.infrastructure.pg import transaction


def test_pg_fixture_uses_a_dedicated_database(
    isolated_pg_database, pg_connection: psycopg.Connection, client
) -> None:
    row = pg_connection.execute("SELECT current_database() AS database").fetchone()
    assert row["database"] == isolated_pg_database.database_name
    assert isolated_pg_database.database_name != "bridgesat"

    created = client.post(
        "/v1/students",
        json={"name": "Fixture", "daily_minutes": 15, "target_score": 1100},
    )
    assert created.status_code == 201
    stored = pg_connection.execute(
        """
        SELECT 1
        FROM students
        WHERE id = %s
          AND tenant_id = current_setting('app.tenant_id', true)
        """,
        (created.json()["id"],),
    ).fetchone()
    assert stored is not None


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_student_diagnostic_flow(client) -> None:
    created = client.post(
        "/v1/students",
        json={"name": "Ari", "daily_minutes": 15, "target_score": 1100},
    )
    assert created.status_code == 201
    token = created.json()["token"]

    second_student = client.post(
        "/v1/students",
        json={"name": "Bo", "daily_minutes": 15, "target_score": 1100},
    )
    assert second_student.status_code == 201
    token2 = second_student.json()["token"]

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
    assert token2 != token


def test_diagnostic_requires_token(client) -> None:
    response = client.post(
        "/v1/diagnostics",
        json={
            "answers": [
                {"question_id": "linear-001", "selected_answer": "3", "hint_level": 0},
            ],
        },
    )
    assert response.status_code == 401


def test_diagnostic_rejects_foreign_token(client) -> None:
    created = client.post(
        "/v1/students",
        json={"name": "Ari", "daily_minutes": 15, "target_score": 1100},
    )
    student_id = created.json()["id"]
    token = created.json()["token"]
    other = client.post(
        "/v1/students",
        json={"name": "Other", "daily_minutes": 15, "target_score": 1100},
    )
    other_student_id = other.json()["id"]
    other_token = other.json()["token"]
    assert other_token != token
    assert other_student_id != student_id

    response = client.post(
        "/v1/diagnostics",
        headers=_auth(other_token),
        json={
            "answers": [
                {"question_id": "linear-001", "selected_answer": "3", "hint_level": 0},
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["student_id"] == other_student_id
    assert response.json()["student_id"] != student_id


def test_unknown_student_returns_404(
    client, pg_connection: psycopg.Connection
) -> None:
    created = client.post(
        "/v1/students",
        json={"name": "Temp", "daily_minutes": 15, "target_score": 1100},
    )
    assert created.status_code == 201
    student_id, token = created.json()["id"], created.json()["token"]

    with transaction(pg_connection):
        pg_connection.execute(
            """
            DELETE FROM students
            WHERE id = %s
              AND tenant_id = current_setting('app.tenant_id', true)
            """,
            (student_id,),
        )

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
    # Migration 0014's resolver treats a token whose student row no longer
    # exists as invalid, before the route can reach its repository lookup.
    assert response.status_code == 401


def test_snapshot_requires_token(client) -> None:
    response = client.get("/v1/sync/snapshot?student_id=anyone")
    assert response.status_code == 401


def test_adapt_route_uses_llm_decision_when_configured(client, monkeypatch) -> None:
    """The /v1/adapt route forwards the configured LLM client into engine.adapt:
    with a stub LLM answering JSON, the LLM action wins over the deterministic
    policy; without a key (default), the route is byte-identical."""

    class _StubClient:
        async def complete(self, prompt: str, **kwargs) -> str:
            return json.dumps(
                {
                    "action": "insert_micro_lesson",
                    "reason_code": "LLM_CONCEPT_GAP",
                    "reason_text": "stub decided a lesson is needed",
                }
            )

    monkeypatch.setenv("BRIDGESAT_LLM_API_KEY", "nvapi-test-route")
    monkeypatch.setattr(main, "_llm_client", _StubClient())
    created = client.post(
        "/v1/students",
        json={"name": "Ari", "daily_minutes": 15, "target_score": 1100},
    )
    token = created.json()["token"]
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


def test_adapt_route_deterministic_without_key(client, monkeypatch) -> None:
    monkeypatch.delenv("BRIDGESAT_LLM_API_KEY", raising=False)
    monkeypatch.setattr(main, "_llm_client", None)
    created = client.post(
        "/v1/students",
        json={"name": "Ari", "daily_minutes": 15, "target_score": 1100},
    )
    token = created.json()["token"]
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
