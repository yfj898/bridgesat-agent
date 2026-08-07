from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ZIP = (
    ROOT
    / "data"
    / "acquisition"
    / "deepmind_mathematics_dataset"
    / "raw"
    / "mathematics_dataset-master.zip"
)
DEFAULT_OUTPUT = (
    ROOT
    / "data"
    / "acquisition"
    / "deepmind_mathematics_dataset"
    / "staging"
    / "candidates.jsonl"
)
DEFAULT_FILTERS = [
    "algebra__linear_1d",
    "algebra__linear_2d",
    "arithmetic__add_or_sub",
    "arithmetic__mul",
    "arithmetic__div",
    "measurement__conversion",
    "measurement__time",
    "probability__swr_p_sequence",
    "polynomials__evaluate",
    "polynomials__expand",
]

HEADER_RE = re.compile(r"\x1b\[1m([^\x1b]+)\x1b\[0m")
ANSWER_RE = re.compile(r"\x1b\[92m([^\x1b]+)\x1b\[0m")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def parse_generator_output(text: str, filter_name: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    current_regime = "unknown"
    current_module = filter_name
    buffer: list[str] = []

    for line in text.splitlines():
        header = HEADER_RE.search(line)
        if header:
            buffer.clear()
            header_value = header.group(1).strip()
            if "/" in header_value:
                current_regime, current_module = header_value.split("/", 1)
            else:
                current_module = header_value
            continue

        buffer.append(line)
        joined = "\n".join(buffer)
        answer_match = ANSWER_RE.search(joined)
        if not answer_match:
            continue

        question_raw = joined[: answer_match.start()]
        question = " ".join(ANSI_RE.sub("", question_raw).split())
        answer = " ".join(answer_match.group(1).split())
        if question and answer:
            item_hash = hashlib.sha256(
                f"{current_module}\n{question}\n{answer}".encode("utf-8")
            ).hexdigest()
            rows.append(
                {
                    "id": f"deepmind-{item_hash[:16]}",
                    "source_id": "deepmind_mathematics_dataset",
                    "upstream_module": current_module,
                    "regime": current_regime,
                    "question": question,
                    "answer": answer,
                    "license_id": "Apache-2.0",
                    "candidate_status": "rewrite_and_educational_review_required",
                    "allowed_use": "candidate_generation_only",
                    "content_hash": item_hash,
                }
            )
        buffer.clear()

    return rows


def generate(
    *,
    archive: Path,
    output: Path,
    dependency_path: Path,
    filters: list[str],
    per_train_module: int,
    per_test_module: int,
) -> dict[str, object]:
    if not archive.exists():
        raise FileNotFoundError(f"DeepMind source archive not found: {archive}")
    if not dependency_path.exists():
        raise FileNotFoundError(
            "temporary dependencies not found; install numpy, sympy, absl-py, and six "
            f"under {dependency_path}"
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    all_rows: list[dict[str, object]] = []
    commands: list[list[str]] = []
    with tempfile.TemporaryDirectory(prefix="bridgesat-math-") as temporary:
        temporary_path = Path(temporary)
        with zipfile.ZipFile(archive) as source_zip:
            source_zip.extractall(temporary_path)
        source_roots = [path for path in temporary_path.iterdir() if path.is_dir()]
        if len(source_roots) != 1:
            raise RuntimeError("unexpected DeepMind archive layout")
        source_root = source_roots[0]

        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(
            [str(dependency_path), str(source_root), environment.get("PYTHONPATH", "")]
        )
        environment["PYTHONHASHSEED"] = "0"

        for filter_name in filters:
            command = [
                sys.executable,
                "-m",
                "mathematics_dataset.generate",
                f"--filter={filter_name}",
                f"--per_train_module={per_train_module}",
                f"--per_test_module={per_test_module}",
            ]
            commands.append(command)
            completed = subprocess.run(
                command,
                cwd=source_root,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
            all_rows.extend(parse_generator_output(completed.stdout, filter_name))

    deduplicated = {str(row["content_hash"]): row for row in all_rows}
    rows = list(deduplicated.values())
    with output.open("w", encoding="utf-8") as output_file:
        for row in rows:
            output_file.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    report = {
        "source_id": "deepmind_mathematics_dataset",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "archive": str(archive),
        "output": str(output),
        "candidate_count": len(rows),
        "filters": filters,
        "per_train_module": per_train_module,
        "per_test_module": per_test_module,
        "commands": commands,
        "review_status": "rewrite_and_educational_review_required",
    }
    output.with_name("generation-report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate governed DeepMind math candidates")
    parser.add_argument("--archive", type=Path, default=DEFAULT_ZIP)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--deps", type=Path, default=Path("/tmp/bridgesat_mathdeps"))
    parser.add_argument("--filters", nargs="+", default=DEFAULT_FILTERS)
    parser.add_argument("--per-train-module", type=int, default=5)
    parser.add_argument("--per-test-module", type=int, default=1)
    args = parser.parse_args()
    report = generate(
        archive=args.archive,
        output=args.output,
        dependency_path=args.deps,
        filters=args.filters,
        per_train_module=max(1, min(args.per_train_module, 100)),
        per_test_module=max(0, min(args.per_test_module, 20)),
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
