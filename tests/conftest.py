from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import uuid

import psycopg
from psycopg import sql
import pytest
from fastapi.testclient import TestClient

from app import question_bank
from app.infrastructure import pg
from app.infrastructure.migration_runner import migrate_database

FIXTURES_DIR = Path(__file__).parent / "fixtures"
PACKS_ROOT = FIXTURES_DIR / "packs"


@dataclass
class IsolatedPostgres:
    database_name: str
    admin_dsn: str
    app_dsn: str
    tenant_id: str
    app_connections: list[psycopg.Connection] = field(default_factory=list)

    def connect_app(self) -> psycopg.Connection:
        connection = pg.connect(self.app_dsn)
        self.app_connections.append(connection)
        return connection

    def close_app_connections(self) -> None:
        for connection in reversed(self.app_connections):
            pg.quiet_close(connection)


def _drop_database(maintenance_dsn: str, database_name: str) -> None:
    maintenance = None
    try:
        maintenance = psycopg.connect(maintenance_dsn, autocommit=True)
        maintenance.execute(
            sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                sql.Identifier(database_name)
            )
        )
    finally:
        pg.quiet_close(maintenance)


@pytest.fixture()
def isolated_pg_database() -> IsolatedPostgres:
    database_name = f"bridgesat_task6_{uuid.uuid4().hex}"
    maintenance_dsn = psycopg.conninfo.make_conninfo(
        pg.admin_dsn(), dbname="postgres"
    )
    admin_dsn = psycopg.conninfo.make_conninfo(
        pg.admin_dsn(), dbname=database_name
    )
    app_dsn = psycopg.conninfo.make_conninfo(
        pg.dsn(), dbname=database_name
    )
    resource = IsolatedPostgres(
        database_name=database_name,
        admin_dsn=admin_dsn,
        app_dsn=app_dsn,
        tenant_id=f"task6_{uuid.uuid4().hex}",
    )
    maintenance = None
    admin = None
    database_created = False
    try:
        maintenance = psycopg.connect(maintenance_dsn, autocommit=True)
        maintenance.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
        )
        database_created = True
        admin = pg.connect_admin(admin_dsn)
        migrate_database(admin)
        yield resource
    finally:
        resource.close_app_connections()
        pg.quiet_close(admin)
        pg.quiet_close(maintenance)
        if database_created:
            _drop_database(maintenance_dsn, database_name)


@pytest.fixture()
def pg_tenant(
    isolated_pg_database: IsolatedPostgres,
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    monkeypatch.setenv("BRIDGESAT_DB", isolated_pg_database.app_dsn)
    monkeypatch.setenv("BRIDGESAT_ADMIN_DB", isolated_pg_database.admin_dsn)
    monkeypatch.setenv("BRIDGESAT_DEFAULT_TENANT", isolated_pg_database.tenant_id)
    return isolated_pg_database.tenant_id


@pytest.fixture()
def pg_connection(
    isolated_pg_database: IsolatedPostgres,
    pg_tenant: str,
) -> psycopg.Connection:
    connection = None
    try:
        connection = isolated_pg_database.connect_app()
        connection.execute(
            "SELECT set_config('app.tenant_id', %s, false)",
            (pg_tenant,),
        )
        connection.commit()
        yield connection
    finally:
        pg.quiet_close(connection)


@pytest.fixture()
def db(pg_connection: psycopg.Connection) -> psycopg.Connection:
    return pg_connection


@pytest.fixture()
def pg_app(
    isolated_pg_database: IsolatedPostgres,
    pg_tenant: str,
):
    from app.main import create_app

    return create_app(
        connection_factory=isolated_pg_database.connect_app,
        run_migrations=False,
    )


@pytest.fixture()
def client(pg_app):
    with TestClient(pg_app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def use_fixture_packs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRIDGESAT_PACKS_ROOT", str(PACKS_ROOT))
    question_bank.clear_cache()
    yield
    question_bank.clear_cache()
