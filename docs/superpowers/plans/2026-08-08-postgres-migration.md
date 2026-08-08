# PostgreSQL 多租户存储迁移实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把全部存储层从 SQLite 单文件迁移到 PostgreSQL 多租户(RLS 隔离),检索层从 FTS5 迁移到 PG tsvector 兜底,并提供 SQLite → PG 数据迁移脚本,278+ 测试全部改跑真实 PG。

**Architecture:** `app/infrastructure/pg.py` 提供 PG 连接池统一入口;迁移器重写为 PG 版(10 个迁移脚本 0001-0010,0008 加租户列与 RLS,0009 加固 SECURITY DEFINER token resolver,0010 加速 misconception evidence 聚合);所有存储模块(StudentRepository/TokenStore/EventStore/LearnerStore/PGMemory/OutboxRepository/SyncService/KnowledgeBackend)改用 psycopg;FastAPI 加租户解析中间件;`scripts/migrate_sqlite_to_pg.py` 一次性迁移现有数据。

**Tech Stack:** psycopg3、PostgreSQL 16(Docker)、PG tsvector、RLS;测试用 pytest 连真实 PG。Milvus 向量检索在 Plan 2,不在本计划内。

**前置:** `docs/superpowers/specs/2026-08-08-commercial-storage-design.md`。

**基线:** `pytest tests/` 278 passed、`python -m evals.run_all` 12 `[ok]`、`node --test web/tests/*.test.js` 21 pass。本计划结束时基线必须全绿(除 evals 中明确标注 PG 相关的项)。

---

## 方言迁移速查(所有任务通用)

| SQLite 写法 | PostgreSQL 写法 |
|---|---|
| `?` 占位符 | `%s` 占位符(psycopg) |
| `sqlite3.connect(path)` | `psycopg.connect(dsn)` |
| `connection.row_factory = sqlite3.Row` | `conn = psycopg.connect(..., row_factory=dict_row)` |
| `connection.executescript(sql)` | `psycopg.extras.execute_script` 或逐条 execute(迁移脚本用 `psycopg.sql` + 逐条) |
| `PRAGMA foreign_keys = ON` | 默认开启,无需设置 |
| `PRAGMA journal_mode = WAL` / `synchronous` | 删除(由 PG 管理) |
| `BEGIN IMMEDIATE` | `BEGIN`(psycopg 隐式事务) |
| `sqlite3.IntegrityError` | `psycopg.errors.UniqueViolation` / `ForeignKeyViolation` |
| `lastrowid` | `INSERT ... RETURNING id` |
| `INTEGER PRIMARY KEY AUTOINCREMENT` | `BIGSERIAL PRIMARY KEY` |
| `executescript` 多语句 | 迁移函数内多次 `cursor.execute` |
| FTS5 虚拟表 | 普通表 + `tsvector` 生成列 + GIN 索引 + `websearch_to_tsquery` |
| `TEXT PRIMARY KEY` | `TEXT PRIMARY KEY`(不变) |
| `json.dumps` 存 TEXT | 不变(保持 TEXT 列,避免 PG json 类型序列化差异) |
| 布尔 `INTEGER NOT NULL` | `BOOLEAN NOT NULL` |

**连接 DSN 约定(双角色):**
- `BRIDGESAT_ADMIN_DB=postgresql://bridgesat:bridgesat@localhost:5432/bridgesat` — 超级用户,**仅用于迁移/DDL**(`apply_migrations`/`connect_admin`)
- `BRIDGESAT_DB=postgresql://bridgesat_app:bridgesat@localhost:5432/bridgesat` — 非超级应用角色,**所有运行时/存储模块/测试业务连接用这个**

**为何双角色:** 单角色 `bridgesat` 是超级用户+表 owner,而 PG 中超级用户与表 owner 都绕过 RLS(owner 需 FORCE 才受限,超级用户不受限)——单角色下 0008 的 RLS 隔离完全无效(已实测跨租户可见)。因此:超级用户只做迁移,应用角色(非 owner)承载 RLS 隔离。token 解析因发生在设置 `app.tenant_id` 之前,必须用 `SECURITY DEFINER` 函数(0008 创建,owner 为超级用户,精确 hash 匹配,只暴露该 token 对应行)。

---

## 文件结构

```
创建:
  docker-compose.yml                          # PG 16 服务
  scripts/dev_env.py                          # 起服务+健康检查
  scripts/migrate_sqlite_to_pg.py             # SQLite → PG 一次性迁移
  app/infrastructure/pg.py                    # 连接池、connect、transaction、Row
  app/infrastructure/migrations_pg/
    0001_bootstrap_legacy_students.py
    0002_content_registry.py
    0003_learning_session_core.py
    0004_episodic_memory.py
    0005_knowledge_fts.py                     # tsvector 版
    0006_sync_protocol.py
    0007_memory_outbox.py
    0008_multi_tenant_rls.py
修改:
  app/infrastructure/migration_runner.py      # PG 版
  app/repository.py                           # → PG
  app/auth.py                                 # → PG + 租户解析
  app/infrastructure/event_store.py           # → PG
  app/infrastructure/learner_store.py         # → PG
  app/memory/sqlite_backend.py                # → PGMemory(改名后全项目引用同步)
  app/memory/outbox.py                        # → PG
  app/sync/service.py                         # → PG
  app/memory/worker.py                        # → PG
  app/knowledge/local_backend.py              # → PG tsvector
  app/main.py                                 # DSN 接线 + 租户中间件
  app/knowledge/router.py                     # DSN 接线
  app/memory/__init__.py, app/memory/worker.py # 构造签名
  tests/conftest.py                           # PG fixture
  tests/test_migrations.py                    # PG 版迁移测试
  tests/test_api.py, tests/security/*         # PG fixture 改造
  .env.example, docs/ARCHITECTURE.md, README.md
删除(仅在 M4 收尾任务):
  app/infrastructure/database.py 及其引用
  app/infrastructure/migrations/ 目录
```

---

## Task 1: docker-compose + 本地 PG 环境脚本

**Files:**
- Create: `docker-compose.yml`
- Create: `scripts/dev_env.py`

- [ ] **Step 1: 写 docker-compose.yml**

```yaml
services:
  postgres:
    image: postgres:16
    container_name: bridgesat-postgres
    environment:
      POSTGRES_USER: bridgesat
      POSTGRES_PASSWORD: bridgesat
      POSTGRES_DB: bridgesat
    ports:
      - "5432:5432"
    volumes:
      - bridgesat-pgdata:/var/lib/postgresql/data
      - ./scripts/pg-initdb:/docker-entrypoint-initdb.d:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U bridgesat -d bridgesat"]
      interval: 2s
      timeout: 2s
      retries: 30

volumes:
  bridgesat-pgdata:
```

Step 1b: 创建 `scripts/pg-initdb/01-app-role.sql`(initdb 阶段创建应用角色;仅首次初始化执行):

```sql
-- Application role for BridgeSAT runtime connections (RLS subject).
CREATE ROLE bridgesat_app LOGIN PASSWORD 'bridgesat';
```

**注意:** 若本地 dev 容器已初始化(volume 存在),initdb 脚本不会重跑;用 dev_env.py 的 `up` 命令补充执行(见 Step 2b)保证幂等。

- [ ] **Step 2: 写 scripts/dev_env.py**

```python
#!/usr/bin/env python3
"""Start/stop the local BridgeSAT dev environment (PostgreSQL).

Usage:
  python scripts/dev_env.py up      # start services and wait until healthy
  python scripts/dev_env.py down    # stop services (keeps data volume)
  python scripts/dev_env.py status  # print service health
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.yml"

DSN = "postgresql://bridgesat:bridgesat@localhost:5432/bridgesat"
APP_DSN = "postgresql://bridgesat_app:bridgesat@localhost:5432/bridgesat"


def _compose(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _wait_healthy(timeout_s: int = 60) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        proc = _compose("ps", "--format", "{{.Health}}", "postgres")
        status = proc.stdout.strip()
        if status == "healthy":
            print("postgres: healthy")
            return
        time.sleep(1)
    print("ERROR: postgres did not become healthy in time")
    sys.exit(1)


def up() -> None:
    _compose("up", "-d", "postgres")
    _wait_healthy()
    _ensure_app_role()
    print(f"admin DSN: {DSN}")
    print(f"app DSN:   {APP_DSN}")


def _ensure_app_role() -> None:
    """Idempotently create the bridgesat_app role (initdb only runs on a
    fresh volume; existing dev volumes need this on every `up`)."""
    proc = subprocess.run(
        [
            "docker", "compose", "-f", str(COMPOSE), "exec", "-T", "postgres",
            "psql", "-U", "bridgesat", "-d", "bridgesat", "-v", "ON_ERROR_STOP=1",
            "-c", "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'bridgesat_app') THEN CREATE ROLE bridgesat_app LOGIN PASSWORD 'bridgesat'; END IF; END $$;",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print(f"WARNING: could not ensure bridgesat_app role: {proc.stderr.strip()}")
    else:
        print("bridgesat_app role: ensured")


def down() -> None:
    _compose("down")
    print("stopped")


def status() -> None:
    proc = _compose("ps")
    print(proc.stdout or "no services running")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    {"up": up, "down": down, "status": status}[command]()
```

- [ ] **Step 3: 启动验证**

Run: `python scripts/dev_env.py up`
Expected: `postgres: healthy` 和 DSN 打印,无报错。

- [ ] **Step 4: 验证 psycopg 可用(安装依赖)**

Run: `pip install "psycopg[binary]>=3.1" && python -c "import psycopg; print(psycopg.__version__)"`
Expected: 打印版本号。将 `psycopg[binary]` 加入 `requirements.txt`(或项目现有依赖清单文件)。

- [ ] **Step 5: Commit**

```bash
git add docker-compose.yml scripts/dev_env.py
git commit -m "chore: 本地 PostgreSQL 开发环境(docker-compose + dev_env)"
```

---

## Task 2: PG 连接层 app/infrastructure/pg.py

**Files:**
- Create: `app/infrastructure/pg.py`
- Test: `tests/test_pg_connect.py`

- [ ] **Step 1: 写失败测试**

