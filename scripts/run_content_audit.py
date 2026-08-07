#!/usr/bin/env python3
"""Content audit eval for the published pack (plan section 11, gate: 内容审核).

Label: controlled internal test.

Audits `content/packs/bridgesat-math-0.1.0` against the release contracts:

- manifest: published, reviewers present, versions consistent, licenses,
  item-hash manifest complete, no withdrawn content in the pack;
- items: schema, 4 unique choices, valid answer, unique answers per skill,
  difficulty bounds, non-empty prompt/hints/explanation, approved review,
  reviewer names, license present, canonical content hash match,
  target skill known, no byte-identical bodies;
- lessons: 8 micro_lessons + 8 worked_examples, distinct ids, non-empty
  target_subskill, no byte-identical pairs, canonical hash match;
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

from app.content_pipeline.contracts import SKILLS, content_hash
from app.ingestion.registry import SourceRegistry

PACK_DIR = ROOT / "content" / "packs" / "bridgesat-math-0.1.0"
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
        check(f"item_{item_id}_no_prohibited_lineage", f"{item_id} no prohibited lineage",
              "college_board" not in str(item.get("source_lineage", ""))
              and "khan_academy" not in str(item.get("source_lineage", ""))
              and "openstax" not in str(item.get("source_lineage", "")),
              str(item.get("source_lineage"))[:80])

    answers_per_skill = {}
    for item in items:
        answer_text = next(
            (c["text"] for c in item.get("choices", [])
             if c["id"] == item.get("answer_choice_id")),
            "",
        )
        answers_per_skill.setdefault(item["target_skill"], []).append(answer_text)
    duplicated = {
        skill: [t for t, n in Counter(ids).items() if n > 1]
        for skill, ids in answers_per_skill.items()
    }
    duplicated = {skill: texts for skill, texts in duplicated.items() if texts}
    check("items_unique_answers", "no duplicated answer text across a skill",
          not duplicated, json.dumps(duplicated))

    bodies = [_canonical_body(i.get("prompt", "")) for i in items]
    check("items_no_identical_bodies", "no byte-identical question bodies",
          len(set(bodies)) == len(bodies))

    # --- lessons ----------------------------------------------------------
    kinds = Counter(l.get("content_type") for l in lessons)
    check("lessons_counts", "8 micro_lessons + 8 worked_examples",
          kinds.get("micro_lesson") == 8 and kinds.get("worked_example") == 8,
          dict(kinds))
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
