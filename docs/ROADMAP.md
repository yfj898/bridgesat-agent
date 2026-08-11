# BridgeSAT Competition Roadmap

Current runtime: PostgreSQL authority, migrations `0001`–`0015`, PostgreSQL
`tsvector` retrieval, and optional rebuildable Mnemis. Historical SQLite/FTS5
milestones are superseded.

## Engineering gates

- [x] FastAPI modular monolith and learner-scoped bearer authentication
- [x] immutable learning/agent events and versioned projections
- [x] session state machine and bounded teaching actions
- [x] weighted-Beta mastery and misconception evidence
- [x] PostgreSQL episodic memory, facts, intervention aggregates, outbox
- [x] public sync path builds a validated Episode from real transfer evidence
- [x] later-session recall changes action timing and retains trace metadata
- [x] IndexedDB active-session recovery, local scoring/policy, pending queue
- [x] idempotent, out-of-order, conflict, and version-bound synchronization
- [x] governed math pack and PostgreSQL full-text/citation/license retrieval
- [x] Mnemis timeout and PostgreSQL fallback
- [x] deterministic evaluation runner prepares its own retrieval index
- [x] one-page write-up and submission-readiness matrix

## Submission gates

- [ ] real human educational/answer/license/accessibility review of 103 questions
  and 24 lessons; current `sim.*` ledger is not a human approval
- [ ] manual keyboard, screen-reader status, small-screen, offline/refresh, and
  reconnect walkthrough
- [ ] clean public deployment and working-demo URL
- [ ] screenshots and ≤3-minute video following `docs/DEMO_SCRIPT.md`
- [ ] public GitHub URL and link verification
- [ ] final metric freeze from the exact README command order

## Post-competition, not MVP

- reading/writing curriculum and broader skill taxonomy;
- real student usability and educational-outcome studies;
- teacher-facing tools or analytics;
- optional retrieval enhancements only if they beat the PostgreSQL baseline on
  Recall@3, latency, citation coverage, and license coverage.

No microservices, multi-agent framework, large vector database, unreviewed
automatic publishing, restricted-source crawling, or external-model dependency
is planned for the competition submission.