```python
"""PG 连接层测试。需要本地 PG 已启动(scripts/dev_env.py up),且
bridgesat_app 角色已创建(up 命令幂等保证)。"""
from __future__ import annotations

import pytest

from app.infrastructure.pg import connect, transaction

DSN = "postgresql://bridgesat_app:bridgesat@localhost:5432/bridgesat"


@pytest.fixture(scope="module")
def pg_conn():
    conn = connect(DSN)
    yield conn
    conn.close()


def test_connect_returns_row_dict(pg_conn) -> None:
    row = pg_conn.execute("SELECT 1 AS one").fetchone()
    assert row["one"] == 1


def test_connect_runs_cleanup_sql(pg_conn) -> None:
    row = pg_conn.execute("SELECT current_setting('search_path') AS sp").fetchone()
    assert row["sp"] == "public"


def test_transaction_commits(pg_conn) -> None:
    with transaction(pg_conn):
        pg_conn.execute(
            "CREATE TEMP TABLE txn_probe (v INTEGER); INSERT INTO txn_probe VALUES (7)"
        )
    row = pg_conn.execute("SELECT COUNT(*) AS n FROM txn_probe").fetchone()
    assert row["n"] == 1


def test_transaction_rolls_back_on_error(pg_conn) -> None:
    with pytest.raises(RuntimeError):
        with transaction(pg_conn):
            pg_conn.execute(
                "CREATE TEMP TABLE txn_probe2 (v INTEGER); INSERT INTO txn_probe2 VALUES (1)"
            )
            raise RuntimeError("boom")
    row = pg_conn.execute(
        "SELECT COUNT(*) AS n FROM information_schema.tables "
        "WHERE table_name = 'txn_probe2'"
    ).fetchone()
    assert row["n"] == 0
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_pg_connect.py -v`
Expected: FAIL(`ModuleNotFoundError: No module named 'app.infrastructure.pg'`)

- [ ] **Step 3: 实现 pg.py**

```python
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

DEFAULT_ADMIN_DSN = "postgresql://bridgesat:bridgesat@localhost:5432/bridgesat"
DEFAULT_APP_DSN = "postgresql://bridgesat_app:bridgesat@localhost:5432/bridgesat"


def admin_dsn() -> str:
    return os.getenv("BRIDGESAT_ADMIN_DB", DEFAULT_ADMIN_DSN)


def dsn() -> str:
    """Application-role DSN (RLS applies). Runtime and tests use this."""
    return os.getenv("BRIDGESAT_DB", DEFAULT_APP_DSN)


def connect_admin(target: str | None = None) -> psycopg.Connection:
    """Superuser connection: migrations/DDL/GRANT only."""
    return psycopg.connect(
        target or admin_dsn(),
        row_factory=dict_row,
        autocommit=False,
    )


def connect(target: str | None = None) -> psycopg.Connection:
    """Application-role connection with dict-row access. Caller closes it."""
    return psycopg.connect(
        target or dsn(),
        row_factory=dict_row,
        autocommit=False,
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
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_pg_connect.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add app/infrastructure/pg.py tests/test_pg_connect.py
git commit -m "feat: PostgreSQL 连接层(pg.py)"
```

---

## Task 3: 迁移器 PG 化

**Files:**
- Modify: `app/infrastructure/migration_runner.py`(重写)
- Test: `tests/test_pg_migration_runner.py`

- [ ] **Step 1: 写失败测试**

```python
"""PG 迁移器测试(替代原 SQLite 版 test_migrations 语义)。"""
from __future__ import annotations

import pytest

from app.infrastructure import pg
from app.infrastructure.migration_runner import (
    MigrationError,
    SCHEMA_VERSION,
    UnsupportedDatabaseError,
    apply_migrations,
    migrate_database,
)


@pytest.fixture()
def database():
    conn = pg.connect_admin()
    migrate_database(conn)
    yield conn
    admin = pg.connect_admin()
    admin.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    admin.commit()
    admin.close()
    conn.close()


def test_fresh_database_migrates_to_supported_version(database) -> None:
    assert pg.database_version(database) == SCHEMA_VERSION


def test_migrations_are_idempotent(database) -> None:
    migrate_database(database)  # second run
    assert pg.database_version(database) == SCHEMA_VERSION


def test_newer_database_schema_is_rejected(database) -> None:
    database.execute("INSERT INTO schema_migrations (version, name, applied_at) "
                     "VALUES (9999, 'future', now())")
    database.commit()
    with pytest.raises(UnsupportedDatabaseError):
        migrate_database(database)


def test_required_tables_exist(database) -> None:
    required = {
        "students", "student_tokens", "student_skill_states", "study_sessions",
        "learning_events", "agent_events", "learning_episodes", "memory_outbox",
        "content_items", "knowledge_fts", "devices", "session_branches",
        "sync_conflicts", "student_deletions", "tenant_roles",
    }
    rows = database.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    ).fetchall()
    names = {row["table_name"] for row in rows}
    missing = required - names
    assert not missing, f"missing tables: {missing}"
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_pg_migration_runner.py -v`
Expected: FAIL(SCHEMA_VERSION 或迁移函数不存在)

- [ ] **Step 3: 重写 migration_runner.py**

```python
"""PostgreSQL migration runner.

Migrations live in app/infrastructure/migrations_pg/ as 000N_*.py modules
exporting `migrate(connection: psycopg.Connection) -> None`. Each migration
runs in its own transaction; schema_migrations records every applied version.
"""
from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from . import pg
from .pg import transaction

MIGRATION_DIR = Path(__file__).resolve().parent / "migrations_pg"

SCHEMA_VERSION = 10


class UnsupportedDatabaseError(RuntimeError):
    pass


class MigrationError(RuntimeError):
    pass


def _load_migration(path: Path):
    module_name = f"app.infrastructure.migrations_pg.{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise MigrationError(f"Cannot load migration {path.name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _available_migrations() -> list[tuple[int, str, Path]]:
    migrations: list[tuple[int, str, Path]] = []
    for path in sorted(MIGRATION_DIR.glob("[0-9][0-9][0-9][0-9]_*.py")):
        version = int(path.stem.split("_", 1)[0])
        migrations.append((version, path.stem, path))
    return migrations


def migrate_database(connection: psycopg.Connection) -> int:
    """Apply all pending migrations and return the resulting schema version."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    applied = {
        row["version"]
        for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
    }
    target = SCHEMA_VERSION
    if applied and max(applied) > target:
        raise UnsupportedDatabaseError(
            f"Database schema version {max(applied)} is newer than supported "
            f"version {target}"
        )
    pending = [m for m in _available_migrations() if m[0] not in applied and m[0] <= target]
    for version, name, path in pending:
        module = _load_migration(path)
        with transaction(connection):
            module.migrate(connection)
            connection.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) "
                "VALUES (%s, %s, now())",
                (version, name),
            )
            applied.add(version)
    return max(applied) if applied else 0


def apply_migrations(target: str | None = None) -> int:
    """Open a superuser connection (migrations need DDL/GRANT rights),
    migrate, close. Returns version."""
    connection = pg.connect_admin(target)
    try:
        return migrate_database(connection)
    finally:
        connection.close()
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_pg_migration_runner.py -v`
Expected: 4 passed(注意:此刻迁移脚本目录尚不存在,Step 3 应同时创建 `app/infrastructure/migrations_pg/` 空目录和 `__init__.py`)

- [ ] **Step 5: Commit**

```bash
git add app/infrastructure/migration_runner.py tests/test_pg_migration_runner.py app/infrastructure/migrations_pg/__init__.py
git commit -m "feat: PostgreSQL 迁移器重写"
```

---

## Task 4: PG 迁移脚本 0001-0004

**Files:**
- Create: `app/infrastructure/migrations_pg/0001_bootstrap_legacy_students.py`
- Create: `app/infrastructure/migrations_pg/0002_content_registry.py`
- Create: `app/infrastructure/migrations_pg/0003_learning_session_core.py`
- Create: `app/infrastructure/migrations_pg/0004_episodic_memory.py`

- [ ] **Step 1: 0001 写迁移脚本**

```python
"""0001: legacy mastery imports (PG)."""
from __future__ import annotations

import psycopg


def migrate(connection: psycopg.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS legacy_mastery_imports (
            import_id TEXT PRIMARY KEY,
            student_id TEXT NOT NULL,
            mastery_json TEXT NOT NULL,
            imported_at TEXT NOT NULL
        )
        """
    )
```

- [ ] **Step 2: 0002 写迁移脚本**

```python
"""0002: content registry (PG). Content is global, NOT tenant-scoped:
published content is shared across tenants and read-only to them."""
from __future__ import annotations

import psycopg


def migrate(connection: psycopg.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS skills (
            skill TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT ''
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_prerequisites (
            skill TEXT NOT NULL,
            prerequisite TEXT NOT NULL,
            PRIMARY KEY (skill, prerequisite)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS content_sources (
            source_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            license TEXT NOT NULL,
            permitted_use TEXT NOT NULL,
            is_trusted BOOLEAN NOT NULL DEFAULT FALSE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS content_items (
            content_id TEXT PRIMARY KEY,
            version INTEGER NOT NULL DEFAULT 1,
            content_type TEXT NOT NULL,
            target_skill TEXT NOT NULL,
            target_subskill TEXT NOT NULL DEFAULT '',
            audience TEXT NOT NULL DEFAULT 'student',
            license_id TEXT NOT NULL DEFAULT '',
            license_name TEXT NOT NULL DEFAULT '',
            source_id TEXT NOT NULL DEFAULT '',
            review_status TEXT NOT NULL DEFAULT 'draft',
            body TEXT NOT NULL DEFAULT ''
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS content_item_versions (
            content_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            versioned_body TEXT NOT NULL,
            versioned_at TEXT NOT NULL,
            PRIMARY KEY (content_id, version)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS content_reviews (
            review_id TEXT PRIMARY KEY,
            content_id TEXT NOT NULL,
            reviewer TEXT NOT NULL,
            decision TEXT NOT NULL,
            reviewed_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS content_packs (
            pack_id TEXT PRIMARY KEY,
            pack_version TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS content_pack_items (
            pack_id TEXT NOT NULL,
            content_id TEXT NOT NULL,
            PRIMARY KEY (pack_id, content_id)
        )
        """
    )
```

