# BridgeSAT competition MVP execution status

> Historical / superseded by PostgreSQL migration. This document originally
> planned a SQLite/FTS5 MVP. It now records the current competition-completion
> sequence; PostgreSQL is the only authoritative runtime.

## Completed engineering gates

| Dependency order | Current output | Acceptance evidence |
|---|---|---|
| 1. schema | PostgreSQL migrations `0001`–`0015` | migration and clean-start tests |
| 2. immutable events | learner-scoped learning/agent events | sync, isolation, replay tests |
| 3. session projections | state machine, attempts, mastery, misconception evidence | state/policy tests |
| 4. bounded policy | deterministic actions and versioned reasons | 24 golden trajectories |
| 5. episodic memory | runtime episode builder, semantic facts, intervention stats | two-session public sync test |
| 6. memory-aware action | recalled episode changes first similar-error action | memory/no-memory comparison |
| 7. offline sync | IndexedDB recovery, stable IDs/sequences, idempotent server sync | 10/10 controlled scenarios |
| 8. governed retrieval | PostgreSQL `tsvector`, filters, prerequisite rerank, citations | retrieval eval and restricted-source audit |
| 9. optional memory | transactional outbox, Mnemis timeout, PostgreSQL fallback | memory ablation/security tests |
| 10. evidence | reproducible reports and demo seed | `evals.run_all` |

## Competition-critical runtime fix

The previous PWA and backend golden path were parallel proofs. They are now
connected through `POST /v1/sync/events`:

1. `ANSWER_SUBMITTED` is scored against the referenced content version.
2. The server updates mastery and misconception evidence in the event
   transaction.
3. The bounded policy receives current session evidence and successful prior
   episodes.
4. The resulting `agent_event` records action, reason, policy version, and
   episode IDs.
5. `server_events` and the snapshot expose the decision to the PWA.
6. A later distinct correct item validates the intervention episode.

The PWA displays `SHOW_WORKED_EXAMPLE`,
`RECALLED_SUCCESSFUL_EPISODE`, the Episode ID, and a short explanation. It also
shows approved lesson review/license/lineage metadata.

## Frozen architecture and current content scope

- math only: 103 original questions, 12 micro-lessons, and 12 worked examples
  across 8 skills in `bridgesat-math-0.3.0`;
- no microservices, multi-agent framework, graph database, or large vector DB;
- no external model or Mnemis dependency in the core loop;
- no GSM8K product content and no acquisition from College Board, Khan Academy,
  OpenStax, or license-unclear sources;
- no unreviewed record is selectable by the PWA;
- PostgreSQL is authoritative; Mnemis is derived and rebuildable.

## Remaining submission sequence

These are human/package gates, not architecture work:

1. complete and sign a real human educational/answer/license/accessibility
   review for all 103 questions and 24 lessons;
2. run the manual browser/mobile accessibility and offline walkthrough recorded
   in `docs/SUBMISSION_READINESS.md`;
3. execute the one reproducible verification order in `README.md` and freeze the
   newly generated metrics;
4. record the three-minute flow in `docs/DEMO_SCRIPT.md` without editing or
   fabricating outputs;
5. add the working demo URL, video URL, screenshots, and final repository URL to
   the submission form;
6. confirm `docs/ONE_PAGE_WRITEUP.md` and README use the same claims.

## Release blockers and fallback

| Risk | Decision |
|---|---|
| real content review incomplete | do not describe the pack as human-approved or deploy to students |
| Mnemis/LLM unavailable | PostgreSQL recall and deterministic policy continue |
| weak/no network | local scoring/policy and pending queue continue after initial setup |
| retrieval enhancement underperforms | retain PostgreSQL `tsvector` baseline |
| offline conflict/unknown content version | preserve the local event, reject safely, never guess a score |
| browser walkthrough finds loss/repetition | block recording, add a regression, then rerun all gates |

## Final acceptance

Engineering is competition-demonstrable only when the public PWA visibly proves
memory changes the teaching action. The overall submission is ready only after
the human content-review and required video/link assets above are complete.
