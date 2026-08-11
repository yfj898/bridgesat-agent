# BridgeSAT AceSAT three-minute demo

This script proves student value, agent initiative, memory-caused behavior, and
offline continuity. Do not show architecture diagrams or optional LLM/Mnemis.

## Before recording

Use the single setup/verification order in `README.md`, then open a clean browser
profile at `http://127.0.0.1:8000`. Do not delete databases or edit report files.
The current content review ledger is simulated; do not call it human-approved.

## 0:00–0:20 — Problem and impact

Show the mobile-width PWA.

> Many students in underserved public schools cannot rely on a personal SAT
> tutor, and unstable internet makes cloud-only tutoring even less reliable.

## 0:20–0:45 — Diagnose and plan

Create the learner. On the linear-equation diagnostic item choose `B` to expose
`sign_error`; on the different-skill item choose its correct answer as shown.
Wait for the plan card to confirm `linear equations` as the weak area before
continuing.

> BridgeSAT does not wait for the student to know what to ask. It identifies the
> weak skill and chooses what to do next.

## 0:45–1:20 — Session 1 learns what helps

Start personalized practice focused on linear equations.

1. Choose `B` on the first linear item: `sign_error`; show
   `RETRY_SAME_SKILL`.
2. Choose `B` on the next distinct item: repeated `sign_error`; show
   `SHOW_WORKED_EXAMPLE`.
3. Point to the short reason and lesson review/license/source metadata.
4. Choose `A` on the next distinct transfer item; show
   `Learning memory validated` and its Episode ID.

> BridgeSAT observed both the misconception and whether the intervention was
> followed by transfer success.

## 1:20–1:55 — Session 2 is the innovation proof

End the session, start a new one, and choose `B` on the first similar item.
Show `SHOW_WORKED_EXAMPLE`, `RECALLED_SUCCESSFUL_EPISODE`, policy version, and
Episode ID.

> In Session 1, the first error produced a retry. With successful memory, the
> first similar error in Session 2 produces the worked example immediately.
> Memory changed what the student experiences.

## 1:55–2:30 — Accessibility under disconnection

Disable the network, select `Next question`, request a hint on that cached item,
answer it, and refresh. Show that the current question/session returns and the
pending-event count remains.

> The core learning loop uses the cached approved record, local scoring, and a
> bounded local policy. It does not depend on a live AI connection.

## 2:30–2:45 — Recovery without double scoring

Restore the network and synchronize. Show the pending count reach zero. Trigger
sync again.

> Stable event IDs and PostgreSQL deduplication make retry safe; the answer is
> not scored twice.

## 2:45–3:00 — Evidence and close

Show one evidence screen only: current Python/Web pass counts, 10/10
offline/sync, and the two-session memory proof. Use values from the final run.

> BridgeSAT does not just remember what a student got wrong. It remembers what
> helped that student recover and reuses that evidence across sessions, even
> when connectivity is unreliable.

Label any educational-improvement number as **synthetic simulation — not real
student outcome**.
