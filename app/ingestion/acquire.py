from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from .fetcher import FetchResult, SafeFetcher
from .registry import SourceRecord, SourceRegistry


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REGISTRY = ROOT / "config" / "sources.yaml"
DEFAULT_OUTPUT = ROOT / "data" / "acquisition"


@dataclass(frozen=True, slots=True)
class Artifact:
    source_id: str
    purpose: str
    requested_url: str
    final_url: str
    local_path: str
    sha256: str
    size_bytes: int
    content_type: str
    fetched_at: str
    registry_status: str
    license_id: str
    allowed_actions: list[str]
    review_status: str
    reused: bool


def _artifact(source: SourceRecord, purpose: str, fetched: FetchResult) -> Artifact:
    return Artifact(
        source_id=source.id,
        purpose=purpose,
        requested_url=fetched.requested_url,
        final_url=fetched.final_url,
        local_path=fetched.local_path,
        sha256=fetched.sha256,
        size_bytes=fetched.size_bytes,
        content_type=fetched.content_type,
        fetched_at=fetched.fetched_at,
        registry_status=source.status,
        license_id=str(source.license.get("id", "unknown")),
        allowed_actions=sorted(source.allowed_actions),
        review_status="source_acquired_item_review_pending",
        reused=fetched.reused,
    )


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as output:
        for row in rows:
            output.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            count += 1
    return count


def _download(
    fetcher: SafeFetcher,
    source: SourceRecord,
    url: str,
    destination: Path,
    *,
    purpose: str,
    hosts: set[str],
    max_bytes: int,
) -> Artifact:
    result = fetcher.download(
        url,
        destination,
        allowed_hosts=hosts,
        max_bytes=max_bytes,
        interval_seconds=float(source.acquisition.get("rate_limit_seconds") or 1.0),
    )
    return _artifact(source, purpose, result)


def acquire_deepmind(
    registry: SourceRegistry,
    fetcher: SafeFetcher,
    output_root: Path,
) -> tuple[list[Artifact], dict[str, Any]]:
    source = registry.acquire("deepmind_mathematics_dataset", "download")
    base = output_root / source.id
    artifacts = [
        _download(
            fetcher,
            source,
            "https://github.com/google-deepmind/mathematics_dataset/archive/refs/heads/master.zip",
            base / "raw" / "mathematics_dataset-master.zip",
            purpose="candidate_generator_source",
            hosts={"github.com", "codeload.github.com"},
            max_bytes=25_000_000,
        ),
        _download(
            fetcher,
            source,
            "https://raw.githubusercontent.com/google-deepmind/mathematics_dataset/master/LICENSE",
            base / "raw" / "LICENSE.txt",
            purpose="license_snapshot",
            hosts={"raw.githubusercontent.com"},
            max_bytes=100_000,
        ),
        _download(
            fetcher,
            source,
            "https://raw.githubusercontent.com/google-deepmind/mathematics_dataset/master/README.md",
            base / "raw" / "README.md",
            purpose="upstream_documentation",
            hosts={"raw.githubusercontent.com"},
            max_bytes=500_000,
        ),
    ]
    summary = {
        "source_id": source.id,
        "mode": "candidate_generator",
        "include_modules": source.raw.get("filters", {}).get("include_modules", []),
        "exclude_modules": source.raw.get("filters", {}).get("exclude_modules", []),
        "generation_status": "source_downloaded_dependencies_required",
        "product_use": "rewrite_and_human_review_required",
    }
    generation_report_path = base / "staging" / "generation-report.json"
    if generation_report_path.exists():
        generation_report = json.loads(generation_report_path.read_text(encoding="utf-8"))
        summary.update(
            {
                "generation_status": "candidates_generated",
                "candidate_count": int(generation_report.get("candidate_count", 0)),
                "candidate_path": str(base / "staging" / "candidates.jsonl"),
                "generated_at": generation_report.get("generated_at"),
            }
        )
    _write_json(base / "staging" / "generator-plan.json", summary)
    return artifacts, summary


_GUTENBERG_KEYWORDS = re.compile(
    r"grammar|composition|essay|science|history|biography|speech|short stor|nature|mathematics|algebra|geometry",
    re.IGNORECASE,
)


def _gutenberg_candidates(catalog_path: Path, limit: int) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    with gzip.open(catalog_path, "rt", encoding="utf-8-sig", errors="replace", newline="") as input_file:
        reader = csv.DictReader(input_file)
        for row in reader:
            language = (row.get("Language") or row.get("Languages") or "").lower()
            languages = {part.strip() for part in re.split(r"[,;|]", language)}
            if "en" not in languages:
                continue
            searchable = " ".join(
                [row.get("Title", ""), row.get("Subjects", ""), row.get("Bookshelves", "")]
            )
            if not _GUTENBERG_KEYWORDS.search(searchable):
                continue
            ebook_id = row.get("Text#") or row.get("Text Number") or row.get("ID") or ""
            candidates.append(
                {
                    "source_id": "project_gutenberg",
                    "upstream_id": ebook_id,
                    "title": row.get("Title", "").strip(),
                    "creator": row.get("Authors", "").strip(),
                    "language": language,
                    "subjects": row.get("Subjects", "").strip(),
                    "bookshelves": row.get("Bookshelves", "").strip(),
                    "issued": row.get("Issued", "").strip(),
                    "canonical_url": f"https://www.gutenberg.org/ebooks/{ebook_id}" if ebook_id else None,
                    "rights_statement": "item-level public-domain and jurisdiction review required",
                    "review_status": "item_review_pending",
                    "allowed_use": "candidate reading passage only",
                }
            )
            if len(candidates) >= limit:
                break
    return candidates


