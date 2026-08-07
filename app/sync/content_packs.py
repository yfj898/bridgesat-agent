"""Content pack download endpoints for offline installation."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException

from app.question_bank import packs_root

router = APIRouter(prefix="/v1/content-packs", tags=["content-packs"])


def _available_packs() -> dict[str, Path]:
    available: dict[str, Path] = {}
    for pack_dir in sorted(packs_root().iterdir()):
        if not pack_dir.is_dir():
            continue
        manifest_path = pack_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("status") != "published":
            continue
        pack_version = manifest.get("pack_version") or pack_dir.name
        available[pack_version] = pack_dir
    return available


@router.get("")
def list_packs() -> dict:
    return {"packs": sorted(_available_packs().keys())}


@router.get("/{pack_version}")
def get_pack(pack_version: str) -> dict:
    available = _available_packs()
    pack_dir = available.get(pack_version)
    if pack_dir is None:
        raise HTTPException(status_code=404, detail=f"Pack {pack_version} not found")
    manifest = json.loads((pack_dir / "manifest.json").read_text())
    items = [
        json.loads(line)
        for line in (pack_dir / "items.jsonl").read_text().splitlines()
        if line.strip()
    ]
    lessons_path = pack_dir / "lessons.jsonl"
    lessons = (
        [
            json.loads(line)
            for line in lessons_path.read_text().splitlines()
            if line.strip()
        ]
        if lessons_path.exists()
        else []
    )
    return {
        "pack_version": pack_version,
        "manifest": manifest,
        "items": items,
        "lessons": lessons,
    }
