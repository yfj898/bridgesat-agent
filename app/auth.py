"""Scoped bearer-token authentication for the BridgeSAT API.

Implements THREAT_MODEL section 10 item 1 (object-level isolation) and
plan section 7: `POST /v1/students` issues a one-time random bearer token,
the database stores only its SHA-256 hash, and every protected endpoint
derives the student scope from the token instead of trusting the request
body or query string.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Header, HTTPException, status

TOKEN_BYTES = 32


class TokenStore:
    """Issue and verify one-time bearer tokens against `student_tokens`.

    The table is created by migration 0003; callers must ensure migrations
    are applied before first use (main.py applies them at import time).
    """

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def issue(
        self,
        student_id: str,
        *,
        device_bound_name: str | None = None,
    ) -> str:
        """Create a token, store only its hash, and return the plaintext once."""
        token = secrets.token_urlsafe(TOKEN_BYTES)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO student_tokens (
                    token_id, student_id, token_hash, device_bound_name,
                    created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    f"tok_{secrets.token_hex(8)}",
                    student_id,
                    token_hash,
                    device_bound_name,
                    now,
                ),
            )
        return token

    def resolve(self, token: str) -> str | None:
        """Return the student_id for an active token, or None."""
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT student_id FROM student_tokens
                WHERE token_hash = ? AND revoked_at IS NULL
                """,
                (token_hash,),
            ).fetchone()
        return row["student_id"] if row is not None else None

    def verify(self, student_id: str, token: str) -> bool:
        return self.resolve(token) == student_id

    def revoke(self, token: str) -> None:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            connection.execute(
                "UPDATE student_tokens SET revoked_at = ? WHERE token_hash = ?",
                (datetime.now(UTC).isoformat(), token_hash),
            )


def require_student(
    authorization: str | None = Header(default=None),
) -> str:
    """FastAPI dependency: parse `Authorization: Bearer <token>` and return
    the authenticated student_id (401 on missing/invalid token).

    Lazy-imports the app-level token store to avoid a circular import and to
    keep the store replaceable in tests (same pattern as `main.repository`).
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token required",
        )
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token required",
        )
    from app.main import token_store

    student_id = token_store.resolve(token)
    if student_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked token",
        )
    return student_id
