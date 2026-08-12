# BridgeSAT 当前实现链路

> Current runtime truth. Earlier SQLite/FTS5 plans are historical and were
> superseded by the PostgreSQL migration.

## Runtime authority

- modular monolith: FastAPI + static PWA;
- authoritative state: PostgreSQL;
- schema: `app/infrastructure/migrations_pg/0001`–`0016`;
- learner events, projections, episodic memory, outbox, sync, content registry:
  PostgreSQL;
- retrieval: PostgreSQL `tsvector` + GIN, metadata/prerequisite reranking, and
  citation/license validation;
- Mnemis/LLM: optional, derived, rebuildable, never authoritative.

## H9 final Hybrid boundary

The competition configuration is frozen to `final_mode=deterministic` and sets
`BRIDGESAT_HYBRID_COMPETITION_MODE=1`; startup rejects contradictory nonzero
Hybrid flags and runtime task gates remain deterministic. All five
Hybrid flags are `0`: `BRIDGESAT_HYBRID_ENABLED`,
`BRIDGESAT_HYBRID_SHADOW_ENABLED`, `BRIDGESAT_HYBRID_EXPLANATION_ENABLED`,
`BRIDGESAT_HYBRID_ACTION_RANKING_ENABLED`, and
`BRIDGESAT_HYBRID_SUMMARY_ENABLED`. H7 action ranking and H8 summary remain
opt-in, verified, and fail-closed paths. H7 action ranking is **No-Go** for
default enablement because the final controlled evidence has scripted-provider
timings but no repeated real-provider latency or lock-duration evidence. See
`docs/HYBRID_FINAL_CONFIGURATION.md` and `reports/hybrid_final_gate.json`.

## Connected learning path

| Step | Runtime implementation | Output consumed by |
|---|---|---|
| diagnose | PWA diagnostic + `/v1/diagnostics` | weakest skill and timed plan |
| answer | PWA local scoring + `ANSWER_SUBMITTED` | pending event and local feedback |
| evidence | `SyncService` version-bound scoring | mastery and misconception projections |
| decide | `decide_next_action` with session evidence and recalled episodes | persisted `agent_events` |
| display | sync `server_events` + snapshot `recent_agent_events` | PWA intervention card + version-bound worked-example/micro-lesson presentation confirmation |
| outcome | next question distinct from the triggering error after a confirmed worked example | runtime episode completion |
| remember | validated episode + semantic fact + intervention statistics | PostgreSQL recall |
| reuse | later-session similar error | earlier `SHOW_WORKED_EXAMPLE` |

The public integration proof is
`tests/test_pg_sync.py::test_runtime_sync_builds_episode_and_memory_changes_next_session_action`.
It submits real sync events rather than directly constructing an episode.

## Observable memory difference

```text
Session 1, first sign_error  → RETRY_SAME_SKILL
Session 1, repeated error    → SHOW_WORKED_EXAMPLE
PWA confirms exact example  → candidate Episode
distinct transfer succeeds  → validated Episode
Session 2, first sign_error  → SHOW_WORKED_EXAMPLE
reason                      → RECALLED_SUCCESSFUL_EPISODE + Episode ID
no-memory baseline          → RETRY_SAME_SKILL
```

The PWA immediately renders a deterministic local decision. When online, the
server decision replaces it and exposes reason code, policy version, Episode ID,
and approved worked-example lineage/license/review metadata. Offline policy can
reuse the last synchronized validated-episode snapshot.

## Offline path

IndexedDB stores the profile, active session, skill/strategy snapshots, content
pack, pending/acknowledged events, and sync cursor. Local answer judgment is
bound to question ID/version. Network failure returns dequeued events to a
retryable state. Refresh restores the current question, phase, hints, feedback,
mastery, and queued events. Reconnect sends stable event IDs and device sequence
numbers; PostgreSQL orders each batch by a strictly increasing device sequence
and deduplicates replay before projections are updated. The local bounded policy
also executes the time-budget closure and ends with review rather than starting
a new item.

## Content and retrieval path

```text
published/review metadata filter
→ license/source filter
→ skill/subskill/misconception filter
→ PostgreSQL tsvector
→ prerequisite expansion
→ deterministic reranking
→ citation and license validation
```

GSM8K is evaluation-only. Restricted or reference-only sources cannot enter the
student pack or retrieval result. LightRAG, embeddings, A-RAG, and RAG-Anything
remain disabled unless they beat the PostgreSQL baseline on Recall@3 without
losing latency, citation, or license coverage. A-RAG is limited to complex plans.

The current `bridgesat-math-0.3.0` pack publishes 103 questions and 24 lessons
across eight skills. Lessons carry explicit misconception targets, and new-skill
questions carry trigger/transfer metadata. PostgreSQL imports and indexes both
questions and lessons; the PWA installs the latest semantic pack version while
retaining the exact cached version for offline scoring and recovery.

## Verification and honest limits

Use the single command order in `README.md`. Generated metrics live in
`reports/final_summary.md`, with the artifact index in `docs/EVIDENCE_PACK.md`.
The repository proves controlled software behavior, not real SAT-score
improvement. A real human content review, manual accessibility walkthrough,
public deployment, and recorded three-minute video remain human submission
gates.
