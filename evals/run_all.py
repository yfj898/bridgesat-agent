#!/usr/bin/env python3
"""Run every BridgeSAT evaluation and publish the competition evidence pack.

Executes:

- policy golden eval            -> reports/policy_eval.json
- educational behavior eval     -> reports/educational_eval.json
- offline and sync eval         -> reports/offline_sync_eval.json
- retrieval (RAG) eval          -> reports/rag_eval.json
- memory ablation eval          -> reports/memory_eval.json
- security test suite (pytest)  -> reports/security_eval.json
- web core-flow tests (node)   -> reports/web_tests.json
- content audit                -> reports/content_audit_eval.json
- performance gates            -> reports/performance_eval.json
- accessibility checklist      -> reports/accessibility_eval.md
- full Python test suite       -> reports/python_tests.json
- Hybrid final decision gate   -> reports/hybrid_final_gate.json
- final summary                 -> reports/final_summary.md
- evidence pack                 -> docs/EVIDENCE_PACK.md

Usage:
    python -m evals.run_all
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"

SECURITY_SUITES = [
    "tests/security",
    "tests/test_sync_protocol.py",
]


def _run(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, cwd=ROOT)


def _refresh_content_index() -> None:
    """Rebuild the approved PostgreSQL content registry/search index.

    Several evaluation/test paths intentionally reset shared PostgreSQL state.
    Retrieval and performance evidence must therefore establish their own
    deterministic content precondition instead of depending on command order.
    """
    result = _run([sys.executable, "scripts/import_content_pack.py"])
    if result.returncode != 0:
        tail = (result.stdout + result.stderr)[-1000:]
        raise RuntimeError(f"content index refresh failed: {tail}")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _first_json_block(stdout: str) -> dict:
    decoder = json.JSONDecoder()
    start = stdout.index("{")
    return decoder.raw_decode(stdout, start)[0]


def run_policy() -> Path:
    result = _run([sys.executable, "scripts/run_policy_evals.py"])
    if result.returncode not in (0, 2):
        raise RuntimeError(f"policy eval failed: {result.stderr[-500:]}")
    return REPORTS / "policy_eval.json"


def run_educational() -> Path:
    result = _run([sys.executable, "scripts/run_educational_evals.py"])
    if result.returncode != 0:
        raise RuntimeError(f"educational eval failed: {result.stderr[-500:]}")
    return REPORTS / "educational_eval.json"


def run_offline_sync() -> Path:
    result = _run([sys.executable, "scripts/run_offline_sync_evals.py"])
    if result.returncode not in (0, 2):
        raise RuntimeError(f"offline sync eval failed: {result.stderr[-500:]}")
    path = REPORTS / "offline_sync_eval.json"
    report = _read_json(path)
    if result.returncode != 0 or report.get("pass_rate") != 1.0:
        raise RuntimeError(f"offline sync gate failed: {report}")
    return path


def run_retrieval() -> Path:
    _refresh_content_index()
    result = _run([sys.executable, "scripts/run_retrieval_evals.py"])
    if result.returncode != 0:
        raise RuntimeError(f"retrieval eval failed: {result.stderr[-500:]}")
    dev, golden = _first_json_block(result.stdout), _first_json_block(
        result.stdout.split("GOLDEN:", 1)[1]
    )
    payload = {
        "schema_version": "1.0",
        "label": "controlled internal test",
        "dev": dev,
        "golden": golden,
        "dev_set": "evals/retrieval/dev.jsonl",
        "golden_set": "evals/retrieval/golden.jsonl",
    }
    path = REPORTS / "rag_eval.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def run_memory() -> Path:
    result = _run([sys.executable, "scripts/run_memory_ablation.py"])
    if result.returncode != 0:
        raise RuntimeError(f"memory eval failed: {result.stderr[-500:]}")
    payload = _first_json_block(result.stdout)
    payload["schema_version"] = "1.0"
    payload["label"] = "controlled internal test"
    payload["golden_set"] = "evals/memory/golden.jsonl"
    path = REPORTS / "memory_eval.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def run_hybrid() -> Path:
    result = _run([sys.executable, "scripts/run_hybrid_ablation.py"])
    if result.returncode != 0:
        raise RuntimeError(f"hybrid eval failed: {result.stderr[-500:]}")
    payload = _first_json_block(result.stdout)
    payload["schema_version"] = "1.0"
    payload["label"] = "controlled internal test"
    payload["golden_set"] = "evals/hybrid/golden.jsonl"
    path = REPORTS / "hybrid_eval.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def run_hybrid_final_gate() -> Path:
    """Freeze the competition mode after the Hybrid report is generated."""
    result = _run(
        [
            sys.executable,
            "scripts/run_hybrid_final_gate.py",
            "--input",
            "reports/hybrid_eval.json",
            "--output",
            "reports/hybrid_final_gate.json",
        ]
    )
    if result.returncode != 0:
        raise RuntimeError(f"hybrid final gate failed: {result.stdout[-500:]}{result.stderr[-500:]}")
    return REPORTS / "hybrid_final_gate.json"


def run_security() -> Path:
    result = _run([sys.executable, "-m", "pytest", *SECURITY_SUITES,
                   "--disable-warnings", "-p", "no:cacheprovider"])
    tail = (result.stdout + result.stderr)[-4000:]
    match = re.search(r"(\d+) passed", tail)
    summary = {
        "schema_version": "1.0",
        "label": "controlled internal test",
        "command": f"pytest {' '.join(SECURITY_SUITES)} -q",
        "passed": int(match.group(1)) if match else 0,
        "tail": tail[-1500:],
    }
    path = REPORTS / "security_eval.json"
    path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    if result.returncode != 0 or summary["passed"] == 0:
        raise RuntimeError(f"security test suite failed: {tail[-1000:]}")
    return path


def run_content_audit() -> Path:
    result = _run([sys.executable, "scripts/run_content_audit.py"])
    if result.returncode not in (0, 2):
        raise RuntimeError(f"content audit failed: {result.stderr[-500:]}")
    path = REPORTS / "content_audit_eval.json"
    report = _read_json(path)
    if result.returncode != 0 or report.get("pass_rate") != 1.0:
        raise RuntimeError(f"content audit gate failed: {report}")
    return path


def run_performance() -> Path:
    _refresh_content_index()
    result = _run([sys.executable, "scripts/run_performance_evals.py"])
    if result.returncode not in (0, 2):
        raise RuntimeError(f"performance eval failed: {result.stderr[-500:]}")
    path = REPORTS / "performance_eval.json"
    report = _read_json(path)
    if result.returncode != 0 or not report.get("all_gates_passed"):
        raise RuntimeError(f"performance gate failed: {report}")
    return path


def run_web_tests() -> Path:
    import glob as _glob

    test_files = sorted(_glob.glob("web/tests/*.test.js"))
    if not test_files:
        raise RuntimeError("no web test files found under web/tests/")
    result = _run(["node", "--test", *test_files])
    tail = (result.stdout + result.stderr)[-4000:]
    pass_match = re.search(r"# pass (\d+)", tail)
    fail_match = re.search(r"# fail (\d+)", tail)
    summary = {
        "schema_version": "1.0",
        "label": "controlled internal test",
        "command": f"node --test {', '.join(test_files)}",
        "passed": int(pass_match.group(1)) if pass_match else 0,
        "failed": int(fail_match.group(1)) if fail_match else -1,
        "tail": tail[-1500:],
    }
    path = REPORTS / "web_tests.json"
    path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    if result.returncode != 0 or summary["failed"] != 0:
        raise RuntimeError(f"web test suite failed: {tail[-1000:]}")
    return path


def run_python_tests() -> Path:
    result = _run([sys.executable, "-m", "pytest", "-p", "no:warnings"])
    tail = (result.stdout + result.stderr)[-4000:]
    pass_match = re.search(r"(\d+) passed", tail)
    fail_match = re.search(r"(\d+) failed", tail)
    if result.returncode != 0 or pass_match is None:
        raise RuntimeError(f"Python test suite failed: {tail[-1000:]}")
    summary = {
        "schema_version": "1.0",
        "label": "controlled internal test",
        "command": "python -m pytest -p no:warnings",
        "passed": int(pass_match.group(1)),
        "failed": int(fail_match.group(1)) if fail_match else 0,
        "tail": tail[-1500:],
    }
    path = REPORTS / "python_tests.json"
    path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return path


ACCESSIBILITY_CHECKLIST = [
    ("WCAG 2.1 AA color contrast target", "Design target; manual check required before demo."),
    ("All core flows keyboard-operable", "Implemented; manual keyboard walkthrough required."),
    ("Visible focus indicator", "styles.css :focus-visible rules (web/styles.css:81-84)."),
    ("Touch targets >= 44x44 CSS px", "Buttons min-height 48px (web/styles.css:65)."),
    ("Form controls have accessible names", "aria-labelledby on cards (web/index.html:21,36)."),
    ("Progress and error states announced to AT", "role=status on network/sync/state (web/index.html:17-18,39)."),
    ("No required information conveyed by color alone", "Statuses carry text labels (web/index.html)."),
    ("200% text zoom does not block core flow", "rem/clamp sizing (web/styles.css); manual check required."),
    ("Reduced-motion preference respected", "No motion-critical animation in the PWA; confirm at demo."),
    ("Mathematical content has accessible text representation", "Math rendered as text expressions; confirm with screen reader."),
    ("Offline and sync state has text labels", "network-status and sync-status role=status elements."),
]


def write_accessibility() -> Path:
    lines = [
        "# Accessibility evaluation",
        "",
        "Covers EVALUATION_SPEC section 9. Items marked 'manual check required'",
        "need a human usability pass before the competition demo.",
        "",
        "| Criterion | Status | Evidence |",
        "|---|---|---|",
    ]
    for criterion, note in ACCESSIBILITY_CHECKLIST:
        status = "verified-by-inspection" if "manual" not in note.lower() else "manual check required"
        lines.append(f"| {criterion} | {status} | {note} |")
    path = REPORTS / "accessibility_eval.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _measured(report: dict, key: str, default: str = "n/a") -> str:
    value = report.get(key)
    if isinstance(value, float):
        return f"{value:.0%}" if 0 <= value <= 1 else f"{value:.3f}"
    return str(value if value is not None else default)


def write_summary(entries: dict[str, Path]) -> Path:
    policy = _read_json(entries["policy"])
    educational = _read_json(entries["educational"])
    offline = _read_json(entries["offline_sync"])
    rag = _read_json(entries["rag"])
    memory = _read_json(entries["memory"])
    hybrid = _read_json(entries["hybrid"])
    hybrid_gate = _read_json(entries["hybrid_final_gate"])
    python_tests = _read_json(entries["python_tests"])
    security = _read_json(entries["security"])
    content_audit = _read_json(entries["content_audit"])
    performance = _read_json(entries["performance"])
    web = _read_json(entries["web"])

    scenario_by_target = {
        s["target"]: ("PASS" if s["passed"] else "FAIL") for s in offline["results"]
    }
    offline_targets = [
        "offline core-flow completion = 100%",
        "duplicate scoring incidents = 0",
        "restart recovery = 100%",
        "known-version scoring consistency = 100%",
        "unacknowledged-event loss = 0",
    ]

    lines = [
        "# BridgeSAT final evaluation summary",
        "",
        "Every result below is labeled by EVALUATION_SPEC section 2:",
        "`synthetic simulation`, `controlled internal test`, or design target.",
        "Synthetic simulation is never presented as real student improvement.",
        "",
        "## Policy (synthetic simulation)",
        "",
        f"- trajectories: {policy['total_trajectories']} (target >= 20)",
        f"- overall pass rate: {policy['pass_rate']:.0%} (design target >= 90%)",
        f"- safety-critical pass rate: {policy['safety_critical_pass_rate']:.0%} (design target 100%)",
        f"- categories covered: {len(policy['categories_covered'])}/12",
        f"- report: reports/policy_eval.json",
        "",
        "## Educational behavior (synthetic simulation)",
        "",
        "- disclaimer: synthetic simulation, not real student improvement",
        f"- correctness delta: {educational['metrics']['correctness_delta']:+.3f}",
        f"- hint usage delta: {educational['metrics']['hint_delta']:+d}",
        f"- immediate transfer delta: {educational['metrics']['transfer_correct_delta']:+.3f}",
        f"- delayed retention delta: {educational['metrics']['retention_correct_delta']:+.3f}",
        f"- mastery change delta: {educational['metrics']['mastery_change_delta']:+.3f}",
        f"- interventions deployed: {educational['metrics']['interventions_deployed']}",
        f"- report: reports/educational_eval.json",
        "",
        "## Retrieval (controlled internal test)",
        "",
        f"- dev set: {rag['dev']['queries']} queries, recall@1 "
        f"{_measured(rag['dev'], 'recall_at_1')}, MRR {_measured(rag['dev'], 'mrr')}, "
        f"citation coverage {_measured(rag['dev'], 'citation_coverage')}, "
        f"license coverage {_measured(rag['dev'], 'license_coverage')}, "
        f"restricted-source hits {rag['dev']['restricted_source_hits']}",
        f"- golden set: {rag['golden']['queries']} queries, recall@1 "
        f"{_measured(rag['golden'], 'recall_at_1')}, MRR {_measured(rag['golden'], 'mrr')}, "
        f"citation coverage {_measured(rag['golden'], 'citation_coverage')}, "
        f"license coverage {_measured(rag['golden'], 'license_coverage')}",
        f"- report: reports/rag_eval.json",
        "",
        "## Long-term memory (controlled internal test)",
        "",
        f"- probes: {memory['summary']['probes']}",
        f"- PostgreSQL similarity recall@3: {_measured(memory['summary']['similar_postgres'], 'recall_at_3')}",
        f"- PostgreSQL similarity next-action accuracy: {_measured(memory['summary']['similar_postgres'], 'next_action_accuracy')}",
        f"- Mnemis dual-route recall@3: {_measured(memory['summary']['mnemis_dual'], 'recall_at_3')}",
        f"- Mnemis dual-route next-action accuracy: {_measured(memory['summary']['mnemis_dual'], 'next_action_accuracy')}",
        "- fallback success: PostgreSQL two-session loop and timeout fallback tested in the test suite",
        f"- report: reports/memory_eval.json",
        "",
        "## Hybrid shadow ablation (controlled internal test)",
        "",
        f"- cases: {hybrid['summary']['cases']} golden, "
        f"{hybrid['summary']['variants']} scripted model variants (no live provider)",
        f"- accepted allowed-action violations: {hybrid['summary']['accepted_allowed_action_violations']} "
        f"(target 0)",
        f"- accepted hallucinated episode/content: {hybrid['summary']['accepted_hallucinated_proposals']} "
        f"(target 0)",
        f"- deterministic fallback success: {hybrid['summary']['fallback_success_rate']:.0%} "
        f"(target 100%)",
        f"- decisive cases with zero model calls: {hybrid['summary']['decisive_zero_model_calls']:.0%}",
        f"- adversarial rejection: {hybrid['summary']['adversarial_rejection_rate']:.0%}",
        f"- verified beneficial differences: {hybrid['summary']['beneficial_difference_cases']}",
        f"- report: reports/hybrid_eval.json",
        "",
        "## Hybrid final gate (controlled internal test)",
        "",
        f"- final competition mode: `{hybrid_gate['final_mode']}`",
        f"- frozen feature flags: "
        f"{', '.join(f'{name}=0' for name in hybrid_gate['frozen_feature_flags'])}",
        f"- H7 action-ranking final decision: **{hybrid_gate['action_ranking_decision']}** "
        "(No-Go is a legal evidence outcome, not a gate failure)",
        f"- gate status: {hybrid_gate['status']} (report shape and safety checks)",
        f"- evidence split: legacy H6/H7 "
        f"{hybrid_gate['evidence_scope']['legacy_metrics']['cases']} cases/"
        f"{hybrid_gate['evidence_scope']['legacy_metrics']['variants']} variants; "
        f"H8 summary "
        f"{hybrid_gate['evidence_scope']['h8_summary']['cases']} cases/"
        f"{hybrid_gate['evidence_scope']['h8_summary']['variants']} variants "
        "(excluded from legacy metrics)",
        f"- safety checks: allowed-action violations "
        f"{hybrid_gate['safety_checks']['allowed_action_violations']}, "
        f"hallucinated acceptance {hybrid_gate['safety_checks']['hallucinated_acceptance']}, "
        f"fallback {hybrid_gate['safety_checks']['fallback_success_rate']:.0%}, "
        f"decisive zero calls {hybrid_gate['safety_checks']['decisive_zero_model_calls']:.0%}, "
        f"explanation grounding {hybrid_gate['safety_checks']['explanation_grounding_accuracy']:.0%}, "
        f"H8 summary grounding {hybrid_gate['safety_checks']['summary_grounding_accuracy']:.0%}, "
        f"unavailable H8 fallback {hybrid_gate['safety_checks']['summary_unavailable_fallback_rate']:.0%}",
        f"- beneficial difference cases: "
        f"{hybrid_gate['safety_checks']['beneficial_difference_cases'] or 'none'} "
        "(controlled synthetic/scripted evidence only)",
        f"- rationale: {hybrid_gate['rationale']}",
        f"- latency disclaimer: {hybrid_gate['synthetic_latency_disclaimer']}",
        f"- missing evidence: {', '.join(hybrid_gate['missing_evidence'])}",
        f"- rollback profile: {hybrid_gate['rollback_profile']['name']}; "
        f"{hybrid_gate['rollback_profile']['action']}",
        f"- report: reports/hybrid_final_gate.json",
        "",
        "## Python test suite (controlled internal test)",
        "",
        f"- pytest: {python_tests['passed']} passed, {python_tests['failed']} failed",
        f"- report: reports/python_tests.json",
        "",
        "## Offline and synchronization (controlled internal test)",
        "",
        f"- scenarios: {offline['scenario_count']}, pass rate: {offline['pass_rate']:.0%}",
        f"- report: reports/offline_sync_eval.json",
        "",
        "## Security (controlled internal test)",
        "",
        f"- pytest security + sync suites: {security['passed']} passed",
        f"- report: reports/security_eval.json",
        "",
        "## Web core-flow tests (controlled internal test)",
        "",
        f"- node --test web/tests: {web['passed']} passed, {web['failed']} failed "
        f"(offline flow, refresh, weak network, accessibility core paths)",
        f"- report: reports/web_tests.json",
        "",
        "## Content audit (controlled internal test)",
        "",
        f"- checks: {content_audit['checks']}, pass rate: {content_audit['pass_rate']:.0%}",
        f"- pack: {content_audit['pack']}",
        "- limitation: reviewer IDs are simulated (`sim.*`); this is not a real human approval",
        f"- report: reports/content_audit_eval.json",
        "",
        "## Performance gates (controlled internal test, this machine)",
        "",
        f"- local policy p95: {performance['results']['local_policy']['p95_ms']} ms "
        f"(target < 150 ms)",
        f"- PostgreSQL tsvector p95: {performance['results']['tsvector']['p95_ms']} ms "
        f"(target < 200 ms)",
        f"- session restore p95: {performance['results']['session_restore']['p95_ms']} ms "
        f"(target < 500 ms)",
        f"- sync throughput: {performance['sync_throughput_events_per_sec']} events/s, "
        f"max RSS {performance['max_rss_mb']} MB",
        f"- report: reports/performance_eval.json",
        "",
        "## Accessibility",
        "",
        "- checklist: reports/accessibility_eval.md",
        "- items needing a human usability pass are marked 'manual check required'",
        "",
        "## Design targets vs measured results",
        "",
        "| Target | Kind | Measured |",
        "|---|---|---|",
        f"| policy overall >= 90% | design target | {policy['pass_rate']:.0%} |",
        f"| policy safety-critical 100% | design target | {policy['safety_critical_pass_rate']:.0%} |",
        *[
            f"| {target} | design target | {scenario_by_target.get(target, 'n/a')} |"
            for target in offline_targets
        ],
        f"| content audit 100% | controlled internal test | {content_audit['pass_rate']:.0%} |",
        f"| local policy p95 < 150 ms | controlled internal test | "
        f"{performance['results']['local_policy']['p95_ms']} ms |",
        f"| PostgreSQL tsvector p95 < 200 ms | controlled internal test | "
        f"{performance['results']['tsvector']['p95_ms']} ms |",
        f"| session restore p95 < 500 ms | controlled internal test | "
        f"{performance['results']['session_restore']['p95_ms']} ms |",
        f"| educational improvement over control | synthetic simulation | "
        f"+{educational['metrics']['correctness_delta']:.1%} correctness |",
        "",
        "## Reproduction",
        "",
        "```bash",
        ".venv/bin/python scripts/import_content_pack.py",
        ".venv/bin/python -m pytest",
        "node --test web/tests/*.test.js",
        ".venv/bin/python -m evals.run_all",
        ".venv/bin/python scripts/seed_demo.py",
        "```",
        "",
        "## Recovery capabilities (API_AND_OPERATIONS sections 7-8)",
        "",
        "| Capability | Implementation | Evidence |",
        "|---|---|---|",
        "| PostgreSQL schema migration | `app/infrastructure/migration_runner.py` applies versioned `migrations_pg` transactions under an advisory lock | `tests/test_pg_migration_runner.py` |",
        "| Rebuild learner projections from events | `scripts/rebuild_learner_projections.py` replays `learning_events` through the sync apply path | `tests/test_sync_protocol.py::test_rebuild_projection_from_events_restores_state` (golden equality) |",
        "| Rebuild PostgreSQL tsvector index from approved content | `app/knowledge/local_backend.index_pack` via `scripts/import_content_pack.py` | `tests/test_pg_retrieval.py`, `tests/test_retrieval.py` |",
        "| Rebuild Mnemis from validated episodes/facts | `scripts/rebuild_memory_index.py` (idempotency keys, dead-letter replay) | `tests/test_scripts.py` |",
        "| Verify content-pack checksums | `verify_pack_hashes` in `app/content_pipeline/packaging.py` | `scripts/build_content_pack.py` + `scripts/run_content_audit.py` |",
        "",
    ]
    path = REPORTS / "final_summary.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_evidence_pack(entries: dict[str, Path], summary_path: Path) -> Path:
    lines = [
        "# BridgeSAT competition evidence pack",
        "",
        "What the repo can prove, and how to reproduce it.",
        "",
        "## Measured results (reproducible)",
        "",
        "| Evaluation | Label | Report | Command |",
        "|---|---|---|---|",
        "| Policy golden | synthetic simulation | reports/policy_eval.json | `.venv/bin/python scripts/run_policy_evals.py` |",
        "| Educational behavior | synthetic simulation | reports/educational_eval.json | `.venv/bin/python scripts/run_educational_evals.py` |",
        "| Retrieval (RAG) | controlled internal test | reports/rag_eval.json | `.venv/bin/python scripts/import_content_pack.py && .venv/bin/python scripts/run_retrieval_evals.py` |",
        "| Long-term memory | controlled internal test | reports/memory_eval.json | `.venv/bin/python scripts/run_memory_ablation.py` |",
        "| Hybrid shadow ablation | controlled internal test | reports/hybrid_eval.json | `.venv/bin/python scripts/run_hybrid_ablation.py` |",
        "| Hybrid final gate | controlled internal test | reports/hybrid_final_gate.json | `.venv/bin/python scripts/run_hybrid_final_gate.py --input reports/hybrid_eval.json --output reports/hybrid_final_gate.json` |",
        "| Full Python test suite | controlled internal test | reports/python_tests.json | `.venv/bin/python -m pytest -p no:warnings` |",
        "| Offline and sync | controlled internal test | reports/offline_sync_eval.json | `.venv/bin/python scripts/run_offline_sync_evals.py` |",
        "| Security | controlled internal test | reports/security_eval.json | `.venv/bin/python -m pytest tests/security tests/test_sync_protocol.py -q` |",
        "| Web core-flow tests | controlled internal test | reports/web_tests.json | `node --test web/tests/*.test.js` |",
        "| Content audit | controlled internal test | reports/content_audit_eval.json | `.venv/bin/python scripts/run_content_audit.py` |",
        "| Performance gates | controlled internal test | reports/performance_eval.json | `.venv/bin/python scripts/import_content_pack.py && .venv/bin/python scripts/run_performance_evals.py` |",
        "| Accessibility | checklist | reports/accessibility_eval.md | `.venv/bin/python -m evals.run_all` |",
        "| Final summary | aggregation | reports/final_summary.md | `.venv/bin/python -m evals.run_all` |",
        "",
        "## Design targets not yet measured",
        "",
        "- real educational outcome (requires a human usability study, EVALUATION_SPEC section 2);",
        "- accessibility manual walkthrough items marked 'manual check required'.",
        "- real human content review (the current `sim.*` ledger is controlled test data).",
        "- real-provider latency and lock-duration evidence for Hybrid action ranking; the final gate records a No-Go and keeps deterministic mode frozen.",
        "",
        "## Honesty rules (EVALUATION_SPEC section 2)",
        "",
        "1. Every result is labeled `synthetic simulation`, `controlled internal test`, "
        "`human usability test`, or `real educational outcome`.",
        "2. Synthetic simulation is never presented as real student improvement.",
        "3. The final summary distinguishes measured results from design targets.",
        "",
        "## Hybrid final decision",
        "",
        "The Hybrid final gate is a controlled internal test over synthetic cases "
        "with scripted provider responses. Set `BRIDGESAT_HYBRID_COMPETITION_MODE=1` "
        "for the competition/demo deployment; startup rejects contradictory flags, "
        "freezes competition mode to deterministic, and keeps all five Hybrid feature "
        "flags at `0`. H7 action "
        "ranking is **No-Go** for default enablement: scripted p50/p95 values of "
        "0 ms are not real-provider latency evidence, and no repeated real-provider "
        "latency/lock-duration run is present. The H8 five-case/five-variant "
        "summary evidence remains separate from legacy H6/H7 metrics. This does "
        "not claim real student outcomes or real-provider latency.",
        "",
        "- final mode: `deterministic`",
        "- action-ranking decision: **No-Go**",
        "- report: `reports/hybrid_final_gate.json`",
        "",
        "## Reproduction",
        "",
        "```bash",
        ".venv/bin/python scripts/import_content_pack.py",
        ".venv/bin/python -m pytest",
        "node --test web/tests/*.test.js",
        ".venv/bin/python -m evals.run_all",
        ".venv/bin/python scripts/seed_demo.py",
        "```",
        "",
        "## Recovery capabilities (API_AND_OPERATIONS sections 7-8)",
        "",
        "| Capability | Implementation | Evidence |",
        "|---|---|---|",
        "| PostgreSQL schema migration | `app/infrastructure/migration_runner.py` + `app/infrastructure/migrations_pg/` | `tests/test_pg_migration_runner.py` |",
        "| Rebuild learner projections from events | `scripts/rebuild_learner_projections.py` | `tests/test_sync_protocol.py::test_rebuild_projection_from_events_restores_state` |",
        "| Rebuild PostgreSQL tsvector index | `app/knowledge/local_backend.index_pack` via `scripts/import_content_pack.py` | `tests/test_pg_retrieval.py`, `tests/test_retrieval.py` |",
        "| Rebuild Mnemis | `scripts/rebuild_memory_index.py` | `tests/test_scripts.py` |",
        "| Verify content-pack checksums | `verify_pack_hashes` | `scripts/run_content_audit.py` |",
        "",
        "## Status",
        "",
        "Generated by `.venv/bin/python -m evals.run_all`. Regenerate after any change.",
        "",
    ]
    path = ROOT / "docs" / "EVIDENCE_PACK.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    failures: list[str] = []
    entries: dict[str, Path] = {}

    steps = [
        ("policy", run_policy),
        ("educational", run_educational),
        ("offline_sync", run_offline_sync),
        ("rag", run_retrieval),
        ("memory", run_memory),
        ("hybrid", run_hybrid),
        ("hybrid_final_gate", run_hybrid_final_gate),
        ("python_tests", run_python_tests),
        ("security", run_security),
        ("content_audit", run_content_audit),
        ("performance", run_performance),
        ("web", run_web_tests),
    ]
    for name, step in steps:
        try:
            entries[name] = step()
            print(f"[ok] {name}")
        except Exception as exc:
            failures.append(name)
            print(f"[FAIL] {name}: {exc}")

    entries["accessibility"] = write_accessibility()
    print("[ok] accessibility")

    if failures:
        print(f"\nAborting summary: {failures}")
        return 1

    summary_path = write_summary(entries)
    write_evidence_pack(entries, summary_path)
    print(f"[ok] reports/final_summary.md")
    print(f"[ok] docs/EVIDENCE_PACK.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
