#!/usr/bin/env python3
"""Import a built content pack into the content registry and FTS index.

Usage:
    python scripts/import_content_pack.py [--db PATH] [--pack PATH]

Default database is BRIDGESAT_DB or ./bridgesat.db; default pack is the
latest built bridgesat-math pack under content/packs/.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.content_pipeline.contracts import PACKS_DIR
from app.content_pipeline.importing import import_pack, verify_import
from app.knowledge.local_backend import index_pack


def _default_db() -> Path:
    return Path(os.getenv("BRIDGESAT_DB", ROOT / "bridgesat.db"))


def _default_pack() -> Path:
    candidates = sorted(PACKS_DIR.glob("bridgesat-math-*"), reverse=True)
    if not candidates:
        print("No built pack found under content/packs/", file=sys.stderr)
        sys.exit(1)
    return candidates[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--pack", type=Path, default=None)
    args = parser.parse_args()

    db = args.db or _default_db()
    pack = args.pack or _default_pack()
    inserted = import_pack(db, pack)
    summary = verify_import(db)
    indexed = index_pack(db, pack)
    print(f"Imported {inserted} items from {pack.name} into {db}")
    print(f"Registry: {summary}")
    print(f"Indexed: {indexed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