- [ ] **Step 3: 0003 写迁移脚本(学习会话核心 + 租户列)**

```python
"""0003: learning session core (PG). All student-scoped tables carry
tenant_id; RLS is enabled by migration 0008."""
from __future__ import annotations

import psycopg


def migrate(connection: psycopg.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS students (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'tenant_demo',
            name TEXT NOT NULL,
            daily_minutes INTEGER NOT NULL,
            target_score INTEGER NOT NULL,
            mastery_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_students_tenant ON students (tenant_id)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS student_tokens (
            token_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'tenant_demo',
            student_id TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            device_bound_name TEXT,
            created_at TEXT NOT NULL,
            revoked_at TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS student_skill_states (
            student_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL DEFAULT 'tenant_demo',
            skill TEXT NOT NULL,
            alpha DOUBLE PRECISION NOT NULL,
            beta DOUBLE PRECISION NOT NULL,
            mastery DOUBLE PRECISION NOT NULL,
            confidence DOUBLE PRECISION NOT NULL,
            evidence_count INTEGER NOT NULL DEFAULT 0,
            correct_streak INTEGER NOT NULL DEFAULT 0,
            incorrect_streak INTEGER NOT NULL DEFAULT 0,
            last_practiced_at TEXT,
            review_due_at TEXT,
            projection_origin TEXT NOT NULL DEFAULT 'live',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (student_id, skill)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS study_plans (
            plan_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'tenant_demo',
            student_id TEXT NOT NULL,
            session_id TEXT,
            plan_json TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            superseded_by_plan_id TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS study_sessions (
            session_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'tenant_demo',
            student_id TEXT NOT NULL,
            session_state TEXT NOT NULL,
            paused_from_state TEXT,
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS session_items (
            session_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL DEFAULT 'tenant_demo',
            sequence INTEGER NOT NULL,
            content_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            skill TEXT NOT NULL,
            subskill TEXT,
            difficulty INTEGER NOT NULL,
            role TEXT NOT NULL,
            shown_at TEXT,
            answered_at TEXT,
            PRIMARY KEY (session_id, sequence)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS answer_attempts (
            attempt_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'tenant_demo',
            event_id TEXT NOT NULL UNIQUE,
            student_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            content_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            sequence INTEGER NOT NULL,
            selected_choice_id TEXT NOT NULL,
            correct INTEGER NOT NULL,
            hint_level INTEGER NOT NULL DEFAULT 0,
            weight DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            validity TEXT NOT NULL DEFAULT 'valid',
            occurred_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS learning_events (
            event_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'tenant_demo',
            student_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            content_version TEXT,
            occurred_at TEXT NOT NULL,
            received_at TEXT NOT NULL,
            device_id TEXT,
            device_sequence INTEGER,
            origin TEXT NOT NULL,
            integrity_hash TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_events (
            event_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'tenant_demo',
            student_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            source_event_id TEXT,
            state_before TEXT,
            state_after TEXT,
            action TEXT NOT NULL,
            action_payload_json TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            reason_text TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            taxonomy_version TEXT,
            content_version TEXT,
            referenced_content_json TEXT,
            episode_ids_json TEXT,
            source TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS misconception_evidence (
            evidence_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'tenant_demo',
            student_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            skill TEXT NOT NULL,
            subskill TEXT,
            misconception TEXT NOT NULL,
            source_label TEXT NOT NULL,
            confidence_label TEXT NOT NULL,
            state TEXT NOT NULL,
            item_id TEXT NOT NULL,
            item_version INTEGER NOT NULL,
            observed_at TEXT NOT NULL
        )
        """
    )
    for table in (
        "student_tokens", "student_skill_states", "study_plans", "study_sessions",
        "session_items", "answer_attempts", "learning_events", "agent_events",
        "misconception_evidence",
    ):
        connection.execute(
            f'CREATE INDEX IF NOT EXISTS idx_{table}_tenant ON {table} (tenant_id)'
        )
```

- [ ] **Step 4: 0004 写迁移脚本**

```python
"""0004: episodic memory (PG)."""
from __future__ import annotations

import psycopg


def migrate(connection: psycopg.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS learning_episodes (
            episode_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'tenant_demo',
            student_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            skill TEXT NOT NULL,
            misconception TEXT,
            intervention TEXT NOT NULL,
            outcome_json TEXT NOT NULL,
            effectiveness DOUBLE PRECISION NOT NULL,
            evidence_event_ids_json TEXT NOT NULL,
            summary TEXT NOT NULL,
            confidence DOUBLE PRECISION NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS student_memory_facts (
            fact_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'tenant_demo',
            student_id TEXT NOT NULL,
            category TEXT NOT NULL,
            normalized_key TEXT NOT NULL,
            fact_text TEXT NOT NULL,
            confidence DOUBLE PRECISION NOT NULL,
            supporting_episode_ids_json TEXT NOT NULL,
            contradicting_episode_ids_json TEXT NOT NULL,
            evidence_count INTEGER NOT NULL,
            contradiction_count INTEGER NOT NULL,
            status TEXT NOT NULL,
            first_observed_at TEXT NOT NULL,
            last_observed_at TEXT NOT NULL,
            version INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS intervention_stats (
            stat_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'tenant_demo',
            student_id TEXT NOT NULL,
            skill TEXT NOT NULL,
            misconception TEXT,
            intervention TEXT NOT NULL,
            difficulty_band TEXT NOT NULL,
            immediate_correct DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            immediate_attempts INTEGER NOT NULL DEFAULT 0,
            immediate_weight DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            short_term_correct DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            short_term_attempts INTEGER NOT NULL DEFAULT 0,
            short_term_weight DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            delayed_correct DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            delayed_attempts INTEGER NOT NULL DEFAULT 0,
            delayed_weight DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            updated_at TEXT NOT NULL,
            UNIQUE (student_id, skill, misconception, intervention, difficulty_band)
        )
        """
    )
    for table in ("learning_episodes", "student_memory_facts", "intervention_stats"):
        connection.execute(
            f'CREATE INDEX IF NOT EXISTS idx_{table}_tenant ON {table} (tenant_id)'
        )
```

- [ ] **Step 5: 运行迁移器验证**

Run: `python -c "
import sys; sys.path.insert(0, '.')
from app.infrastructure.migration_runner import migrate_database
from app.infrastructure import pg
conn = pg.connect_admin(); print('version:', migrate_database(conn)); conn.close()
"`
Expected: `version: 4`(0005-0008 尚未创建,只到 4)

- [ ] **Step 6: Commit**

```bash
git add app/infrastructure/migrations_pg/
git commit -m "feat: PG 迁移脚本 0001-0004(基础+会话核心+记忆)"
```

---

## Task 5: PG 迁移脚本 0005-0010

**Files:**
- Create: `app/infrastructure/migrations_pg/0005_knowledge_fts.py`(tsvector 版)
- Create: `app/infrastructure/migrations_pg/0006_sync_protocol.py`
- Create: `app/infrastructure/migrations_pg/0007_memory_outbox.py`
- Create: `app/infrastructure/migrations_pg/0008_multi_tenant_rls.py`
- Create: `app/infrastructure/migrations_pg/0009_harden_token_resolver.py`(存量 v8 数据库的 SECURITY DEFINER 加固)
- Create: `app/infrastructure/migrations_pg/0010_misconception_evidence_index.py`(misconception evidence 聚合索引)

- [ ] **Step 1: 0005 写迁移脚本(tsvector 兜底检索)**

```python
"""0005: knowledge search index (PG tsvector).

Replaces the SQLite FTS5 virtual table with a regular table carrying a
generated tsvector column and a GIN index. This is the Level-1 fallback
retrieval path; Milvus vector search (Level 2) comes in Plan 2.
"""
from __future__ import annotations

import psycopg


def migrate(connection: psycopg.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge_fts (
            content_id TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            content_type TEXT NOT NULL,
            target_skill TEXT NOT NULL,
            target_subskill TEXT NOT NULL DEFAULT '',
            audience TEXT NOT NULL DEFAULT 'student',
            license_id TEXT NOT NULL DEFAULT '',
            license_name TEXT NOT NULL DEFAULT '',
            source_id TEXT NOT NULL DEFAULT '',
            review_status TEXT NOT NULL DEFAULT 'published',
            body TEXT NOT NULL DEFAULT '',
            body_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', body)) STORED
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_fts_tsv "
        "ON knowledge_fts USING GIN (body_tsv)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge_index_log (
            log_id BIGSERIAL PRIMARY KEY,
            indexed_at TEXT NOT NULL,
            pack_id TEXT NOT NULL,
            pack_version TEXT NOT NULL,
            item_count INTEGER NOT NULL,
            lesson_count INTEGER NOT NULL,
            status TEXT NOT NULL
        )
        """
    )
```

- [ ] **Step 2: 0006 写迁移脚本**

```python
"""0006: sync protocol (PG)."""
from __future__ import annotations

import psycopg


def migrate(connection: psycopg.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS devices (
            device_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'tenant_demo',
            student_id TEXT NOT NULL,
            device_name TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            last_seen_at TEXT,
            revoked_at TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS session_branches (
            branch_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'tenant_demo',
            session_id TEXT NOT NULL,
            parent_session_id TEXT,
            device_id TEXT NOT NULL,
            branched_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open'
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_conflicts (
            conflict_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'tenant_demo',
            session_id TEXT NOT NULL,
            device_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            resolution TEXT NOT NULL,
            resolved_at TEXT NOT NULL
        )
        """
    )
    for table in ("devices", "session_branches", "sync_conflicts"):
        connection.execute(
            f'CREATE INDEX IF NOT EXISTS idx_{table}_tenant ON {table} (tenant_id)'
        )
```

- [ ] **Step 3: 0007 写迁移脚本**

```python
"""0007: memory outbox (PG)."""
from __future__ import annotations

import psycopg


def migrate(connection: psycopg.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_outbox (
            outbox_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'tenant_demo',
            student_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            next_retry_at TEXT NOT NULL,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_outbox_tenant ON memory_outbox (tenant_id)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS student_deletions (
            deletion_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'tenant_demo',
            student_id TEXT NOT NULL,
            requested_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            completed_at TEXT
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_student_deletions_tenant "
        "ON student_deletions (tenant_id)"
    )
```

