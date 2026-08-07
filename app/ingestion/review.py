from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .acquire import _load_loc_rows, _loc_candidates
from .registry import SourceRecord, SourceRegistry


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REGISTRY = ROOT / "config" / "sources.yaml"
DEFAULT_ACQUISITION = ROOT / "data" / "acquisition"
DEFAULT_OUTPUT = ROOT / "data" / "reviewed"

MVP_SKILLS = {
    "linear_equations",
    "systems_equations",
    "ratios_percentages",
    "functions_models",
    "main_idea_inference",
    "evidence_selection",
    "words_in_context",
    "sentence_boundaries",
}

MODULE_MAPPING: dict[str, dict[str, Any]] = {
    "algebra__linear_1d": {
        "primary_skill": "linear_equations",
        "subskill": "isolate_variables",
        "role": "question_candidate",
    },
    "algebra__linear_2d": {
        "primary_skill": "systems_equations",
        "subskill": "solve_systems",
        "role": "question_candidate",
    },
    "measurement__conversion": {
        "primary_skill": "ratios_percentages",
        "subskill": "unit_rates",
        "role": "question_candidate",
    },
    "measurement__time": {
        "primary_skill": "ratios_percentages",
        "subskill": "unit_rates",
        "role": "question_candidate",
    },
    "polynomials__evaluate": {
        "primary_skill": "functions_models",
        "subskill": "function_evaluation",
        "role": "question_candidate",
    },
    "polynomials__expand": {
        "primary_skill": "functions_models",
        "subskill": "algebraic_models",
        "role": "question_candidate",
    },
    "arithmetic__add_or_sub": {
        "primary_skill": None,
        "prerequisite": "arithmetic_operations",
        "role": "prerequisite_candidate",
    },
    "arithmetic__mul": {
        "primary_skill": None,
        "prerequisite": "arithmetic_operations",
        "role": "prerequisite_candidate",
    },
    "arithmetic__div": {
        "primary_skill": None,
        "prerequisite": "arithmetic_operations",
        "role": "prerequisite_candidate",
    },
    "probability__swr_p_sequence": {
        "primary_skill": None,
        "prerequisite": None,
        "role": "out_of_scope_candidate",
    },
}

READING_KEYWORDS: dict[str, tuple[str, ...]] = {
    "sentence_boundaries": (
        "grammar",
        "composition",
        "punctuation",
        "sentence",
        "syntax",
        "writing",
    ),
    "words_in_context": (
        "dictionary",
        "vocabulary",
        "language",
        "rhetoric",
        "speech",
        "essay",
    ),
    "evidence_selection": (
        "history",
        "science",
        "biography",
        "government",
        "law",
        "argument",
        "research",
    ),
    "main_idea_inference": (
        "short stories",
        "fiction",
        "literature",
        "nature",
        "philosophy",
        "memoir",
        "narrative",
    ),
}

SENSITIVE_CONTEXT: dict[str, tuple[str, ...]] = {
    "violence_or_conflict": (
        "war",
        "battle",
        "bombing",
        "murder",
        "death",
        "weapon",
        "violence",
        "blood",
    ),
    "discrimination_or_historical_trauma": (
        "slavery",
        "segregation",
        "racism",
        "lynching",
        "holocaust",
        "genocide",
    ),
    "substances_or_adult_context": (
        "alcohol",
        "tobacco",
        "drug",
        "gambling",
    ),
    "medical_or_distressing": (
        "disease",
        "injury",
        "hospital",
        "disaster",
    ),
}


@dataclass(frozen=True, slots=True)
class Duplicate:
    duplicate_id: str
    canonical_id: str
    reason: str
    similarity: float


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number}: expected an object")
        rows.append(value)
    return rows


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    materialized = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in materialized:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return len(materialized)


