#!/usr/bin/env python3
"""Content audit eval for the published pack (plan section 11, gate: 内容审核).

Label: controlled internal test.

Audits the current published content pack against the release contracts:

- manifest: published, reviewers present, versions consistent, licenses,
  question/lesson hash manifests complete, no withdrawn content in the pack;
- items: schema, 4 unique choices, valid answer, unique answers per skill,
  difficulty bounds, non-empty prompt/hints/explanation, approved review,
  reviewer names, license present, canonical content hash match,
  target skill known, no duplicate or near-duplicate bodies;
- lessons: every skill has a micro lesson and worked example, explicit
  misconception targets, distinct ids/bodies, canonical hash match;
- sources: restricted-source registry audit passes (no College Board/Khan/
  OpenStax acquisition), no prohibited source lineage in any item.

Writes reports/content_audit_eval.json and evals/content_audit/REPORT.md.

Usage:
    python scripts/run_content_audit.py [--json reports/content_audit_eval.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.content_pipeline.contracts import MISCONCEPTIONS, SKILLS, content_hash
from app.content_pipeline.packaging import PACK_VERSION
from app.content_pipeline.validation import rewrite_similarity, validate_all
from app.ingestion.registry import SourceRegistry

PACK_DIR = ROOT / "content" / "packs" / f"bridgesat-math-{PACK_VERSION}"
SOURCES_YAML = ROOT / "config" / "sources.yaml"
REPORT_JSON = ROOT / "reports" / "content_audit_eval.json"
REPORT_MD = ROOT / "evals" / "content_audit" / "REPORT.md"


def _load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]


def _canonical_body(text: str) -> str:
    return " ".join(text.split())


def _audit(pack_dir: Path) -> list[dict]:
    findings: list[dict] = []

    def check(check_id: str, description: str, passed: bool, detail: str = "") -> None:
        findings.append(
            {"check": check_id, "description": description, "passed": bool(passed),
             "detail": detail}
        )

    manifest = json.loads((pack_dir / "manifest.json").read_text(encoding="utf-8"))
    items = _load_jsonl(pack_dir / "items.jsonl")
    lessons = _load_jsonl(pack_dir / "lessons.jsonl")

    # --- manifest ---------------------------------------------------------
    check("manifest_published", "pack status is published",
          manifest.get("status") == "published", manifest.get("status"))
    check("manifest_reviewers", "reviewers present in manifest",
          manifest.get("reviewers_present") is True)
    check("manifest_version_dir", "pack_version matches directory name",
          manifest.get("pack_version") == pack_dir.name.split("-")[-1],
          manifest.get("pack_version"))
    check("manifest_schema", "manifest schema_version is v1",
          manifest.get("schema_version") == "v1", manifest.get("schema_version"))
    check("manifest_min_app", "minimum_app_version present",
          bool(manifest.get("minimum_app_version")))
    check("manifest_allowed_schemas", "allowed_item_schema_versions contains v1",
          "v1" in manifest.get("allowed_item_schema_versions", []))
    check("manifest_licenses", "source_licenses documented",
          bool(manifest.get("source_licenses")),
          json.dumps(manifest.get("source_licenses")))
    check("manifest_hashes_complete", "item_hashes cover every item",
          len(manifest.get("item_hashes", {})) == len(items),
          f"{len(manifest.get('item_hashes', {}))}/{len(items)}")
    missing_hashes = [i["id"] for i in items if i["id"] not in manifest.get("item_hashes", {})]
    check("manifest_hashes_no_extra", "no stale hash entries",
          set(manifest.get("item_hashes", {})) == {i["id"] for i in items},
          f"{missing_hashes}")
    check("manifest_lesson_hashes_complete", "lesson_hashes cover every lesson",
          set(manifest.get("lesson_hashes", {})) == {lesson["id"] for lesson in lessons},
          f"{len(manifest.get('lesson_hashes', {}))}/{len(lessons)}")
    check("manifest_content_counts", "manifest content counts match artifacts",
          manifest.get("content_counts") == {"questions": len(items), "lessons": len(lessons)},
          str(manifest.get("content_counts")))
    check("manifest_skills", "manifest skill catalog matches questions",
          set(manifest.get("skills", [])) == {item["target_skill"] for item in items},
          str(manifest.get("skills")))

    # --- items ------------------------------------------------------------
    for item in items:
        item_id = item["id"]
        check(f"item_{item_id}_schema", f"{item_id} schema v1",
              item.get("schema_version") == "v1", item.get("schema_version"))
        check(f"item_{item_id}_type", f"{item_id} is a question",
              item.get("content_type") == "question", item.get("content_type"))
        choices = item.get("choices", [])
        choice_ids = [c["id"] for c in choices]
        check(f"item_{item_id}_four_choices", f"{item_id} has exactly 4 choices",
              len(choices) == 4, str(len(choices)))
        check(f"item_{item_id}_unique_choices", f"{item_id} choice ids unique",
              len(set(choice_ids)) == len(choice_ids))
        check(f"item_{item_id}_answer_valid", f"{item_id} answer is a choice",
              item.get("answer_choice_id") in choice_ids, item.get("answer_choice_id"))
        check(f"item_{item_id}_difficulty", f"{item_id} difficulty in 1..3",
              item.get("difficulty") in (1, 2, 3), str(item.get("difficulty")))
        check(f"item_{item_id}_prompt", f"{item_id} has a prompt",
              bool(str(item.get("prompt", "")).strip()))
        check(f"item_{item_id}_hints", f"{item_id} hints levels 1-3",
              all(any(h.get("level") == level and str(h.get("text", "")).strip()
                      for h in item.get("hints", [])) for level in (1, 2, 3)))
        check(f"item_{item_id}_explanation", f"{item_id} has worked_explanation",
              bool(str(item.get("worked_explanation", "")).strip()))
        check(f"item_{item_id}_approved", f"{item_id} review_status approved",
              item.get("review_status") == "approved", item.get("review_status"))
        check(f"item_{item_id}_reviewers", f"{item_id} has reviewer names",
              bool(item.get("reviewers")),
              str(item.get("reviewers"))[:80])
        check(f"item_{item_id}_license", f"{item_id} has a license",
              bool(item.get("license")), str(item.get("license")))
        check(f"item_{item_id}_skill_known", f"{item_id} target_skill known",
              item.get("target_skill") in SKILLS, str(item.get("target_skill")))
        check(f"item_{item_id}_hash", f"{item_id} canonical content_hash matches",
              item.get("content_hash") == content_hash(item))
        check(f"item_{item_id}_choice_text", f"{item_id} has no generator placeholders",
              all("?" not in str(choice.get("text", "")) for choice in choices))
        check(f"item_{item_id}_no_prohibited_lineage", f"{item_id} no prohibited lineage",
              "college_board" not in str(item.get("source_lineage", ""))
              and "khan_academy" not in str(item.get("source_lineage", ""))
              and "openstax" not in str(item.get("source_lineage", "")),
              str(item.get("source_lineage"))[:80])

    bodies = [_canonical_body(i.get("prompt", "")) for i in items]
    check("items_no_identical_bodies", "no byte-identical question bodies",
          len(set(bodies)) == len(bodies))
    near_duplicates = []
    for index, item in enumerate(items):
        for other in items[index + 1:]:
            similarity = rewrite_similarity(item["prompt"], other["prompt"])
            if similarity >= 0.9:
                near_duplicates.append([item["id"], other["id"], round(similarity, 3)])
    check("items_no_near_duplicate_prompts", "no prompt pair has similarity >= 0.9",
          not near_duplicates, json.dumps(near_duplicates[:10]))
    check("items_unique_ids", "question ids are unique",
          len({item["id"] for item in items}) == len(items))

    formal_errors = validate_all(items, lessons)
    check("formal_validation", "all published content passes formal validation",
          not formal_errors, json.dumps(formal_errors)[:500])

    for skill in sorted({item["target_skill"] for item in items}):
        skill_items = [item for item in items if item["target_skill"] == skill]
        difficulties = {item["difficulty"] for item in skill_items}
        misconceptions = {
            value
            for item in skill_items
            for value in (item.get("misconception_map") or {}).values()
        }
        check(f"coverage_{skill}_questions", f"{skill} has at least 10 questions",
              len(skill_items) >= 10, str(len(skill_items)))
        check(f"coverage_{skill}_difficulty", f"{skill} has at least 2 difficulty levels",
              len(difficulties) >= 2, str(sorted(difficulties)))
        check(f"coverage_{skill}_misconceptions", f"{skill} has at least 2 misconceptions",
              len(misconceptions) >= 2, str(sorted(misconceptions)))

    # --- lessons ----------------------------------------------------------
    kinds = Counter(l.get("content_type") for l in lessons)
    check("lessons_counts", "published pack has 24 adaptive lessons",
          len(lessons) == 24, dict(kinds))
    check("lessons_unique_ids", "lesson ids distinct",
          len({l["id"] for l in lessons}) == len(lessons))
    lesson_bodies = [_canonical_body(l.get("body", "")) for l in lessons]
    check("lessons_no_identical", "no byte-identical lesson bodies",
          len(set(lesson_bodies)) == len(lesson_bodies))
    for lesson in lessons:
        lesson_id = lesson["id"]
        check(f"lesson_{lesson_id}_subskill", f"{lesson_id} has target_subskill",
              bool(str(lesson.get("target_subskill", "")).strip()),
              str(lesson.get("target_subskill")))
        check(f"lesson_{lesson_id}_hash", f"{lesson_id} canonical hash matches",
              lesson.get("content_hash") == content_hash(lesson))
        check(f"lesson_{lesson_id}_approved", f"{lesson_id} approved",
              lesson.get("review_status") == "approved", lesson.get("review_status"))
        targets = lesson.get("target_misconceptions") or []
        check(f"lesson_{lesson_id}_misconceptions",
              f"{lesson_id} targets known misconceptions",
              bool(targets) and set(targets) <= set(MISCONCEPTIONS), str(targets))

    for skill in sorted({item["target_skill"] for item in items}):
        skill_kinds = {
            lesson["content_type"]
            for lesson in lessons
            if lesson["target_skill"] == skill
        }
        check(f"lessons_{skill}_kinds", f"{skill} has both teaching asset kinds",
              {"micro_lesson", "worked_example"} <= skill_kinds, str(sorted(skill_kinds)))

    simulated_review_count = sum(
        1
        for entry in [*items, *lessons]
        if any(str(reviewer).startswith("sim.") for reviewer in (entry.get("reviewers") or {}).values())
    )
    provenance = manifest.get("review_provenance") or {}
    expected_mode = "simulated_competition_review" if simulated_review_count else "human_review"
    check("review_provenance_labeled", "review provenance matches reviewer identities",
          provenance.get("mode") == expected_mode
          and provenance.get("human_approved") is (simulated_review_count == 0),
          f"mode={provenance.get('mode')}; simulated_records={simulated_review_count}; "
          f"human_approved={provenance.get('human_approved')}")
    answer_labels = Counter(item.get("answer_choice_id") for item in items)
    check("answer_label_distribution", "correct answers are not exposed by one fixed label",
          set(answer_labels) == {"A", "B", "C", "D"}
          and max(answer_labels.values()) / len(items) <= 0.40,
          str(dict(sorted(answer_labels.items()))))

    # --- sources ----------------------------------------------------------
    registry = SourceRegistry(SOURCES_YAML)
    try:
        registry.validate_restricted_sources()
        restricted_ok = True
        restricted_detail = "no restricted source acquisition or crawler enabled"
    except Exception as exc:
        restricted_ok = False
        restricted_detail = str(exc)
    check("sources_restricted_audit", "restricted-source audit passes",
          restricted_ok, restricted_detail)
    check("sources_no_prohibited_lineage",
          "no item carries prohibited-source lineage",
          all("college_board" not in str(i.get("source_lineage", ""))
              and "khan_academy" not in str(i.get("source_lineage", ""))
              and "openstax" not in str(i.get("source_lineage", ""))
              for i in items))

    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", type=Path, default=REPORT_JSON)
    parser.add_argument("--pack-dir", type=Path, default=PACK_DIR)
    args = parser.parse_args()

    pack_dir = args.pack_dir
    findings = _audit(pack_dir)
    passed = sum(1 for f in findings if f["passed"])
    summary = {
        "schema_version": "1.0",
        "label": "controlled internal test",
        "pack": str(pack_dir),
        "checks": len(findings),
        "passed": passed,
        "pass_rate": round(passed / len(findings), 4) if findings else 0.0,
        "targets": {"published_content_gate": "100%", "restricted_source_recall": "0"},
        "findings": findings,
    }

    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    REPORT_MD.parent.mkdir(parents=True, exist_ok=True)
    REPORT_MD.write_text(
        f"""# Content audit eval report

- label: {summary['label']}
- pack: {summary['pack']}
- checks: {summary['checks']}, pass rate: {summary['pass_rate']:.0%}

| Check | Passed | Detail |
|---|---|---|
""" + "\n".join(
            f"| {f['check']} | {'PASS' if f['passed'] else 'FAIL'} | {f['detail']} |"
            for f in findings
        ) + "\n",
        encoding="utf-8",
    )

    print(json.dumps({
        "pack": summary["pack"],
        "checks": summary["checks"],
        "pass_rate": summary["pass_rate"],
    }, indent=2))
    for f in findings:
        if not f["passed"]:
            print(f"  FAIL {f['check']}: {f['detail']}")
    return 0 if summary["pass_rate"] == 1.0 else 2


if __name__ == "__main__":
    sys.exit(main())