- [ ] **Step 4: 0008 写迁移脚本(RLS)**

```python
"""0008: multi-tenant row-level security.

Enables RLS on every tenant-scoped table with a single policy: the row's
tenant_id must equal the session variable app.tenant_id. Application code
sets `SET LOCAL app.tenant_id = '<tenant>'` inside each request transaction
(see app/main.py tenant middleware). Content tables stay unprotected.
"""
from __future__ import annotations

import psycopg

TENANT_TABLES = (
    "students", "student_tokens", "student_skill_states", "study_plans",
    "study_sessions", "session_items", "answer_attempts", "learning_events",
    "agent_events", "misconception_evidence", "learning_episodes",
    "student_memory_facts", "intervention_stats", "devices",
    "session_branches", "sync_conflicts", "memory_outbox", "student_deletions",
)


def migrate(connection: psycopg.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS tenant_roles (
            tenant_id TEXT PRIMARY KEY,
            role_name TEXT NOT NULL DEFAULT 'tenant_member'
        )
        """
    )
    for table in TENANT_TABLES:
        connection.execute(
            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"
        )
        connection.execute(
            f"DROP POLICY IF EXISTS tenant_isolation ON {table}"
        )
        connection.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = current_setting('app.tenant_id', true))
            """
        )
    _grant_app_role(connection)
    _create_token_resolver(connection)


def _grant_app_role(connection: psycopg.Connection) -> None:
    """Grant runtime privileges to bridgesat_app.

    bridgesat (superuser) owns all tables and bypasses RLS; bridgesat_app is
    the RLS subject. Default privileges make future tables usable without
    re-granting.
    """
    connection.execute("GRANT USAGE ON SCHEMA public TO bridgesat_app")
    connection.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO bridgesat_app"
    )
    connection.execute(
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO bridgesat_app"
    )
    connection.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO bridgesat_app"
    )
    connection.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT USAGE, SELECT ON SEQUENCES TO bridgesat_app"
    )


def _create_token_resolver(connection: psycopg.Connection) -> None:
    """Token resolution runs BEFORE app.tenant_id is set, so it must bypass
    RLS. SECURITY DEFINER (owner = bridgesat superuser) + exact hash match
    exposes only the row for the presented token."""
    connection.execute(
        """
        CREATE OR REPLACE FUNCTION public.resolve_token(p_hash TEXT)
        RETURNS TABLE (tenant_id TEXT, student_id TEXT)
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = pg_catalog, public, pg_temp
        AS $$
            SELECT tenant_id, student_id FROM public.student_tokens
            WHERE token_hash = p_hash AND revoked_at IS NULL
        $$
        """
    )
    connection.execute("REVOKE ALL ON FUNCTION public.resolve_token(TEXT) FROM PUBLIC")
    connection.execute("GRANT EXECUTE ON FUNCTION public.resolve_token(TEXT) TO bridgesat_app")
```

- [ ] **Step 4b: 0009 加固存量 v8 的 token resolver**

`0008` 初次创建 resolver 后,新增 `0009_harden_token_resolver.py` 对已记录为
version 8 的数据库执行 `CREATE OR REPLACE FUNCTION public.resolve_token(TEXT)`。
函数必须使用 `SET search_path = pg_catalog, public, pg_temp`、显式查询
`public.student_tokens`,并重新执行 `REVOKE PUBLIC`/`GRANT bridgesat_app`。
`SCHEMA_VERSION` 提升为 9;回归测试使用独立临时数据库模拟 v8→v9,验证临时表不能劫持 resolver 及函数 ACL/SECURITY DEFINER 属性。

- [ ] **Step 4c: 0010 加速 misconception evidence 聚合**

新增幂等索引 `public.idx_misconception_evidence_lookup`:
`(tenant_id, student_id, skill, misconception, item_id)`,覆盖 LearnerStore 的
`COUNT(*)`/`COUNT(DISTINCT item_id)` 查询;`SCHEMA_VERSION` 提升为 10,并验证 fresh 与 v9→v10 升级路径。

- [ ] **Step 5: 运行迁移器验证(全部 10 个)**

Run: `python -c "
import sys; sys.path.insert(0, '.')
from app.infrastructure.migration_runner import migrate_database
from app.infrastructure import pg
conn = pg.connect_admin(); print('version:', migrate_database(conn)); conn.close()
"`
Expected: `version: 10`。再跑一次仍为 10(幂等)。已有 v9 数据库也必须应用 0010;已有 v8 数据库应依次应用 0009、0010。

- [ ] **Step 5b: 验证 RLS 在 app 角色下真正生效(双角色核心)**

```python
# 1) admin 连接插入学生
# 2) app 角色(未设置 tenant)→ 看不到;设置 tenant_a → 看到;
#    设置 tenant_b → 看不到;INSERT 跨租户被拒
```
具体验证(临时脚本,不进 git):admin 连接 `INSERT INTO students (..., tenant_id) VALUES ('stu_x', 'tenant_a')` 后,`bridgesat_app` 连接 `SELECT * FROM students` = 空;`SET app.tenant_id = 'tenant_a'` 后可读到 stu_x;`SET app.tenant_id = 'tenant_b'` 后读不到;`INSERT ... tenant_id='tenant_b'` 报 RLS 错误。另验证 `resolve_token('...')` 无需设置 tenant 即可返回租户(先造一条 student_tokens)。

**注意:** 0008 的 GRANT 依赖 `bridgesat_app` 角色已存在(dev_env.py up 保证)。若直接跑测试而角色缺失,0008 迁移会失败——先 `python scripts/dev_env.py up`。

- [ ] **Step 6: Commit**

```bash
git add app/infrastructure/migrations_pg/
git commit -m "feat: PG 迁移脚本 0005-0008(tsvector 检索+同步+outbox+RLS)"
```

---

## Task 6: StudentRepository → PG

**Files:**
- Modify: `app/repository.py`
- Test: `tests/test_pg_repository.py`

- [ ] **Step 1: 写失败测试**

```python
"""StudentRepository on PostgreSQL. Needs local PG (scripts/dev_env.py up)."""
from __future__ import annotations

import pytest

from app.infrastructure import pg
from app.infrastructure.migration_runner import migrate_database
from app.models import Skill, StudentCreate
from app.repository import StudentRepository

TENANT = "tenant_test"


@pytest.fixture()
def repo():
    """Test fixture template for ALL PG tasks:
    - admin connection (superuser) for migrations + schema reset
    - app connection (bridgesat_app) for the store under test, so RLS applies
    """
    admin = pg.connect_admin()
    migrate_database(admin)
    admin.close()
    conn = pg.connect()
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (TENANT,))
    conn.commit()
    yield StudentRepository(conn)
    admin = pg.connect_admin()
    admin.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    admin.commit()
    admin.close()
    conn.close()


def test_create_and_get(repo) -> None:
    student = repo.create(StudentCreate(name="Ada", daily_minutes=30, target_score=600))
    fetched = repo.get(student.id)
    assert fetched is not None
    assert fetched.name == "Ada"
    assert fetched.mastery[Skill("linear_equations")] == 0.5


def test_get_other_tenant_returns_none(repo) -> None:
    student = repo.create(StudentCreate(name="Ada", daily_minutes=30, target_score=600))
    repo2_conn = pg.connect()
    repo2_conn.execute("SELECT set_config('app.tenant_id', 'tenant_other', false)")
    repo2_conn.commit()
    repo2 = StudentRepository(repo2_conn)
    assert repo2.get(student.id) is None
    repo2_conn.close()


def test_update_mastery(repo) -> None:
    student = repo.create(StudentCreate(name="Ada", daily_minutes=30, target_score=600))
    repo.update_mastery(student.id, {Skill("linear_equations"): 0.9})
    fetched = repo.get(student.id)
    assert fetched.mastery[Skill("linear_equations")] == 0.9
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_pg_repository.py -v`
Expected: FAIL(构造签名/导入错误)

- [ ] **Step 3: 重写 repository.py**

```python
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
                id, name, daily_minutes, target_score, mastery_json,
                status, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, 'active', %s, %s)
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
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_pg_repository.py -v`
Expected: 3 passed

- [ ] **Step 5: 运行既有 repository 相关测试**

Run: `pytest tests/test_api.py -v 2>&1 | tail -20`
Expected: 失败(api 测试仍用旧 SQLite fixture)——本任务暂不修复,Task 15 统一改造测试层。记录失败列表即可。

- [ ] **Step 6: Commit**

```bash
git add app/repository.py tests/test_pg_repository.py
git commit -m "feat: StudentRepository 迁移到 PostgreSQL"
```

---

## Task 7: TokenStore + 租户解析 → PG

**Files:**
- Modify: `app/auth.py`
- Test: `tests/test_pg_auth.py`

- [ ] **Step 1: 写失败测试**

```python
"""TokenStore + tenant resolution on PostgreSQL."""
from __future__ import annotations

import pytest

from app.auth import TokenStore, resolve_tenant
from app.infrastructure import pg
from app.infrastructure.migration_runner import migrate_database

TENANT = "tenant_test"


@pytest.fixture()
def store():
    admin = pg.connect_admin()
    migrate_database(admin)
    admin.close()
    conn = pg.connect()
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (TENANT,))
    conn.commit()
    yield TokenStore(conn)
    admin = pg.connect_admin()
    admin.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    admin.commit()
    admin.close()
    conn.close()


def test_issue_and_resolve_round_trip(store) -> None:
    token = store.issue("stu_1")
    assert store.resolve(token) == "stu_1"
    assert store.verify("stu_1", token) is True


def test_revoke(store) -> None:
    token = store.issue("stu_1")
    store.revoke(token)
    assert store.resolve(token) is None


def test_resolve_works_from_any_tenant_context(store) -> None:
    """Token resolution bypasses RLS via SECURITY DEFINER: it must work even
    before app.tenant_id is set (the middleware depends on this)."""
    token = store.issue("stu_1")
    conn2 = pg.connect()
    conn2.execute("SELECT set_config('app.tenant_id', 'tenant_other', false)")
    conn2.commit()
    store2 = TokenStore(conn2)
    assert store2.resolve(token) == "stu_1"
    conn2.close()


def test_resolve_tenant_maps_student(store) -> None:
    token = store.issue("stu_1")
    assert resolve_tenant(store, token) == (TENANT, "stu_1")
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_pg_auth.py -v`
Expected: FAIL(签名不匹配)

