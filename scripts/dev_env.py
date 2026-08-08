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
    print(f"DSN: {DSN}")
    print(f"APP DSN: {APP_DSN}")


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
