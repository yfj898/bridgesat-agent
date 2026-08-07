# BridgeSAT Data Acquisition

## Purpose

This pipeline downloads only sources explicitly enabled in `config/sources.yaml`.
It uses official machine-readable catalogs, public APIs, repository archives, and
dataset files rather than unrestricted website scraping.

Restricted sources such as College Board, Khan Academy, and OpenStax are never
passed to the acquisition layer.

## Initial acquisition

```bash
python scripts/acquire_sources.py --limit 100
```

The default run acquires:

- DeepMind Mathematics Dataset source and license for controlled candidate generation;
- Project Gutenberg's official compressed catalog and policy snapshots;
- Library of Congress Free to Use and Reuse sample metadata and documentation;
- GSM8K's test split in an isolated evaluation-only directory.

Generate actual DeepMind candidate question-answer pairs after installing the
four upstream runtime dependencies into a temporary directory:

```bash
python -m pip install --target /tmp/bridgesat_mathdeps \
  'numpy<2.0' 'sympy>=1.12,<2' 'absl-py>=2,<3' 'six>=1.16,<2'
python scripts/generate_math_candidates.py
```

Generated records remain candidate-only and cannot be published directly.

## Output layout

```text
data/acquisition/
├── artifacts.jsonl
├── run-report.json
├── deepmind_mathematics_dataset/
│   ├── raw/
│   └── staging/
├── project_gutenberg/
│   ├── raw/
│   └── staging/candidates.jsonl
├── library_of_congress_free_to_use/
│   ├── raw/
│   └── staging/candidates.jsonl
└── gsm8k/
    ├── raw/
    └── staging/evaluation-sample.jsonl
```

Files under `raw/` are ignored by Git by default. Manifests, hashes, and staging
records remain available for review and reproducibility.

## Review boundary

Downloaded content is not automatically approved for students.

- Gutenberg and Library of Congress candidates require item-level rights,
  educational, age-suitability, and accessibility review.
- DeepMind output is candidate-generation material and must be rewritten and
  reviewed before product use.
- GSM8K remains isolated evaluation data and must not enter product RAG or the
  offline content pack.

## Candidate normalization and pre-review

Run after acquisition and mathematics candidate generation:

```bash
python scripts/review_candidates.py
```

This stage:

- rebuilds Library of Congress staging records from the official mixed-case schema;
- assigns stable IDs and normalized content hashes;
- removes exact and high-confidence within-source near duplicates;
- maps candidates to the frozen BridgeSAT skills or prerequisite nodes;
- performs a machine license precheck without claiming legal approval;
- flags sensitive or insufficient content for age-suitability review;
- assigns a transparent quality score and review route;
- keeps evaluation-only material isolated from product candidates;
- writes reports and review queues under `data/reviewed/`.

Automated processing never marks a record approved for student use.

Validate the generated partitions without external test dependencies:

```bash
python scripts/validate_review_outputs.py
```

Expected output:

```text
data/reviewed/
├── all-candidates.jsonl
├── review-queue.jsonl
├── evaluation-only.jsonl
├── blocked.jsonl
├── duplicates.jsonl
├── review-report.json
├── skill-distribution.json
└── routes/
```

## Security properties

- HTTPS only;
- explicit host allowlists;
- public-IP validation to reduce SSRF risk;
- redirect validation;
- response byte limits;
- per-host request spacing;
- atomic `.part` downloads;
- SHA-256 artifact hashes;
- fail-closed source registry checks.
