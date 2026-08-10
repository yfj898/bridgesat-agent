"""Scoped bearer-token authentication with tenant resolution."""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime

import psycopg
from fastapi import Header, HTTPException, Request, status

TOKEN_BYTES = 32


class TokenStore:
    def __init__(self, connection: psycopg.Connection) -> None:
        self.connection = connection

    def issue(
        self,
        student_id: str,
        *,
        device_bound_name: str | None = None,
    ) -> str:
        token = secrets.token_urlsafe(TOKEN_BYTES)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            """
            INSERT INTO student_tokens (
                token_id, tenant_id, student_id, token_hash, device_bound_name,
                created_at
            ) VALUES (%s, current_setting('app.tenant_id'), %s, %s, %s, %s)
            """,
            (
                f"tok_{secrets.token_hex(8)}",
                student_id,
                token_hash,
                device_bound_name,
                now,
            ),
        )
        self.connection.commit()
        return token

    def resolve(self, token: str) -> str | None:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        row = self.connection.execute(
            "SELECT student_id FROM resolve_token(%s)",
            (token_hash,),
        ).fetchone()
        return row["student_id"] if row is not None else None

    def resolve_tenant(self, token: str) -> tuple[str, str] | None:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        row = self.connection.execute(
            "SELECT tenant_id, student_id FROM resolve_token(%s)",
            (token_hash,),
        ).fetchone()
        return (row["tenant_id"], row["student_id"]) if row is not None else None

    def verify(self, student_id: str, token: str) -> bool:
        return self.resolve(token) == student_id

    def revoke(self, token: str) -> None:
        self.connection.execute(
            "UPDATE student_tokens SET revoked_at = %s WHERE token_hash = %s",
            (
                datetime.now(UTC).isoformat(),
                hashlib.sha256(token.encode("utf-8")).hexdigest(),
            ),
        )
        self.connection.commit()


def resolve_tenant(store: TokenStore, token: str) -> tuple[str, str] | None:
    return store.resolve_tenant(token)


def require_student(
    request: Request,
    authorization: str | None = Header(default=None),
) -> str:
    """FastAPI dependency: parse `Authorization: Bearer <token>` and return
    the authenticated student_id (401 on missing/invalid token).
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
    student_id = request.state.token_store.resolve(token)
    if student_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked token",
        )
    return student_id
