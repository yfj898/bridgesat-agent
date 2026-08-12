# BridgeSAT offline-first SAT tutor remembers teaching strategies that actually helped and uses them sooner.

This three-minute student story shows continuity: the tutor notices a repeated
stuck point, verifies what helps, and brings that help back in the next session.
Keep the focus on what the student sees. Do not show architecture diagrams,
internal action labels, episode identifiers, policy versions, or sync queues.

## Before recording

Use the setup and verification path in `README.md`, then open a clean browser
profile at `http://127.0.0.1:8000`. Do not delete databases or edit reports.

## Final configuration preflight

Before recording, confirm `final_mode=deterministic` in
`docs/HYBRID_FINAL_CONFIGURATION.md`. Set
`BRIDGESAT_HYBRID_COMPETITION_MODE=1`, then confirm all five Hybrid flags are
`0`:

- `BRIDGESAT_HYBRID_ENABLED=0`;
- `BRIDGESAT_HYBRID_SHADOW_ENABLED=0`;
- `BRIDGESAT_HYBRID_EXPLANATION_ENABLED=0`;
- `BRIDGESAT_HYBRID_ACTION_RANKING_ENABLED=0`.
- `BRIDGESAT_HYBRID_SUMMARY_ENABLED=0`.

H7 is **No-Go** for this recording. No live provider is used. The current content
review is simulated and is not human-approved.

## 0:00–0:20 — The tutoring continuity problem

Show the mobile-width PWA.

> A student may not have a personal tutor, a new device, or a reliable connection.
> When ordinary practice ends, it can forget the teaching strategy that was just
> beginning to help. Every student deserves a tutor that remembers what actually
> helps them learn. BridgeSAT is an offline-first SAT tutor that remembers
> teaching strategies that actually helped and uses them sooner.

## 0:20–0:55 — Starting point, then Session 1 practice

Create the learner and answer the two diagnostic questions to find a useful
starting point. Do not characterize those two questions as similar.

Then begin Session 1 practice. On the first similar linear-equation practice
problem, choose `B`. After the first answer, show: **Try one more similar problem
so I can see whether the same step is getting in the way.** On the next similar
practice problem, choose `B` again. Show the intervention that follows the
repeated stuck point.

> BridgeSAT does not ask the student to diagnose the mistake. It notices the
> repeated pattern and offers a useful next step.

## 0:55–1:20 — Check the help on a new problem

Use **Try a new problem** after the lesson. Answer the different-item verification
problem correctly, then show: **This approach worked on a new problem.** Point
out that it is not the item that triggered the help.

> This verifies that the teaching strategy worked on a different item before the
> tutor remembers it for a later session.

## 1:20–1:55 — Session 2 remembers what helped

End the first session, start a second one, and make the planned first similar
error. Show that the same successful intervention appears early, without waiting
for a second repeat.

> The tutor intervenes earlier because this same teaching strategy was validated
> by the new-problem success in Session 1.

## 1:55–2:25 — Keep learning offline

Disable the network. Continue cached practice, answer a problem, and refresh the
page. Show the session return after refresh. Restore the network and show that
progress saves automatically after reconnection.

> The student can keep practicing through a weak connection; the tutor preserves
> the session and saves progress when service returns.

## 2:25–2:50 — Open the learning record only for evaluator evidence

Open the collapsed **Learning record details** section for evaluator evidence.
Point out the same misconception, the validated intervention, the exact approved
content shown, the different-item transfer, and the recalled action in the next
session.

> This is verified interaction and transfer evidence for this controlled
> interaction: the same misunderstanding, a confirmed intervention, and success
> on a different problem. Under BridgeSAT's existing learning-memory semantics,
> that different-item success validates the intervention for later recall. It does
> not establish permanent mastery or SAT-score improvement.

## 2:50–3:00 — Close

> Every student deserves a tutor that remembers what actually helps them learn.