def acquire_gutenberg(
    registry: SourceRegistry,
    fetcher: SafeFetcher,
    output_root: Path,
    *,
    limit: int,
) -> tuple[list[Artifact], dict[str, Any]]:
    source = registry.acquire("project_gutenberg", "download")
    base = output_root / source.id
    artifacts = [
        _download(
            fetcher,
            source,
            "https://www.gutenberg.org/cache/epub/feeds/pg_catalog.csv.gz",
            base / "raw" / "pg_catalog.csv.gz",
            purpose="official_machine_readable_catalog",
            hosts={"www.gutenberg.org"},
            max_bytes=10_000_000,
        ),
        _download(
            fetcher,
            source,
            "https://www.gutenberg.org/policy/robot_access.html",
            base / "raw" / "robot_access.html",
            purpose="access_policy_snapshot",
            hosts={"www.gutenberg.org"},
            max_bytes=500_000,
        ),
        _download(
            fetcher,
            source,
            "https://www.gutenberg.org/ebooks/offline_catalogs.html",
            base / "raw" / "offline_catalogs.html",
            purpose="catalog_documentation_snapshot",
            hosts={"www.gutenberg.org"},
            max_bytes=1_000_000,
        ),
    ]
    candidates = _gutenberg_candidates(base / "raw" / "pg_catalog.csv.gz", limit)
    count = _write_jsonl(base / "staging" / "candidates.jsonl", candidates)
    return artifacts, {
        "source_id": source.id,
        "candidate_count": count,
        "candidate_path": str(base / "staging" / "candidates.jsonl"),
        "review_status": "item_rights_educational_and_age_review_pending",
    }