- [ ] **Step 3: 重写 auth.py**

```python
"""Scoped bearer-token authentication with tenant resolution.

Every student token binds to (tenant_id, student_id). The tenant middleware
reads the token, resolves the tenant, and sets the RLS session variable
before any handler runs.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime

import psycopg
from fastapi import Header, HTTPException, status

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
                token_id, student_id, token_hash, device_bound_name, created_at
            ) VALUES (%s, %s, %s, %s, %s)
            """,
            (f"tok_{secrets.token_hex(8)}", student_id, token_hash, device_bound_name, now),
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
        """Return (tenant_id, student_id) for an active token, or None.

        Uses the SECURITY DEFINER resolver so lookup works before any
        app.tenant_id is set (RLS would otherwise hide every row)."""
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
            (datetime.now(UTC).isoformat(), hashlib.sha256(token.encode()).hexdigest()),
        )
        self.connection.commit()


def resolve_tenant(store: TokenStore, token: str) -> tuple[str, str] | None:
    return store.resolve_tenant(token)


def require_student(
    authorization: str | None = Header(default=None),
) -> str:
    """FastAPI dependency: parse `Authorization: Bearer <token>` and return
    the authenticated student_id (401 on missing/invalid token)."""
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
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_pg_auth.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add app/auth.py tests/test_pg_auth.py
git commit -m "feat: TokenStore 迁移到 PG + 租户解析"
```

---

## Task 8: EventStore → PG

**Files:**
- Modify: `app/infrastructure/event_store.py`
- Test: `tests/test_pg_event_store.py`

- [ ] **Step 1: 写失败测试(覆盖原语义:追加、去重、查询、事务)**

```python
"""EventStore on PostgreSQL."""
from __future__ import annotations

import pytest

from app.domain.events import AgentEvent, LearningEvent
from app.infrastructure import pg
from app.infrastructure.event_store import (
    DuplicateEventError,
    EventStore,
    run_in_transaction,
)
from app.infrastructure.migration_runner import migrate_database


@pytest.fixture()
def store():
    admin = pg.connect_admin()
    migrate_database(admin)
    admin.close()
    conn = pg.connect()
    conn.execute("SELECT set_config('app.tenant_id', 'tenant_test', false)")
    conn.commit()
    yield EventStore(conn)
    admin = pg.connect_admin()
    admin.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    admin.commit()
    admin.close()
    conn.close()


def _learning_event(event_id: str) -> LearningEvent:
    return LearningEvent(
        event_id=event_id,
        student_id="stu_1",
        session_id="sess_1",
        event_type="answer_submitted",
        payload={},
        policy_version="test",
        occurred_at="2026-01-01T00:00:00+00:00",
        received_at="2026-01-01T00:00:00+00:00",
        origin="api",
    )


def test_append_and_get(store) -> None:
    event = _learning_event("evt_1")
    store.append_learning_event(event)
    events = store.get_learning_events("stu_1")
    assert [e.event_id for e in events] == ["evt_1"]


def test_duplicate_append_raises(store) -> None:
    store.append_learning_event(_learning_event("evt_1"))
    with pytest.raises(DuplicateEventError):
        store.append_learning_event(_learning_event("evt_1"))


def test_append_agent_event_duplicate_ignored(store) -> None:
    event = AgentEvent(
        event_id="aev_1", student_id="stu_1", session_id="sess_1",
        action="insert_micro_lesson", action_payload={}, reason_code="r",
        reason_text="t", policy_version="test", source="test",
        created_at="2026-01-01T00:00:00+00:00",
    )
    store.append_agent_event(event)
    assert store.append_agent_event(event, on_duplicate="ignore") is False


def test_run_in_transaction(store) -> None:
    events = [_learning_event("evt_a"), _learning_event("evt_b")]

    def _both(conn) -> None:
        for event in events:
            conn.execute(
                "INSERT INTO learning_events (event_id, student_id, session_id, event_type, "
                "payload_json, policy_version, occurred_at, received_at, origin, integrity_hash) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (event.event_id, event.student_id, event.session_id, event.event_type,
                 "{}", event.policy_version, event.occurred_at, event.received_at,
                 event.origin, "hash"),
            )

    run_in_transaction(store.connection, _both)
    assert len(store.get_learning_events("stu_1")) == 2
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_pg_event_store.py -v`
Expected: FAIL

- [ ] **Step 3: 重写 event_store.py**

先读原文件(在改写前必须通读),将每个 `sqlite3.connect(...)` 改为使用传入连接,`?` → `%s`,`sqlite3.IntegrityError` → 捕获 `psycopg.errors.UniqueViolation`。完整实现:

```python
"""Append-only learning/agent event store on PostgreSQL.

The event log is the immutable source of truth for learner behaviour;
duplicate appends are rejected by primary key.
"""
from __future__ import annotations

import psycopg
from psycopg.errors import UniqueViolation

from app.domain.events import AgentEvent, LearningEvent


class DuplicateEventError(RuntimeError):
    pass


class EventStore:
    def __init__(self, connection: psycopg.Connection) -> None:
        self.connection = connection

    def append_learning_event(self, event: LearningEvent) -> None:
        try:
            self.connection.execute(
                """
                INSERT INTO learning_events (
                    event_id, student_id, session_id, event_type, payload_json,
                    policy_version, content_version, occurred_at, received_at,
                    device_id, device_sequence, origin, integrity_hash
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    event.event_id, event.student_id, event.session_id,
                    event.event_type, event.payload_json, event.policy_version,
                    event.content_version, event.occurred_at, event.received_at,
                    event.device_id, event.device_sequence, event.origin,
                    event.integrity_hash,
                ),
            )
            self.connection.commit()
        except UniqueViolation as exc:
            self.connection.rollback()
            raise DuplicateEventError(
                f"learning event {event.event_id} already exists"
            ) from exc

    def append_agent_event(
        self, event: AgentEvent, *, on_duplicate: str = "ignore"
    ) -> bool:
        try:
            self.connection.execute(
                """
                INSERT INTO agent_events (
                    event_id, student_id, session_id, source_event_id,
                    state_before, state_after, action, action_payload_json,
                    reason_code, reason_text, policy_version, taxonomy_version,
                    content_version, referenced_content_json, episode_ids_json,
                    source, created_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    event.event_id, event.student_id, event.session_id,
                    event.source_event_id, event.state_before, event.state_after,
                    event.action, event.action_payload_json, event.reason_code,
                    event.reason_text, event.policy_version, event.taxonomy_version,
                    event.content_version, event.referenced_content_json,
                    event.episode_ids_json, event.source, event.created_at,
                ),
            )
            self.connection.commit()
            return True
        except UniqueViolation:
            self.connection.rollback()
            if on_duplicate == "raise":
                raise DuplicateEventError(f"agent event {event.event_id} already exists")
            return False

    def learning_event_exists(self, event_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 AS hit FROM learning_events WHERE event_id = %s",
            (event_id,),
        ).fetchone()
        return row is not None

    def get_learning_events(
        self, student_id: str, *, session_id: str | None = None
    ) -> list[LearningEvent]:
        if session_id is None:
            rows = self.connection.execute(
                "SELECT * FROM learning_events WHERE student_id = %s ORDER BY received_at",
                (student_id,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM learning_events WHERE student_id = %s AND session_id = %s "
                "ORDER BY received_at",
                (student_id, session_id),
            ).fetchall()
        return [_row_to_learning_event(row) for row in rows]

    def get_agent_events(
        self, student_id: str, *, session_id: str | None = None
    ) -> list[AgentEvent]:
        if session_id is None:
            rows = self.connection.execute(
                "SELECT * FROM agent_events WHERE student_id = %s ORDER BY created_at",
                (student_id,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM agent_events WHERE student_id = %s AND session_id = %s "
                "ORDER BY created_at",
                (student_id, session_id),
            ).fetchall()
        return [_row_to_agent_event(row) for row in rows]


def run_in_transaction(connection: psycopg.Connection, fn) -> None:
    try:
        fn(connection)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _row_to_learning_event(row: dict) -> LearningEvent:
    return LearningEvent(
        event_id=row["event_id"],
        student_id=row["student_id"],
        session_id=row["session_id"],
        event_type=row["event_type"],
        payload_json=row["payload_json"],
        policy_version=row["policy_version"],
        content_version=row["content_version"],
        occurred_at=row["occurred_at"],
        received_at=row["received_at"],
        device_id=row["device_id"],
        device_sequence=row["device_sequence"],
        origin=row["origin"],
        integrity_hash=row["integrity_hash"],
    )


def _row_to_agent_event(row: dict) -> AgentEvent:
    return AgentEvent(
        event_id=row["event_id"],
        student_id=row["student_id"],
        session_id=row["session_id"],
        source_event_id=row["source_event_id"],
        state_before=row["state_before"],
        state_after=row["state_after"],
        action=row["action"],
        action_payload_json=row["action_payload_json"],
        reason_code=row["reason_code"],
        reason_text=row["reason_text"],
        policy_version=row["policy_version"],
        taxonomy_version=row["taxonomy_version"],
        content_version=row["content_version"],
        referenced_content_json=row["referenced_content_json"],
        episode_ids_json=row["episode_ids_json"],
        source=row["source"],
        created_at=row["created_at"],
    )
```

注意:改写前必须通读原文件核对每个字段名与默认值(例如 `payload_json` vs 域对象属性),如有出入以原文件为准。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_pg_event_store.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add app/infrastructure/event_store.py tests/test_pg_event_store.py
git commit -m "feat: EventStore 迁移到 PostgreSQL"
```

---

## Task 9: LearnerStore → PG

**Files:**
- Modify: `app/infrastructure/learner_store.py`(最大改动,417 行)
- Test: `tests/test_pg_learner_store.py`

- [ ] **Step 1: 先通读原文件并写失败测试**

Run: `sed -n 1,417p app/infrastructure/learner_store.py`
(逐行核对字段后)

写测试覆盖核心语义(基于原测试文件 `tests/test_learner_store.py` 若存在;否则以下面为准):

```python
"""LearnerStore on PostgreSQL — core session flow."""
from __future__ import annotations

