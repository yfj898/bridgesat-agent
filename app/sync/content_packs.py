"""Content pack download endpoints for offline installation.

The PostgreSQL content registry is the runtime authority. Local pack files are
used by import/build tooling, never as a student-facing serving fallback.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request

from app.request_context import request_connection

router = APIRouter(prefix="/v1/content-packs", tags=["content-packs"])


def _published_pack_versions(request: Request) -> list[str]:
    rows = request_connection(request).execute(
        """
        SELECT DISTINCT pack_version
        FROM content_packs
        WHERE status = 'published'
        ORDER BY pack_version
        """
    ).fetchall()
    return [row["pack_version"] for row in rows]


def _registry_pack(request: Request, pack_version: str) -> dict:
    connection = request_connection(request)
    pack_rows = connection.execute(
        """
        SELECT pack_id, pack_version, manifest_json
        FROM content_packs
        WHERE pack_version = %s AND status = 'published'
        """,
        (pack_version,),
    ).fetchall()
    if len(pack_rows) != 1:
        raise HTTPException(
            status_code=409 if pack_rows else 404,
            detail=(
                f"Pack {pack_version} is ambiguous in the content registry"
                if pack_rows
                else f"Pack {pack_version} not found"
            ),
        )
    pack = pack_rows[0]
    try:
        manifest = json.loads(pack["manifest_json"] or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=503, detail="Invalid content registry manifest") from exc
    if (
        manifest.get("pack_id") != pack["pack_id"]
        or manifest.get("pack_version") != pack_version
        or manifest.get("status") != "published"
    ):
        raise HTTPException(status_code=503, detail="Content registry manifest mismatch")

    rows = connection.execute(
        """
        SELECT ci.content_type, civ.item_json
        FROM content_pack_items AS cpi
        JOIN content_items AS ci
          ON ci.content_id = cpi.content_id
         AND ci.version = cpi.version
        JOIN content_item_versions AS civ
          ON civ.content_id = cpi.content_id
         AND civ.version = cpi.version
        WHERE cpi.pack_id = %s
          AND ci.review_status = 'approved'
          AND ci.status = 'approved'
          AND ci.withdrawn_at IS NULL
        ORDER BY ci.content_id
        """,
        (pack["pack_id"],),
    ).fetchall()
    items: list[dict] = []
    lessons: list[dict] = []
    for row in rows:
        try:
            item = json.loads(row["item_json"] or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=503, detail="Invalid content registry item") from exc
        if row["content_type"] == "question":
            items.append(item)
        else:
            lessons.append(item)
    return {
        "pack_version": pack_version,
        "manifest": manifest,
        "items": items,
        "lessons": lessons,
    }


@router.get("")
def list_packs(request: Request) -> dict:
    return {"packs": _published_pack_versions(request)}


@router.get("/{pack_version}")
def get_pack(pack_version: str, request: Request) -> dict:
    return _registry_pack(request, pack_version)