def _load_loc_rows(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("results", "items", "data"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    return []


def _loc_candidates(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    def value(row: dict[str, Any], *names: str) -> Any:
        lowered = {str(key).lower(): item for key, item in row.items()}
        for name in names:
            item = lowered.get(name.lower())
            if item not in (None, "", [], {}):
                return item
        return None

    def names(value_: Any) -> list[str]:
        if not isinstance(value_, list):
            return []
        result: list[str] = []
        for item in value_:
            if isinstance(item, dict):
                name = item.get("Name") or item.get("name")
                if name:
                    result.append(str(name))
            elif item:
                result.append(str(item))
        return result

    candidates: list[dict[str, Any]] = []
    for row in rows[:limit]:
        item_id = value(row, "id", "item_id", "identifier", "lccn")
        creators = names(value(row, "creators")) or names(value(row, "contributors"))
        rights = value(row, "rights", "rights_statement", "rights_advisory")
        repositories = value(row, "repository")
        candidates.append(
            {
                "source_id": "library_of_congress_free_to_use",
                "upstream_id": item_id,
                "title": value(row, "title", "item_title") or "Untitled",
                "creator": creators,
                "date": value(row, "date", "created_published", "date_text"),
                "description": value(row, "description", "summary", "notes"),
                "subjects": value(row, "subjects", "subject", "subject_headings"),
                "canonical_url": value(row, "url", "item_url") or item_id,
                "rights_statement": rights
                or "collection and item rights review required",
                "credit_line": value(row, "credit_line") or repositories,
                "resource_type": value(row, "type_of_resource", "original_format"),
                "online_format": value(row, "online_format", "mime_type"),
                "iiif_manifest": value(row, "iiif_manifest"),
                "preview_url": value(row, "preview_url"),
                "review_status": "item_review_pending",
                "allowed_use": "candidate multimodal or reading material only",
            }
        )
    return candidates


def acquire_loc(
    registry: SourceRegistry,
    fetcher: SafeFetcher,
    output_root: Path,
    *,
    limit: int,
) -> tuple[list[Artifact], dict[str, Any]]:
    source = registry.acquire("library_of_congress_free_to_use", "download")
    base = output_root / source.id
    artifacts = [
        _download(
            fetcher,
            source,
            "https://data.labs.loc.gov/free-to-use/sample-data/metadata.json",
            base / "raw" / "sample-metadata.json",
            purpose="official_sample_metadata",
            hosts={"data.labs.loc.gov"},
            max_bytes=2_000_000,
        ),
        _download(
            fetcher,
            source,
            "https://data.labs.loc.gov/free-to-use/sample-data/manifest.json",
            base / "raw" / "sample-manifest.json",
            purpose="official_sample_manifest",
            hosts={"data.labs.loc.gov"},
            max_bytes=500_000,
        ),
        _download(
            fetcher,
            source,
            "https://data.labs.loc.gov/free-to-use/README.md",
            base / "raw" / "README.md",
            purpose="dataset_documentation_and_rights_snapshot",
            hosts={"data.labs.loc.gov"},
            max_bytes=500_000,
        ),
    ]
    rows = _load_loc_rows(base / "raw" / "sample-metadata.json")
    candidates = _loc_candidates(rows, limit)
    count = _write_jsonl(base / "staging" / "candidates.jsonl", candidates)
    return artifacts, {
        "source_id": source.id,
        "downloaded_metadata_rows": len(rows),
        "candidate_count": count,
        "candidate_path": str(base / "staging" / "candidates.jsonl"),
        "review_status": "rights_educational_age_and_accessibility_review_pending",
    }


def _take_jsonl(path: Path, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as input_file:
        for line in input_file:
            if not line.strip():
                continue
            row = json.loads(line)
            row["source_id"] = "gsm8k"
            row["usage_partition"] = "isolated_internal_evaluation"
            rows.append(row)
            if len(rows) >= limit:
                break
    return rows


def acquire_gsm8k(
    registry: SourceRegistry,
    fetcher: SafeFetcher,
    output_root: Path,
    *,
    limit: int,
) -> tuple[list[Artifact], dict[str, Any]]:
    source = registry.acquire("gsm8k", "download")
    base = output_root / source.id
    artifacts = [
        _download(
            fetcher,
            source,
            "https://raw.githubusercontent.com/openai/grade-school-math/master/grade_school_math/data/test.jsonl",
            base / "raw" / "test.jsonl",
            purpose="isolated_evaluation_dataset",
            hosts={"raw.githubusercontent.com"},
            max_bytes=5_000_000,
        ),
        _download(
            fetcher,
            source,
            "https://raw.githubusercontent.com/openai/grade-school-math/master/LICENSE",
            base / "raw" / "LICENSE.txt",
            purpose="license_snapshot",
            hosts={"raw.githubusercontent.com"},
            max_bytes=100_000,
        ),
    ]
    sample = _take_jsonl(base / "raw" / "test.jsonl", limit)
    count = _write_jsonl(base / "staging" / "evaluation-sample.jsonl", sample)
    return artifacts, {
        "source_id": source.id,
        "evaluation_sample_count": count,
        "sample_path": str(base / "staging" / "evaluation-sample.jsonl"),
        "separation_rule": "must_not_enter_product_rag_or_offline_pack",
    }


ACQUIRERS = {
    "deepmind_mathematics_dataset": acquire_deepmind,
    "project_gutenberg": acquire_gutenberg,
    "library_of_congress_free_to_use": acquire_loc,
    "gsm8k": acquire_gsm8k,
}


def acquire_sources(
    *,
    registry_path: Path,
    output_root: Path,
    source_ids: list[str],
    limit: int,
) -> dict[str, Any]:
    registry = SourceRegistry(registry_path)
    registry.validate_restricted_sources()
    fetcher = SafeFetcher()
    artifacts: list[Artifact] = []
    summaries: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for source_id in source_ids:
        acquire_fn = ACQUIRERS.get(source_id)
        if acquire_fn is None:
            errors.append({"source_id": source_id, "error": "no governed acquirer implemented"})
            continue
        try:
            if source_id in {"project_gutenberg", "library_of_congress_free_to_use", "gsm8k"}:
                new_artifacts, summary = acquire_fn(registry, fetcher, output_root, limit=limit)
            else:
                new_artifacts, summary = acquire_fn(registry, fetcher, output_root)
            artifacts.extend(new_artifacts)
            summaries.append(summary)
        except Exception as exc:  # One endpoint failure must not discard other sources.
            errors.append({"source_id": source_id, "error": f"{type(exc).__name__}: {exc}"})

    artifact_rows = [asdict(item) for item in artifacts]
    _write_jsonl(output_root / "artifacts.jsonl", artifact_rows)
    report = {
        "registry": str(registry_path),
        "output_root": str(output_root),
        "source_ids": source_ids,
        "artifact_count": len(artifacts),
        "artifact_bytes": sum(item.size_bytes for item in artifacts),
        "summaries": summaries,
        "errors": errors,
        "run_fingerprint": hashlib.sha256(
            json.dumps(artifact_rows, sort_keys=True).encode("utf-8")
        ).hexdigest(),
    }
    _write_json(output_root / "run-report.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Acquire governed BridgeSAT data sources")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--sources", nargs="+", default=list(ACQUIRERS), choices=sorted(ACQUIRERS)
    )
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args(argv)
    report = acquire_sources(
        registry_path=args.registry,
        output_root=args.output,
        source_ids=args.sources,
        limit=max(1, min(args.limit, 1000)),
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