import pytest

from app.domain.learner import SkillState
from app.domain.sessions import SessionState
from app.infrastructure import pg
from app.infrastructure.learner_store import LearnerStore
from app.infrastructure.migration_runner import migrate_database


@pytest.fixture()
def store():
    admin = pg.connect_admin()
    migrate_database(admin)
    admin.close()
    conn = pg.connect()
    conn.execute("SELECT set_config('app.tenant_id', 'tenant_test', false)")
    conn.commit()
    yield LearnerStore(conn)
    admin = pg.connect_admin()
    admin.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    admin.commit()
    admin.close()
    conn.close()


def test_create_student_emits_event(store) -> None:
    student_id, event = store.create_student("Ada", 30, 600)
    assert event.student_id == student_id
    assert event.event_type == "student_created"


def test_session_round_trip(store) -> None:
    student_id, _ = store.create_student("Ada", 30, 600)
    session_id = store.create_session(student_id)
    assert store.get_session_state(session_id) == SessionState.QUESTION_ACTIVE
    store.transition_session(session_id, SessionState.QUESTION_ACTIVE, SessionState.SESSION_SUMMARY)
    assert store.get_session_state(session_id) == SessionState.SESSION_SUMMARY


def test_record_answer_updates_skill_state(store) -> None:
    student_id, _ = store.create_student("Ada", 30, 600)
    session_id = store.create_session(student_id)
    store.record_answer_evaluation(
        student_id=student_id,
        session_id=session_id,
        content_id="math.linear_equations.001",
        version=1,
        sequence=1,
        selected_choice_id="c1",
        correct=True,
        skill="linear_equations",
        subskill="isolate_variable",
        occurred_at="2026-01-01T00:00:00+00:00",
    )
    state = store.get_skill_state(student_id, "linear_equations")
    assert state is not None
    assert state.mastery > 0.5
    assert isinstance(state, SkillState)
```

(依据原文件方法签名调整参数;`record_answer_evaluation` 真实签名以通读为准。)

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_pg_learner_store.py -v`
Expected: FAIL

- [ ] **Step 3: 重写 learner_store.py**

通读原文件后按以下规则逐方法改写:
- `self.database_path` → `self.connection`(构造签名改为 `__init__(self, connection: psycopg.Connection)`)
- `with self._connect() as connection:` → 直接使用 `self.connection`
- `?` → `%s`
- `sqlite3.IntegrityError` → `psycopg.errors.UniqueViolation`
- `connection.execute("BEGIN IMMEDIATE")` 类语句删除(psycopg 隐式事务),commit 调用保留
- `ON CONFLICT (student_id, skill) DO NOTHING` 语法 PG 原生支持,保留
- 保持领域语义完全不变:DuplicateEventIdError、CORE_MISCONCEPTION_SKILLS、默认 alpha/beta、证据权重逻辑、状态机转换全部照抄

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_pg_learner_store.py -v`
Expected: passed

- [ ] **Step 5: 运行原学习者相关测试(若有 tests/test_learner_store.py)**

Run: `ls tests/ | grep -i learner; pytest tests/test_learner_store.py -v 2>&1 | tail -5`
若存在,更新该测试文件为 PG fixture 并全部转绿。

- [ ] **Step 6: Commit**

```bash
git add app/infrastructure/learner_store.py tests/test_pg_learner_store.py
git commit -m "feat: LearnerStore 迁移到 PostgreSQL"
```

---

## Task 10: SQLiteMemory → PGMemory(改名)

**Files:**
- Modify: `app/memory/sqlite_backend.py` → 重写为 PG 版,文件名保留 `sqlite_backend.py` 会在 M4 重命名;本任务改为创建 `app/memory/pg_memory.py` 并全项目引用更新
- Modify: `app/memory/__init__.py`(如导出 SQLiteMemory)
- Modify: `app/sync/service.py:73`(`SQLiteMemory(database_path)` 调用)
- Test: `tests/test_pg_memory.py`

- [ ] **Step 1: 写失败测试**

```python
"""PGMemory on PostgreSQL: episodes, facts, intervention stats."""
from __future__ import annotations

import pytest

from app.domain.learner import InterventionOutcome, MemoryFact
from app.infrastructure import pg
from app.infrastructure.migration_runner import migrate_database
from app.memory.pg_memory import PGMemory


@pytest.fixture()
def memory():
    admin = pg.connect_admin()
    migrate_database(admin)
    admin.close()
    conn = pg.connect()
    conn.execute("SELECT set_config('app.tenant_id', 'tenant_test', false)")
    conn.commit()
    yield PGMemory(conn)
    admin = pg.connect_admin()
    admin.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    admin.commit()
    admin.close()
    conn.close()


def test_upsert_fact_creates_fact(memory) -> None:
    episode = ...  # 构造 Episode(以原 sqlite_backend 测试为准)
    fact = memory.upsert_fact_for_episode(episode)
    assert fact.fact_id
    fetched = memory.get_facts("stu_1")
    assert len(fetched) == 1


def test_recall_episodes_empty_before_data(memory) -> None:
    assert memory.recall_episodes("stu_1") == []
```

(依据原 `tests/test_sqlite_memory.py` 的具体 Episode 构造方法补全。)

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_pg_memory.py -v`
Expected: FAIL(ModuleNotFoundError)

- [ ] **Step 3: 实现 pg_memory.py**

通读 `app/memory/sqlite_backend.py` 全部 348 行后按方言规则重写,签名 `__init__(self, connection: psycopg.Connection)`。`_episode_from_row`/`_fact_from_row`/`_stat_from_row` 的 dict 访问方式不变(row 已是 dict)。所有 `?` → `%s`,`sqlite3.Row` → `dict`。

- [ ] **Step 4: 更新引用**

```bash
grep -rn "SQLiteMemory\|sqlite_backend" app/ tests/ --include="*.py" | grep -v "migrations_pg\|test_pg_memory"
```
逐一改为 `from app.memory.pg_memory import PGMemory`,`SQLiteMemory(...)` → `PGMemory(...)`。

- [ ] **Step 5: 运行确认通过**

Run: `pytest tests/test_pg_memory.py -v`
Expected: passed

- [ ] **Step 6: 迁移原内存测试**

Run: `pytest tests/test_sqlite_memory.py tests/test_memory_deletion.py -v 2>&1 | tail -10`
更新这些测试文件为 PG fixture,全部转绿。

- [ ] **Step 7: Commit**

```bash
git add app/memory/pg_memory.py app/memory/__init__.py app/sync/service.py tests/test_pg_memory.py
git commit -m "feat: 记忆层迁移到 PostgreSQL(PGMemory)"
```

---

## Task 11: OutboxRepository → PG

**Files:**
- Modify: `app/memory/outbox.py`
- Modify: `app/memory/worker.py`
- Test: `tests/test_pg_outbox.py`

- [ ] **Step 1: 写失败测试(覆盖 enqueue/claim/complete/mark_failed/重试预算)**

```python
"""OutboxRepository on PostgreSQL."""
from __future__ import annotations

import pytest

from app.infrastructure import pg
from app.infrastructure.migration_runner import migrate_database
from app.memory.outbox import OutboxRepository, next_retry_delay_seconds


@pytest.fixture()
def repo():
    admin = pg.connect_admin()
    migrate_database(admin)
    admin.close()
    conn = pg.connect()
    conn.execute("SELECT set_config('app.tenant_id', 'tenant_test', false)")
    conn.commit()
    yield OutboxRepository(conn, default_student_id="stu_1")
    admin = pg.connect_admin()
    admin.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    admin.commit()
    admin.close()
    conn.close()


def test_enqueue_and_claim(repo) -> None:
    outbox_id = repo.enqueue(repo.connection, "{}", student_id="stu_1")
    record = repo.claim_due("worker_1")
    assert record is not None
    assert record.outbox_id == outbox_id
    assert record.state == "in_flight"


def test_complete_marks_done(repo) -> None:
    outbox_id = repo.enqueue(repo.connection, "{}", student_id="stu_1")
    record = repo.claim_due("worker_1")
    repo.complete(record.outbox_id)
    fetched = repo.get(outbox_id)
    assert fetched.state == "done"


def test_retry_delay_grows(repo) -> None:
    outbox_id = repo.enqueue(repo.connection, "{}", student_id="stu_1")
    first = repo.claim_due("w")
    repo.mark_failed(first.outbox_id, "boom")
    second = repo.claim_due("w")
    assert second is not None
    assert second.attempt_count == first.attempt_count + 1
```

(以原 `tests/test_memory_outbox.py` 的真实 API 为准;若签名不同以原文件为准。)

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_pg_outbox.py -v`
Expected: FAIL

- [ ] **Step 3: 重写 outbox.py 与 worker.py**

通读 `app/memory/outbox.py` 与 `app/memory/worker.py` 后按方言规则改写:
- `OutboxRepository.__init__(self, connection: psycopg.Connection, *, default_student_id=None)`
- `OutboxWorker.__init__(self, connection: psycopg.Connection, *, index=None)`(原来 `database_path`,检查 worker.py 全部用法)
- `?` → `%s`;`sqlite3.IntegrityError` → `UniqueViolation`;`lastrowid` → `RETURNING outbox_id`
- 幂等键、重试预算、state 机(pending→in_flight→done/failed)逻辑照抄

- [ ] **Step 4: 更新引用**

```bash
grep -rn "OutboxWorker(\|OutboxRepository(" app/ tests/ scripts/ --include="*.py"
```
全部改为传入连接对象。`app/main.py` 与 lifespan 中的构造在 Task 14 一起改。

- [ ] **Step 5: 运行确认通过**

Run: `pytest tests/test_pg_outbox.py -v`
Expected: passed

- [ ] **Step 6: 迁移原 outbox 测试**

Run: `pytest tests/test_memory_outbox.py tests/test_memory_worker.py tests/test_memory_outbox_wiring.py -v 2>&1 | tail -10`
更新为 PG fixture,全部转绿。

- [ ] **Step 7: Commit**

```bash
git add app/memory/outbox.py app/memory/worker.py tests/test_pg_outbox.py
git commit -m "feat: Outbox 层迁移到 PostgreSQL"
```

---

## Task 12: SyncService → PG

**Files:**
- Modify: `app/sync/service.py`(912 行,最大文件)
- Test: `tests/test_pg_sync.py`(及原 sync 测试迁移)

- [ ] **Step 1: 先通读并记录全部 SQL 与构造**

Run: `sed -n 1,912p app/sync/service.py`
(记录所有 `database_path`、`?` 占位符、`sqlite3` 引用位置。)

- [ ] **Step 2: 写失败测试(基于原 sync 测试的最小闭环)**

```python
"""SyncService on PostgreSQL — device registration and event batch."""
from __future__ import annotations