def _flatten(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_flatten(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_flatten(item) for item in value)
    return str(value)


def normalize_text(value: Any) -> str:
    text = _flatten(value).lower()
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[^a-z0-9%+\-*/=.' ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tokens(value: Any) -> set[str]:
    return {token for token in normalize_text(value).split() if len(token) > 1}


def _contains_term(text: str, term: str) -> bool:
    normalized_term = normalize_text(term)
    if not normalized_term:
        return False
    pattern = r"(?<![a-z0-9])" + re.escape(normalized_term) + r"(?![a-z0-9])"
    return re.search(pattern, text) is not None


def _identity_text(row: dict[str, Any]) -> str:
    return normalize_text(
        [
            row.get("question"),
            row.get("answer"),
            row.get("title"),
            row.get("creator"),
            row.get("description"),
            row.get("subjects"),
            row.get("canonical_url"),
        ]
    )


def _stable_id(row: dict[str, Any]) -> str:
    existing = row.get("id")
    if existing:
        return str(existing)
    source = str(row.get("source_id", "unknown"))
    upstream = row.get("upstream_id") or row.get("canonical_url") or _identity_text(row)
    digest = hashlib.sha256(f"{source}\n{upstream}".encode("utf-8")).hexdigest()[:16]
    return f"{source}-{digest}"


def _record_hash(row: dict[str, Any]) -> str:
    return hashlib.sha256(_identity_text(row).encode("utf-8")).hexdigest()


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def deduplicate(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[Duplicate]]:
    kept: list[dict[str, Any]] = []
    duplicates: list[Duplicate] = []
    exact: dict[str, str] = {}
    token_cache: list[tuple[str, set[str], str]] = []

    for row in rows:
        row_id = _stable_id(row)
        row["id"] = row_id
        digest = _record_hash(row)
        row["normalized_content_hash"] = digest
        if digest in exact:
            duplicates.append(Duplicate(row_id, exact[digest], "exact_normalized_match", 1.0))
            continue

        identity = _identity_text(row)
        tokens = _tokens(identity)
        near_match: tuple[str, float] | None = None
        if len(tokens) >= 5:
            for candidate_id, candidate_tokens, candidate_source in token_cache:
                # Near duplicates are only collapsed inside the same source. Cross-source
                # matches are useful contamination evidence and are retained for review.
                if candidate_source != str(row.get("source_id")):
                    continue
                similarity = _jaccard(tokens, candidate_tokens)
                if similarity >= 0.94:
                    near_match = (candidate_id, similarity)
                    break
        if near_match:
            duplicates.append(Duplicate(row_id, near_match[0], "near_duplicate", near_match[1]))
            continue

        exact[digest] = row_id
        token_cache.append((row_id, tokens, str(row.get("source_id"))))
        kept.append(row)
    return kept, duplicates


def map_skill(row: dict[str, Any]) -> dict[str, Any]:
    source_id = str(row.get("source_id", ""))
    if source_id == "deepmind_mathematics_dataset":
        module = str(row.get("upstream_module", ""))
        mapping = dict(MODULE_MAPPING.get(module, {}))
        if not mapping:
            if module.startswith("algebra__linear_1d"):
                mapping = {
                    "primary_skill": "linear_equations",
                    "subskill": "isolate_variables",
                    "role": "question_candidate",
                }
            elif module.startswith("algebra__linear_2d"):
                mapping = {
                    "primary_skill": "systems_equations",
                    "subskill": "solve_systems",
                    "role": "question_candidate",
                }
            elif module.startswith("arithmetic__"):
                mapping = {
                    "primary_skill": None,
                    "prerequisite": "arithmetic_operations",
                    "role": "prerequisite_candidate",
                }
            elif module.startswith("measurement__conversion") or module.startswith("measurement__time"):
                mapping = {
                    "primary_skill": "ratios_percentages",
                    "subskill": "unit_rates",
                    "role": "question_candidate",
                }
            elif module.startswith("polynomials__evaluate") or module.startswith("polynomials__expand"):
                mapping = {
                    "primary_skill": "functions_models",
                    "subskill": "algebraic_models",
                    "role": "question_candidate",
                }
            elif module.startswith("probability__"):
                mapping = {
                    "primary_skill": None,
                    "prerequisite": None,
                    "role": "out_of_scope_candidate",
                }
            else:
                mapping = {
                    "primary_skill": None,
                    "prerequisite": None,
                    "role": "unmapped_candidate",
                }
        mapping["mapping_method"] = (
            "deterministic_upstream_module"
            if module in MODULE_MAPPING
            else "deterministic_upstream_module_family"
        )
        mapping["mapping_confidence"] = 1.0 if mapping["role"] != "unmapped_candidate" else 0.0
        return mapping

    haystack = normalize_text(
        [row.get("title"), row.get("description"), row.get("subjects"), row.get("bookshelves")]
    )
    scores: dict[str, int] = {}
    matched: dict[str, list[str]] = {}
    for skill, keywords in READING_KEYWORDS.items():
        hits = [keyword for keyword in keywords if _contains_term(haystack, keyword)]
        if hits:
            scores[skill] = len(hits)
            matched[skill] = hits
    if scores:
        primary = max(scores, key=lambda key: (scores[key], key))
        confidence = min(0.9, 0.45 + 0.15 * scores[primary])
        return {
            "primary_skill": primary,
            "secondary_skills": sorted(skill for skill in scores if skill != primary),
            "role": "passage_or_multimodal_candidate",
            "mapping_method": "metadata_keyword_rules",
            "mapping_confidence": round(confidence, 2),
            "matched_keywords": matched,
        }

    if source_id == "gsm8k":
        question = normalize_text(row.get("question"))
        if any(_contains_term(question, word) for word in ("percent", "ratio", "per", "rate")):
            skill = "ratios_percentages"
            confidence = 0.65
        elif any(_contains_term(question, word) for word in ("equation", "variable", "unknown")):
            skill = "linear_equations"
            confidence = 0.55
        else:
            skill = None
            confidence = 0.25
        return {
            "primary_skill": skill,
            "prerequisite": None if skill else "arithmetic_operations",
            "role": "evaluation_item",
            "mapping_method": "evaluation_keyword_rules",
            "mapping_confidence": confidence,
        }

    return {
        "primary_skill": None,
        "secondary_skills": [],
        "role": "unmapped_candidate",
        "mapping_method": "metadata_keyword_rules",
        "mapping_confidence": 0.0,
        "matched_keywords": {},
    }


def license_precheck(row: dict[str, Any], source: SourceRecord) -> dict[str, Any]:
    if source.status in {"reference_only", "prohibited"}:
        return {
            "decision": "blocked",
            "scope": "none",
            "reason": "source status does not allow acquisition or reuse",
            "human_review_required": True,
        }
    if source.status == "candidate_generation_only":
        return {
            "decision": "clear_for_candidate_generation",
            "scope": "candidate_generation_only",
            "reason": f"source-level {source.license.get('id', 'unknown')} scope",
            "human_review_required": True,
        }
    if source.status == "evaluation_only":
        return {
            "decision": "clear_for_isolated_evaluation",
            "scope": "evaluation_only",
            "reason": f"source-level {source.license.get('id', 'unknown')} scope",
            "human_review_required": False,
        }
    if source.status == "approved":
        return {
            "decision": "source_approved_item_review_required",
            "scope": "configured_source_actions",
            "reason": "approved source; item review remains mandatory",
            "human_review_required": True,
        }

    rights = normalize_text(row.get("rights_statement"))
    if not rights or "review required" in rights:
        return {
            "decision": "insufficient_item_rights_evidence",
            "scope": "metadata_review_only",
            "reason": "item-level rights statement is missing or only a placeholder",
            "human_review_required": True,
        }
    if any(
        phrase in rights
        for phrase in (
            "public domain and are free to use and reuse",
            "free to use and reuse",
            "in the public domain",
            "are in the public domain",
        )
    ):
        return {
            "decision": "provisionally_clear_requires_human_confirmation",
            "scope": "candidate_material_only",
            "reason": "explicit public-domain/free-to-reuse language found",
            "human_review_required": True,
        }
    if "no known restrictions" in rights or "no known copyright" in rights:
        return {
            "decision": "manual_rights_review_required",
            "scope": "metadata_review_only",
            "reason": "no-known-restrictions language is not an automatic legal clearance",
            "human_review_required": True,
        }
    return {
        "decision": "manual_rights_review_required",
        "scope": "metadata_review_only",
        "reason": "rights statement contains conditions or requires independent assessment",
        "human_review_required": True,
    }


def age_precheck(row: dict[str, Any]) -> dict[str, Any]:
    if row.get("source_id") == "deepmind_mathematics_dataset":
        return {
            "decision": "clear_for_candidate_review",
            "flags": [],
            "reason": "symbolic school-level mathematics candidate",
            "human_review_required": True,
        }

    text = normalize_text(
        [
            row.get("title"),
            row.get("description"),
            row.get("subjects"),
            row.get("bookshelves"),
            row.get("question"),
        ]
    )
    flags: list[dict[str, Any]] = []
    for category, keywords in SENSITIVE_CONTEXT.items():
        hits = sorted(keyword for keyword in keywords if _contains_term(text, keyword))
        if hits:
            flags.append({"category": category, "matched_terms": hits})

    has_substantive_content = bool(row.get("question") or row.get("description"))
    if flags:
        return {
            "decision": "context_review_required",
            "flags": flags,
            "reason": "metadata indicates potentially sensitive historical or contextual material",
            "human_review_required": True,
        }
    if not has_substantive_content:
        return {
            "decision": "insufficient_content_for_age_review",
            "flags": [],
            "reason": "metadata alone cannot establish age suitability",
            "human_review_required": True,
        }
    return {
        "decision": "no_automated_flags",
        "flags": [],
        "reason": "no configured sensitive-context terms found; human review still required",
        "human_review_required": True,
    }


def quality_score(
    row: dict[str, Any],
    *,
    skill: dict[str, Any],
    license_result: dict[str, Any],
    age_result: dict[str, Any],
) -> dict[str, Any]:
    components: dict[str, int] = {}

    identity_fields = (row.get("source_id"), row.get("id"), row.get("upstream_id") or row.get("canonical_url"))
    components["identity_and_traceability"] = round(15 * sum(bool(value) for value in identity_fields) / 3)

    content_fields = (row.get("question"), row.get("answer"), row.get("title"), row.get("description"))
    present_content = sum(bool(value) for value in content_fields)
    components["content_completeness"] = min(25, 5 + 5 * present_content)

    mapping_confidence = float(skill.get("mapping_confidence", 0.0))
    components["educational_relevance"] = round(25 * mapping_confidence)

    license_points = {
        "clear_for_candidate_generation": 18,
        "clear_for_isolated_evaluation": 20,
        "source_approved_item_review_required": 16,
        "provisionally_clear_requires_human_confirmation": 14,
        "manual_rights_review_required": 8,
        "insufficient_item_rights_evidence": 3,
        "blocked": 0,
    }
    components["rights_clarity"] = license_points.get(str(license_result.get("decision")), 0)

    age_points = {
        "clear_for_candidate_review": 10,
        "no_automated_flags": 8,
        "insufficient_content_for_age_review": 4,
        "context_review_required": 4,
    }
    components["age_reviewability"] = age_points.get(str(age_result.get("decision")), 0)

    text = _identity_text(row)
    technical = 5
    if len(text) >= 20:
        technical += 2
    if row.get("normalized_content_hash"):
        technical += 2
    if "untitled" not in normalize_text(row.get("title")):
        technical += 1
    components["technical_quality"] = min(10, technical)

    total = min(100, sum(components.values()))
    if total >= 80:
        band = "high"
    elif total >= 60:
        band = "medium"
    elif total >= 40:
        band = "low"
    else:
        band = "insufficient"
    return {"score": total, "band": band, "components": components}


def route_record(
    row: dict[str, Any],
    *,
    skill: dict[str, Any],
    license_result: dict[str, Any],
    age_result: dict[str, Any],
    quality: dict[str, Any],
) -> str:
    source_id = str(row.get("source_id"))
    if license_result.get("decision") == "blocked":
        return "blocked"
    if source_id == "gsm8k":
        return "evaluation_only"
    if source_id == "deepmind_mathematics_dataset":
        if skill.get("role") == "out_of_scope_candidate":
            return "hold_out_of_scope"
        return "ready_for_rewrite" if quality["score"] >= 60 else "manual_review_required"
    if license_result.get("decision") == "provisionally_clear_requires_human_confirmation":
        return "priority_manual_review" if quality["score"] >= 55 else "manual_review_required"
    if age_result.get("decision") == "context_review_required":
        return "sensitive_context_review"
    return "manual_review_required"


def _load_acquired_rows(acquisition_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    rows.extend(
        _read_jsonl(
            acquisition_root
            / "deepmind_mathematics_dataset"
            / "staging"
            / "candidates.jsonl"
        )
    )
    rows.extend(_read_jsonl(acquisition_root / "project_gutenberg" / "staging" / "candidates.jsonl"))

    # Rebuild LOC rows directly from raw metadata so review never depends on stale
    # staging output created by an older schema adapter.
    loc_raw = acquisition_root / "library_of_congress_free_to_use" / "raw" / "sample-metadata.json"
    if loc_raw.exists():
        loc_rows = _load_loc_rows(loc_raw)
        loc_candidates = _loc_candidates(loc_rows, len(loc_rows))
        _write_jsonl(
            acquisition_root / "library_of_congress_free_to_use" / "staging" / "candidates.jsonl",
            loc_candidates,
        )
        rows.extend(loc_candidates)

    rows.extend(_read_jsonl(acquisition_root / "gsm8k" / "staging" / "evaluation-sample.jsonl"))
    return rows


def _write_review_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "id",
        "source_id",
        "review_route",
        "quality_score",
        "primary_skill",
        "prerequisite",
        "license_decision",
        "age_decision",
        "title_or_question",
        "canonical_url",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            skill = row["skill_mapping"]
            writer.writerow(
                {
                    "id": row["id"],
                    "source_id": row["source_id"],
                    "review_route": row["review_route"],
                    "quality_score": row["quality"]["score"],
                    "primary_skill": skill.get("primary_skill") or "",
                    "prerequisite": skill.get("prerequisite") or "",
                    "license_decision": row["license_precheck"]["decision"],
                    "age_decision": row["age_precheck"]["decision"],
                    "title_or_question": row.get("question") or row.get("title") or "",
                    "canonical_url": row.get("canonical_url") or "",
                }
            )


def _write_markdown_report(path: Path, report: dict[str, Any]) -> None:
    def table(mapping: dict[str, int]) -> str:
        lines = ["| Category | Count |", "|---|---:|"]
        lines.extend(f"| `{key}` | {value} |" for key, value in mapping.items())
        return "\n".join(lines)

    content = f"""# BridgeSAT Candidate Review Report

- Generated: `{report['generated_at']}`
- Input records: **{report['input_count']}**
- Unique records: **{report['unique_count']}**
- Duplicates removed: **{report['duplicate_count']}**
- Student-ready records: **{report['student_content_approved_count']}**
- Run fingerprint: `{report['run_fingerprint']}`

## Review routes

{table(report['route_counts'])}

## Source distribution

{table(report['source_counts'])}

## Skill and prerequisite distribution

{table(report['skill_counts'])}

## License precheck decisions

{table(report['license_decisions'])}

## Age-suitability precheck decisions

{table(report['age_decisions'])}

## Quality summary

- Mean: **{report['quality']['mean']}**
- Minimum: **{report['quality']['minimum']}**
- Maximum: **{report['quality']['maximum']}**

{table(report['quality']['bands'])}

## Limitations

"""
    content += "\n".join(f"- {item}" for item in report["important_limitations"])
    content += "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def process_candidates(
    *,
    registry_path: Path = DEFAULT_REGISTRY,
    acquisition_root: Path = DEFAULT_ACQUISITION,
    output_root: Path = DEFAULT_OUTPUT,
) -> dict[str, Any]:
    registry = SourceRegistry(registry_path)
    registry.validate_restricted_sources()
    raw_rows = _load_acquired_rows(acquisition_root)

    for row in raw_rows:
        row["id"] = _stable_id(row)
    unique_rows, duplicates = deduplicate(raw_rows)

    reviewed: list[dict[str, Any]] = []
    route_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    skill_counts: Counter[str] = Counter()
    license_counts: Counter[str] = Counter()
    age_counts: Counter[str] = Counter()

    for row in unique_rows:
        source = registry.get(str(row.get("source_id")))
        skill = map_skill(row)
        license_result = license_precheck(row, source)
        age_result = age_precheck(row)
        quality = quality_score(
            row,
            skill=skill,
            license_result=license_result,
            age_result=age_result,
        )
        route = route_record(
            row,
            skill=skill,
            license_result=license_result,
            age_result=age_result,
            quality=quality,
        )
        normalized = dict(row)
        normalized.update(
            {
                "review_schema_version": "1.0",
                "reviewed_at": datetime.now(timezone.utc).isoformat(),
                "skill_mapping": skill,
                "license_precheck": license_result,
                "age_precheck": age_result,
                "quality": quality,
                "review_route": route,
                "human_approval_required_for_student_use": route != "evaluation_only",
            }
        )
        reviewed.append(normalized)
        route_counts[route] += 1
        source_counts[str(row.get("source_id"))] += 1
        primary_skill = skill.get("primary_skill") or skill.get("prerequisite") or "unmapped"
        skill_counts[str(primary_skill)] += 1
        license_counts[str(license_result.get("decision"))] += 1
        age_counts[str(age_result.get("decision"))] += 1

    reviewed.sort(key=lambda row: (-int(row["quality"]["score"]), str(row["id"])))
    partitions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in reviewed:
        partitions[str(row["review_route"])].append(row)

    _write_jsonl(output_root / "all-candidates.jsonl", reviewed)
    _write_jsonl(output_root / "review-queue.jsonl", [
        row for row in reviewed if row["review_route"] not in {"evaluation_only", "blocked"}
    ])
    _write_jsonl(output_root / "evaluation-only.jsonl", partitions.get("evaluation_only", []))
    _write_jsonl(output_root / "blocked.jsonl", partitions.get("blocked", []))
    _write_jsonl(output_root / "duplicates.jsonl", [asdict(duplicate) for duplicate in duplicates])
    for route, route_rows in partitions.items():
        _write_jsonl(output_root / "routes" / f"{route}.jsonl", route_rows)

    report = {
        "review_schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "registry": str(registry_path),
        "acquisition_root": str(acquisition_root),
        "output_root": str(output_root),
        "input_count": len(raw_rows),
        "unique_count": len(unique_rows),
        "duplicate_count": len(duplicates),
        "route_counts": dict(sorted(route_counts.items())),
        "source_counts": dict(sorted(source_counts.items())),
        "skill_counts": dict(sorted(skill_counts.items())),
        "license_decisions": dict(sorted(license_counts.items())),
        "age_decisions": dict(sorted(age_counts.items())),
        "quality": {
            "mean": round(sum(row["quality"]["score"] for row in reviewed) / max(1, len(reviewed)), 2),
            "minimum": min((row["quality"]["score"] for row in reviewed), default=0),
            "maximum": max((row["quality"]["score"] for row in reviewed), default=0),
            "bands": dict(sorted(Counter(row["quality"]["band"] for row in reviewed).items())),
        },
        "student_content_approved_count": 0,
        "important_limitations": [
            "Automated license checks are pre-screening, not legal approval.",
            "Gutenberg candidates currently contain catalog metadata, not selected passage text.",
            "LOC candidates contain descriptive metadata and media links; educational and accessibility review remains manual.",
            "DeepMind output must be rewritten and reviewed before student use.",
            "GSM8K remains isolated from product RAG and offline packs.",
        ],
    }
    report["run_fingerprint"] = hashlib.sha256(
        json.dumps(
            {
                "ids": [row["id"] for row in reviewed],
                "routes": report["route_counts"],
                "duplicates": [asdict(duplicate) for duplicate in duplicates],
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    _write_json(output_root / "review-report.json", report)
    _write_review_csv(
        output_root / "review-queue.csv",
        [row for row in reviewed if row["review_route"] not in {"evaluation_only", "blocked"}],
    )
    _write_markdown_report(output_root / "review-report.md", report)
    _write_json(
        output_root / "skill-distribution.json",
        {"skills": dict(sorted(skill_counts.items())), "mvp_skills": sorted(MVP_SKILLS)},
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Review acquired BridgeSAT data candidates")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--acquisition", type=Path, default=DEFAULT_ACQUISITION)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    report = process_candidates(
        registry_path=args.registry,
        acquisition_root=args.acquisition,
        output_root=args.output,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
