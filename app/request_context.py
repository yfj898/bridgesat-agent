"""Request-scoped PostgreSQL and tenant context."""

from __future__ import annotations

import os
from collections.abc import Callable

import psycopg
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from .auth import TokenStore

ConnectionFactory = Callable[[], psycopg.Connection]
DEFAULT_TENANT = "tenant_demo"


def request_connection(request: Request) -> psycopg.Connection:
    """Return the connection opened for the current request."""
    return request.state.connection


def request_token_store(request: Request) -> TokenStore:
    """Return the token store bound to the current request connection."""
    return request.state.token_store


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token or None


def _requires_database(request: Request) -> bool:
    path = request.url.path
    # Only stateful /v1 routes need a connection. Health, questions, content
    # packs, and every non-/v1 path are filesystem/static/PWA-only paths.
    if path == "/health" or path == "/v1/questions":
        return False
    if path == "/v1/content-packs" or path.startswith("/v1/content-packs/"):
        return False
    return path.startswith("/v1/")


class TenantContextMiddleware(BaseHTTPMiddleware):
    """Open, tenant-scope, and clean up one app-role connection per request."""

    def __init__(self, app, connection_factory: ConnectionFactory) -> None:
        super().__init__(app)
        self.connection_factory = connection_factory

    async def dispatch(self, request: Request, call_next):
        if not _requires_database(request):
            return await call_next(request)

        connection = self.connection_factory()
        request.state.connection = connection
        request.state.token_store = TokenStore(connection)

        try:
            tenant_id = self._tenant_for_request(request)
            connection.execute(
                "SELECT set_config('app.tenant_id', %s, false)",
                (tenant_id,),
            )
            connection.commit()
            # Request DB routes return materialized JSON responses; no current
            # route streams or runs background work that reads request.state
            # after this await, so BaseHTTPMiddleware cleanup is safe here.
            return await call_next(request)
        finally:
            try:
                connection.rollback()
            finally:
                connection.close()

    @staticmethod
    def _tenant_for_request(request: Request) -> str:
        token = _bearer_token(request.headers.get("Authorization"))
        if token is not None:
            resolved = request_token_store(request).resolve_tenant(token)
            if resolved is not None:
                return resolved[0]
        return os.getenv("BRIDGESAT_DEFAULT_TENANT") or DEFAULT_TENANT