import pytest

from app.infrastructure import pg
from app.infrastructure.migration_runner import migrate_database
from app.sync.service import SyncService


@pytest.fixture()
def service():
    admin = pg.connect_admin()
    migrate_database(admin)
    admin.close()
    conn = pg.connect()
    conn.execute("SELECT set_config('app.tenant_id', 'tenant_test', false)")
    conn.commit()
    yield SyncService(conn)
    admin = pg.connect_admin()
    admin.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    admin.commit()
    admin.close()
    conn.close()


def test_register_and_verify_device(service) -> None:
    device_id = service.register_device("stu_1", device_name="phone")
    service._verify_device(device_id, "stu_1")  # 不应抛错


def test_process_batch_round_trip(service) -> None:
    device_id = service.register_device("stu_1", device_name="phone")
    response = service.process_batch(...)  # 以原 sync 测试请求构造为准
    assert response.snapshot is not None
```

(以原 `tests/test_sync_service.py` 或 `tests/test_sync_protocol.py` 的真实请求构造为准,补全。)

- [ ] **Step 3: 重写 service.py**

- 构造:`SyncService.__init__(self, connection: psycopg.Connection)`;内部 `self.events = EventStore(connection)`、`self.learner = LearnerStore(connection)`、`self.memory = PGMemory(connection)`
- `apply_migrations(database_path)` 调用删除(main.py 统一迁移)
- 每个 `?` → `%s`;`sqlite3` 引用 → `psycopg`
- 保持 912 行内的业务逻辑逐行等价:_student_lock 锁、设备注册/吊销、批处理状态机、完整性哈希校验、序列递增、冲突记录、快照构建全部照抄
- `threading.Lock` 保持不变(进程内)

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_pg_sync.py -v`
Expected: passed

- [ ] **Step 5: 迁移原 sync 测试**

Run: `pytest tests/test_sync_protocol.py tests/test_sync_service.py -v 2>&1 | tail -10`
若有这些文件,更新为 PG fixture 并转绿。

- [ ] **Step 6: Commit**

```bash
git add app/sync/service.py tests/test_pg_sync.py
git commit -m "feat: SyncService 迁移到 PostgreSQL"
```

---

## Task 13: KnowledgeBackend → PG tsvector

**Files:**
- Modify: `app/knowledge/local_backend.py`(562 行)
- Modify: `app/knowledge/router.py`
- Test: `tests/test_pg_retrieval.py`

- [ ] **Step 1: 先通读并记录 FTS5 用法**

Run: `sed -n 1,562p app/knowledge/local_backend.py`
(记录 `MATCH` 查询、`_match_phrase`、`bm25` 排名的确切位置。)

- [ ] **Step 2: 写失败测试(迁移 golden eval 语义的最小集)**

```python
"""KnowledgeBackend on PostgreSQL tsvector — golden eval semantics."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.infrastructure import pg
from app.infrastructure.migration_runner import migrate_database
from app.knowledge.local_backend import KnowledgeBackend, index_pack

GOLDEN = Path("evals/retrieval/golden.jsonl")


@pytest.fixture()
def backend():
    admin = pg.connect_admin()
    migrate_database(admin)
    admin.close()
    conn = pg.connect()
    conn.execute("SELECT set_config('app.tenant_id', 'tenant_demo', false)")
    conn.commit()
    yield KnowledgeBackend(conn)
    admin = pg.connect_admin()
    admin.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    admin.commit()
    admin.close()
    conn.close()


def test_index_pack_populates_tsvector(backend, tmp_path: Path) -> None:
    # 复制 fixtures/packs 下的已发布包到 tmp 并索引
    from tests.conftest import PACKS_ROOT
    pack_dir = next(PACKS_ROOT.glob("math.linear_equations*"))
    index_pack(backend.connection, pack_dir)
    response = backend.retrieve("isolate x when there is a constant on the same side",
                                skill="linear_equations")
    ids = [r.content_id for r in response.results]
    assert "math.linear_equations.micro_lesson.001" in ids


def test_golden_queries_match(backend) -> None:
    # 索引 fixtures 全部包后逐条跑 golden.jsonl,期望命中集必须包含
    ...
```

- [ ] **Step 3: 重写 local_backend.py**

- 构造:`KnowledgeBackend.__init__(self, connection: psycopg.Connection, *, weights: dict | None = None)`;`self.database_path` 属性删除(router 的 `connect(backend.database_path)` 同步改)
- `index_pack(connection, pack_dir)`:`apply_migrations` 删除;`knowledge_fts` 从 FTS5 虚拟表改为普通表写入(`INSERT INTO knowledge_fts (...) VALUES (%s, ...) ON CONFLICT (content_id) DO UPDATE SET ...`,先 `DELETE FROM knowledge_fts` 保持重建语义)
- `_search`:SQLite `MATCH` 表达式替换为 `body_tsv @@ websearch_to_tsquery('english', %s)`,带 `%s` 参数绑定
- `_match_phrase`:改为构造 `websearch_to_tsquery` 安全查询串(保留 STOPWORDS 过滤、MIN_BARE_QUERY_TERM_HITS 语义)
- bm25 排名:改为 `ts_rank_cd(body_tsv, websearch_to_tsquery('english', %s))`,权重列在 rerank 阶段保持 WEIGHTS_V1
- `RetrievalResult`/`RetrievalResponse`/rerank/citation 校验逻辑**全部照抄不变**
- `knowledge/router.py`:依赖注入 `KnowledgeBackend(connection)` + `BRIDGESAT_KNOWLEDGE_DB` env → `BRIDGESAT_DB`(单一 DSN)

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_pg_retrieval.py -v`
Expected: passed

- [ ] **Step 5: 迁移检索测试与 golden eval**

Run: `pytest tests/test_retrieval.py tests/test_content_loader.py -v 2>&1 | tail -10`
更新为 PG fixture 全部转绿。

Run: `python scripts/run_retrieval_evals.py && cat reports/rag_eval.json`
Expected: 8 条 golden 全部命中(或等价通过)。

- [ ] **Step 6: Commit**

```bash
git add app/knowledge/local_backend.py app/knowledge/router.py tests/test_pg_retrieval.py
git commit -m "feat: KnowledgeBackend 迁移到 PG tsvector"
```

---

## Task 14: main.py 接线 + 租户中间件 + 全量回归

**Files:**
- Modify: `app/main.py`
- Modify: `app/knowledge/router.py`(依赖注入)
- Test: `tests/test_pg_api.py`(租户隔离 API 测试)

- [ ] **Step 1: 写失败测试(租户隔离 + 401)**

```python
"""API-level tenant isolation on PostgreSQL."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.infrastructure import pg
from app.infrastructure.migration_runner import migrate_database


@pytest.fixture()
def client():
    admin = pg.connect_admin()
    migrate_database(admin)
    admin.close()
    conn = pg.connect()
    conn.execute("SELECT set_config('app.tenant_id', 'tenant_test', false)")
    conn.commit()
    # main.py 应用工厂化后的 client(见 Task 14 Step 3)
    ...


def test_tenant_isolated_student_lookup(client) -> None:
    # 租户 A 创建的学生,token 在租户 B 下解析失败
    ...
```

- [ ] **Step 2: 重写 main.py**

- `DATABASE_PATH` 删除,改为 `DATABASE_DSN = os.getenv("BRIDGESAT_DB", DEFAULT_APP_DSN)` 与模块级连接 `database_connection = pg.connect()`(应用单连接;后续可换连接池)
- 迁移走 admin 连接:`_migration_admin = pg.connect_admin(); migrate_database(_migration_admin); _migration_admin.close()`(import 时)
- `repository = StudentRepository(database_connection)`、`token_store = TokenStore(database_connection)`
- 新增租户中间件(BaseHTTPMiddleware):

```python
class TenantContextMiddleware(BaseHTTPMiddleware):
    """Resolve the bearer token to (tenant_id, student_id) and set the RLS
    session variable for the rest of the request."""

    async def dispatch(self, request, call_next):
        authorization = request.headers.get("authorization", "")
        token = authorization.removeprefix("Bearer ").strip() if authorization.startswith("Bearer ") else None
        if token:
            resolved = token_store.resolve_tenant(token)
            if resolved is not None:
                tenant_id, student_id = resolved
                database_connection.execute(
                    "SELECT set_config('app.tenant_id', %s, true)", (tenant_id,)
                )
        response = await call_next(request)
        database_connection.rollback()
        return response
```

(注意:`set_config(..., true)` 用事务内 set_local;若单连接跨请求有状态泄漏,改为每次请求事务包裹,由 Task 14 Step 3 决定——要求:任何请求结束时必须 rollback,避免租户泄漏。)

- `OutboxWorker(database_connection, index=build_mnemis_index(...))` — 按 Task 11 签名
- `require_student` 保持

- [ ] **Step 3: 应用工厂化(如 main.py 当前无 app 工厂)**

检查当前 `app = FastAPI(...)` 是否可直接测试;若不能,将应用创建提取为 `create_app(connection)` 工厂,`main.py` 尾部 `app = create_app(...)`。测试用 `create_app(test_connection)`。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_pg_api.py -v`
Expected: passed

- [ ] **Step 5: 迁移 API 测试**

Run: `pytest tests/test_api.py tests/security/ -v 2>&1 | tail -20`
更新 `_fresh_app(tmp_path)` 为 PG 版本(连真实 PG、每测试清理 schema),全部转绿。`tests/security/conftest.py` 同步。

- [ ] **Step 6: 全量回归(此时应接近全绿)**

Run: `pytest tests/ -x -q 2>&1 | tail -30`
修复剩余失败(多为 fixture 未迁移)。记录当前失败清单。

- [ ] **Step 7: Commit**

```bash
git add app/main.py app/knowledge/router.py tests/test_pg_api.py
git commit -m "feat: main.py PG 接线 + 租户 RLS 中间件"
```

---

## Task 15: SQLite → PG 数据迁移脚本

**Files:**
- Create: `scripts/migrate_sqlite_to_pg.py`
- Test: `tests/test_pg_migrate_script.py`

- [ ] **Step 1: 写失败测试(迁移 demo 库后校验关键行)**

```python
"""SQLite → PG migration script behaviour."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


