# BridgeSAT Agent

BridgeSAT is an offline-first adaptive SAT learning agent designed for students with limited tutoring access, older devices, or unstable internet connections.

The initial project skeleton contains:

- a FastAPI application;
- a deterministic diagnostic and adaptation engine;
- SQLite-backed student state;
- a small original question pack;
- a mobile-first PWA shell;
- unit and API tests;
- architecture and competition roadmaps.

## Planning documents

- `docs/ARCHITECTURE.md`: complete system architecture and design boundaries;
- `docs/IMPLEMENTATION_PLAN.md`: frozen technology choices, module contracts, evaluation gates, and dated delivery plan;
- `docs/ROADMAP.md`: concise execution checklist.
- `docs/PEDAGOGY_SPEC.md`: curriculum, mastery, misconception, intervention, and fairness rules;
- `docs/MEMORY_CONSISTENCY.md`: authoritative memory store, outbox, Mnemis indexing, correction, and deletion;
- `docs/SYNC_PROTOCOL.md`: offline events, idempotency, conflicts, and snapshots;
- `docs/THREAT_MODEL.md`: privacy and security threat model;
- `docs/API_AND_OPERATIONS.md`: API, migration, deployment, accessibility, and operations contract;
- `docs/EVALUATION_SPEC.md`: complete evaluation and acceptance criteria.
- `docs/DATA_SOURCE_REGISTRY.md`: approved, conditional, evaluation-only, and restricted data sources;
- `docs/DATA_ACQUISITION.md`: governed acquisition, generation, normalization, and review workflow;
- `config/sources.yaml`: machine-readable source permissions and acquisition controls.

## Core workflow

```text
student profile
  -> diagnostic answers
  -> skill-gap analysis
  -> daily study plan
  -> adaptive question selection
  -> error diagnosis
  -> plan adjustment
  -> progress persistence
```

## Quick start

```bash
cd bridgesat-agent
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000`.

Run tests:

```bash
pytest
```

## Current API

- `GET /health`
- `GET /v1/questions`
- `POST /v1/students`
- `POST /v1/diagnostics`
- `POST /v1/adapt`

## Design boundary

The core learning policy must continue working without an external LLM. A model may later improve explanations, classify free-form mistakes, or translate content, but it must not be required for question selection, mastery updates, progress storage, or offline practice.

## Status

This repository is a new, independent competition project. The included content is starter data for development and must be expanded and reviewed before submission.
