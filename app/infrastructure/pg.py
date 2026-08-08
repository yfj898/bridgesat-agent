"""PostgreSQL connection layer for BridgeSAT.

All storage modules get connections from here; nothing else talks to the
database directly. Connections use psycopg3 with dict-row access and the
public schema by default.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

DEFAULT_DSN = "postgresql://bridgesat:bridgesat@localhost:5432/bridgesat"


def dsn() -> str:
    return os.getenv("BRIDGESAT_DB", DEFAULT_DSN)


def connect(target: str | None = None) -> psycopg.Connection:
    """Open a new connection with dict-row access. Caller closes it."""
    return psycopg.connect(
        target or dsn(),
        row_factory=dict_row,
        autocommit=False,
        options="-c search_path=public",
    )


@contextmanager
def transaction(connection: psycopg.Connection) -> Iterator[psycopg.Connection]:
    """Commit on success, rollback and re-raise on any exception.

    psycopg3 opens a transaction implicitly on first execute; we only need
    explicit commit/rollback control.
    """
    try:
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def database_version(connection: psycopg.Connection) -> int:
    row = connection.execute(
        "SELECT MAX(version) AS version FROM schema_migrations"
    ).fetchone()
    return int(row["version"]) if row and row["version"] is not None else 0
