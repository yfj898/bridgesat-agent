from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class RegistryError(RuntimeError):
    """Raised when an acquisition violates the source registry."""


@dataclass(frozen=True, slots=True)
class SourceRecord:
    id: str
    name: str
    status: str
    official_url: str | None
    allowed_actions: frozenset[str]
    prohibited_actions: frozenset[str]
    acquisition: dict[str, Any]
    license: dict[str, Any]
    governance: dict[str, Any]
    raw: dict[str, Any]

    def require_action(self, action: str) -> None:
        if action in self.prohibited_actions:
            raise RegistryError(f"{self.id}: action {action!r} is explicitly prohibited")
        if action not in self.allowed_actions:
            raise RegistryError(f"{self.id}: action {action!r} is not allowed")

    def require_acquisition(self) -> None:
        if not bool(self.acquisition.get("enabled", False)):
            raise RegistryError(f"{self.id}: acquisition is disabled")


class SourceRegistry:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        payload = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise RegistryError("source registry must be a mapping")
        rows = payload.get("sources", [])
        if not isinstance(rows, list):
            raise RegistryError("sources must be a list")

        self.payload = payload
        self.sources: dict[str, SourceRecord] = {}
        for row in rows:
            if not isinstance(row, dict) or not row.get("id"):
                raise RegistryError("every source needs a string id")
            source_id = str(row["id"])
            if source_id in self.sources:
                raise RegistryError(f"duplicate source id: {source_id}")
            self.sources[source_id] = SourceRecord(
                id=source_id,
                name=str(row.get("name", source_id)),
                status=str(row.get("status", "prohibited")),
                official_url=row.get("official_url"),
                allowed_actions=frozenset(str(v) for v in row.get("allowed_actions", [])),
                prohibited_actions=frozenset(str(v) for v in row.get("prohibited_actions", [])),
                acquisition=dict(row.get("acquisition", {})),
                license=dict(row.get("license", {})),
                governance=dict(row.get("governance", {})),
                raw=row,
            )

    def get(self, source_id: str) -> SourceRecord:
        try:
            return self.sources[source_id]
        except KeyError as exc:
            raise RegistryError(f"unknown source: {source_id}") from exc

    def acquire(self, source_id: str, action: str = "download") -> SourceRecord:
        source = self.get(source_id)
        source.require_acquisition()
        source.require_action(action)
        return source

    def validate_restricted_sources(self) -> None:
        for source in self.sources.values():
            if source.status in {"reference_only", "prohibited"}:
                if source.acquisition.get("enabled", False):
                    raise RegistryError(f"restricted source acquisition enabled: {source.id}")
                if source.acquisition.get("crawler_enabled", False):
                    raise RegistryError(f"restricted source crawler enabled: {source.id}")