def test_script_runs_idempotently(tmp_path: Path) -> None:
    sqlite_db = tmp_path / "demo.db"
    # 用当前 data/bridgesat.db 或构造最小 SQLite 库
    proc = subprocess.run(
        [sys.executable, "scripts/migrate_sqlite_to_pg.py", str(sqlite_db)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    proc2 = subprocess.run(
        [sys.executable, "scripts/migrate_sqlite_to_pg.py", str(sqlite_db)],
        capture_output=True, text=True,
    )
    assert proc2.returncode == 0  # 幂等
```

- [ ] **Step 2: 写脚本**

```python
#!/usr/bin/env python3
"""One-shot migration: copy all rows from a legacy SQLite database into the
PostgreSQL target (all rows tagged tenant_demo). Idempotent: rows that
already exist in PG are skipped (ON CONFLICT DO NOTHING).

Usage: python scripts/migrate_sqlite_to_pg.py [sqlite_db_path]
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from app.infrastructure import pg
from app.infrastructure.migration_runner import migrate_database

ROOT = Path(__file__).resolve().parents[1]

# table -> columns to copy (empty = copy all except these excluded)
TABLES = {
    "students": None,
    "student_tokens": None,
    "student_skill_states": None,
    "study_plans": None,
    "study_sessions": None,
    "session_items": None,
    "answer_attempts": None,
    "learning_events": None,
    "agent_events": None,
    "misconception_evidence": None,
    "learning_episodes": None,
    "student_memory_facts": None,
    "intervention_stats": None,
    "devices": None,
    "session_branches": None,
    "sync_conflicts": None,
    "memory_outbox": None,
    "student_deletions": None,
    "content_items": None,
    "content_item_versions": None,
    "content_reviews": None,
    "content_packs": None,
    "content_pack_items": None,
    "skills": None,
    "skill_prerequisites": None,
    "content_sources": None,
    "legacy_mastery_imports": None,
}

TENANT_COLUMNS = (
    "students", "student_tokens", "student_skill_states", "study_plans",
    "study_sessions", "session_items", "answer_attempts", "learning_events",
    "agent_events", "misconception_evidence", "learning_episodes",
    "student_memory_facts", "intervention_stats", "devices",
    "session_branches", "sync_conflicts", "memory_outbox", "student_deletions",
)


def migrate(sqlite_path: Path) -> dict[str, int]:
    if not sqlite_path.is_file():
        raise SystemExit(f"SQLite database not found: {sqlite_path}")
    # 备份
    backup = sqlite_path.with_suffix(".pre-pg-migration.db")
    backup.write_bytes(sqlite_path.read_bytes())

    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    dst = pg.connect_admin()
    try:
        migrate_database(dst)
        dst.execute("SELECT set_config('app.tenant_id', 'tenant_demo', false)")
        dst.commit()
        counts: dict[str, int] = {}
        for table, _columns in TABLES.items():
            src_columns = [row["name"] for row in src.execute(f"PRAGMA table_info({table})")]
            if not src_columns:
                continue
            rows = src.execute(f"SELECT * FROM {table}").fetchall()
            if not rows:
                counts[table] = 0
                continue
            columns = [col for col in src_columns]
            if table in TENANT_COLUMNS and "tenant_id" not in columns:
                columns = ["tenant_id", *columns]
                insert_columns = ", ".join(columns)
                values_placeholder = ", ".join(["%s"] * len(columns))
                sql = (
                    f"INSERT INTO {table} ({insert_columns}) VALUES ({values_placeholder}) "
                    "ON CONFLICT DO NOTHING"
                )
                for row in rows:
                    dst.execute(
                        sql,
                        ["tenant_demo", *[row[col] for col in src_columns]],
                    )
            else:
                insert_columns = ", ".join(columns)
                values_placeholder = ", ".join(["%s"] * len(columns))
                sql = (
                    f"INSERT INTO {table} ({insert_columns}) VALUES ({values_placeholder}) "
                    "ON CONFLICT DO NOTHING"
                )
                for row in rows:
                    dst.execute(sql, [row[col] for col in src_columns])
            dst.commit()
            counts[table] = len(rows)
        return counts
    finally:
        src.close()
        dst.close()


if __name__ == "__main__":
    path = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "data" / "bridgesat.db")
    counts = migrate(path)
    total = sum(counts.values())
    print(f"migrated {total} rows across {len(counts)} tables to tenant_demo")
    for table, n in sorted(counts.items()):
        print(f"  {table}: {n}")
```

注意:`knowledge_fts` 与 `knowledge_index_log` 不入迁移清单(tsvector 索引由 `index_pack` 重建);迁移前确认 `content_items` 的表结构在 PG 中已存在(0002)。

- [ ] **Step 3: 运行确认通过**

Run: `pytest tests/test_pg_migrate_script.py -v`
Expected: passed

- [ ] **Step 4: 用真实 demo 库端到端验证**

Run: `python scripts/migrate_sqlite_to_pg.py && python -c "
import sys; sys.path.insert(0, '.')
from app.infrastructure import pg
from app.infrastructure.migration_runner import migrate_database
conn = pg.connect_admin(); migrate_database(conn)
conn.execute(\"SELECT set_config('app.tenant_id', 'tenant_demo', false)\")
n = conn.execute('SELECT COUNT(*) AS n FROM students').fetchone()['n']
print('students in PG:', n); conn.close()
"`
Expected: 迁移前 `data/bridgesat.db` 的学生数 == PG 中计数。

- [ ] **Step 5: Commit**

```bash
git add scripts/migrate_sqlite_to_pg.py tests/test_pg_migrate_script.py
git commit -m "feat: SQLite → PostgreSQL 一次性迁移脚本"
```

---

## Task 16: M4 收尾 — 删 SQLite 后端、文档更新、全量回归

**Files:**
- Delete: `app/infrastructure/database.py`、`app/infrastructure/migrations/`
- Modify: `.env.example`、`docs/ARCHITECTURE.md`、`README.md`、`docs/IMPLEMENTATION_PLAN.md`(如引用 sqlite)
- Modify: 清理所有 `sqlite3` import(除 migrate 脚本)

- [ ] **Step 1: 清理 sqlite 引用**

Run: `grep -rn "sqlite3\|database.py\|from .database\|from app.infrastructure.database" app/ tests/ scripts/ --include="*.py" | grep -v "migrate_sqlite_to_pg\|migrations_pg"`
逐处清理:删除 `app/infrastructure/database.py`、`app/infrastructure/migrations/`,更新所有 import 为 `app.infrastructure.pg`。

- [ ] **Step 2: 删除 SQLite 迁移目录**

Run: `rm -rf app/infrastructure/migrations app/infrastructure/database.py && python -m compileall app tests 2>&1 | tail -5`
Expected: 无编译错误(除已知待改测试)。

- [ ] **Step 3: 更新配置文档**

- `.env.example`:`BRIDGESAT_DB=postgresql://bridgesat:bridgesat@localhost:5432/bridgesat`、删除 `BRIDGESAT_KNOWLEDGE_DB`、新增 `BRIDGESAT_EMBEDDING_MODEL` 占位(Plan 2 启用)
- `docs/ARCHITECTURE.md` §12:`SQLite FTS5` → `PostgreSQL tsvector(Level 1 兜底)`,注明 Milvus 为 Plan 2
- `README.md`:安装步骤加 `docker compose up -d`、`python scripts/dev_env.py up`;测试命令加 PG 前置说明

- [ ] **Step 4: 全量回归**

Run: `python scripts/dev_env.py up && pytest tests/ -q 2>&1 | tail -5`
Expected: 278+ passed(原有 278 + 新增 PG 测试)

Run: `python -m evals.run_all && node --test web/tests/*.test.js 2>&1 | tail -5`
Expected: 12 `[ok]` + 21 pass(offline_sync/retrieval eval 若依赖 SQLite fixture 需同步改,见 Task 13)

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore: 清理 SQLite 后端,文档更新,全量回归绿"
```

---

## 自审

**Spec 覆盖检查:**
- docker-compose + 本地 PG → Task 1
- pg 连接层 → Task 2
- 迁移器 PG 化 → Task 3
- 8 个迁移脚本(含 tenant_id、tsvector、RLS)→ Task 4、5
- StudentRepository → Task 6
- TokenStore + 租户解析 → Task 7
- EventStore → Task 8
- LearnerStore → Task 9
- 记忆层 → Task 10
- Outbox/Worker → Task 11
- SyncService → Task 12
- KnowledgeBackend tsvector → Task 13
- main.py 接线 + 租户中间件 → Task 14
- SQLite→PG 迁移脚本 → Task 15
- 删 SQLite、文档 → Task 16

**类型一致性:** 所有存储模块统一构造签名 `__init__(self, connection: psycopg.Connection)`;`apply_migrations(database_path)` 旧签名全部替换为 `migrate_database(connection)`(Task 3 定义,Task 4-16 使用)。`KnowledgeBackend(connection)`、`PGMemory(connection)`、`OutboxWorker(connection, index=...)` 名称一致。

**遗留已知项(Plan 2):** Milvus 向量检索、embedding 索引、双路融合、`BRIDGESAT_MILVUS_URI`、vector_golden 评估。

**风险:** tsvector 与 FTS5 查询语义差异(排名、词形)可能在 golden eval 边界用例上漂移——Task 13 Step 5 的 rag_eval 是硬门槛;若个别 golden 条目不达,允许在 TSVECTOR 兜底路径调整 STOPWORDS/HOW_TO_CUES 集合并在 dev.jsonl 上验证,不改 golden 断言。
