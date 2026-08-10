# Repository Guidelines

## Project Structure & Module Organization

BridgeSAT is a Python 3.11+ FastAPI application with a mobile-first PWA shell.
Core backend code lives in `app/`: API entrypoint in `app/main.py`, deterministic
learning logic in `app/domain/` and `app/agent/`, persistence and migrations in
`app/infrastructure/`, ingestion in `app/ingestion/`, and governed content tooling
in `app/content_pipeline/`. The PWA lives in `web/`, including offline modules and
service worker code. Tests are under `tests/`, with golden scenarios in
`tests/golden/`, security tests in `tests/security/`, Node tests in `tests/node/`,
and web-specific tests in `web/tests/`. Content, acquisition, review, and published
packs are under `data/` and `content/`; long-form design and operations documents
are under `docs/`.

## Build, Test, and Development Commands

Set up locally with:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

Run the API/PWA shell:

```bash
python scripts/import_content_pack.py
python scripts/seed_demo.py
uvicorn app.main:app --reload
```

Use `pytest` for the Python suite, `node --test web/tests/*.test.js` for PWA
offline/accessibility paths, and `python -m evals.run_all` to regenerate evaluation
reports. Use `docker compose up -d postgres` when working against PostgreSQL and
RLS migrations.

## Coding Style & Naming Conventions

Use four-space indentation and type hints for Python. Keep domain logic
deterministic and side-effect-light; persistence belongs in `app/infrastructure/`.
Prefer Pydantic models for API contracts and structured records. Python modules and
functions use `snake_case`; classes use `PascalCase`; constants use `UPPER_SNAKE_CASE`.
Frontend code is plain JavaScript/CSS, so keep modules small and explicit.

## Testing Guidelines

Tests use `pytest` and Node's built-in test runner. Name Python test files
`test_*.py` and keep fixtures in `tests/fixtures/`. Add focused tests for every
state-machine, memory, sync, content, or security change. Core invariants should be
covered by golden or security tests before being treated as submission-ready.

## Commit & Pull Request Guidelines

Recent history uses Conventional Commit prefixes, often with Chinese summaries:
`feat: ...`, `fix: ...`, `docs(plan): ...`. Keep commits scoped and evidence-based.
PRs should include a short problem statement, implementation summary, test/eval
commands run, screenshots for PWA changes, and links to relevant docs or issues.

## Security & Data Governance

PostgreSQL is the authority for learner facts and events; optional memory or LLM
layers must degrade safely. Do not add College Board, Khan Academy, OpenStax, or
license-unclear content paths. GSM8K is evaluation-only. Student-facing content
must come from approved packs with source lineage, license metadata, review status,
and content hashes.
