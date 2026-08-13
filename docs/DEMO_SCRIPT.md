# BridgeSAT offline-first SAT tutor remembers teaching strategies that actually helped and uses them sooner.

This three-minute student story shows continuity: the tutor notices a repeated
stuck point, verifies what helps, and brings that help back in the next session.
Keep the focus on what the student sees. Do not show architecture diagrams,
internal action labels, episode identifiers, policy versions, or sync queues.

## Final recorded artifact

The final submission cut is `BridgeSAT_Submission_Demo_2m44s_Voiceover.mp4`:
164.000 seconds (2:44), 1920×1080, 30 fps, H.264 video, AAC stereo audio, English
hard subtitles, and natural US-English neural narration. The recorded story is:
diagnostic → `RETRY_SAME_SKILL` → `SHOW_WORKED_EXAMPLE` → different-item transfer
validation → Session 2 recall → learning-record evidence → offline learning →
refresh recovery → reconnect with `pending = 0` and `failed = 0`.

The video production and hosting gates are complete. The hosted submission video
is `https://youtu.be/BE-CNibViYk`; verify it once in an anonymous/incognito browser
before final Devpost submission and use that same URL in the video field.

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

## Recording target

Aim for a **2:40–2:50 final cut**, not 3:00 exactly. The submission limit is
three minutes, so leave time for page loads, clicks, reconnection, and video
encoding. It is fine to cut between the major sections below as long as the
recording does not imply behavior that did not occur.

### Deterministic runtime click sheet for `bridgesat-math-0.3.0`

Use these choices for the current published pack **as returned by the runtime
content-pack API** and the deterministic PWA picker. The API returns items in a
different order than the source JSONL, so this table is the recording authority.
Do not substitute a generic "choose B" instruction: distractor letters differ
by item.

| Stage | Item | Choice | Purpose |
|---|---|---:|---|
| Diagnostic 1 | `math.coordinate_geometry.001` | **A** | wrong; maps to `slope_sign_error` and makes coordinate geometry the weak area |
| Diagnostic 2 | `math.exponents_radicals.001` | **B** | correct |
| Session 1 — first practice | `math.coordinate_geometry.001` | **A** | first `slope_sign_error`; expect one-more-similar response |
| Session 1 — second practice | `math.coordinate_geometry.002` | **A** | second `slope_sign_error`; expect worked example |
| Session 1 — different-item check | `math.coordinate_geometry.003` | **C** | correct on a different item; validates the intervention |
| Session 2 — first practice | `math.coordinate_geometry.001` | **A** | same `slope_sign_error`; expect immediate recalled worked example |

The competition proof is therefore **A / B diagnostic, then A / A / C / A**
for the two-session learning-memory path. This exact route was verified against
the running PWA and produced a validated `SHOW_WORKED_EXAMPLE` episode before
the Session 2 recall.

## 0:00–0:15 — The tutoring continuity problem

Show the mobile-width PWA.

> A student may not have a personal tutor, a new device, or a reliable connection.
> When ordinary practice ends, it can forget the teaching strategy that was just
> beginning to help. Every student deserves a tutor that remembers what actually
> helps them learn. BridgeSAT is an offline-first SAT tutor that remembers
> teaching strategies that actually helped and uses them sooner.

## 0:15–0:50 — Starting point, then Session 1 practice

Create the learner. On `math.coordinate_geometry.001`, choose **A**; on
`math.exponents_radicals.001`, choose **B**. The first answer is wrong and the
second is correct, which makes coordinate geometry the useful starting point.
Do not characterize those two diagnostic questions as similar.

Then begin Session 1 practice. On the first coordinate-geometry practice problem
(`math.coordinate_geometry.001`), choose **A**. This choice maps to
`slope_sign_error`. After the first answer, show: **Try one more similar problem
so I can see whether the same step is getting in the way.** On the next practice
problem (`math.coordinate_geometry.002`), choose **A**, which maps to the same
`slope_sign_error`. Show the worked-example intervention that follows the
repeated stuck point.

> BridgeSAT does not ask the student to diagnose the mistake. It notices the
> repeated pattern and offers a useful next step.

## 0:50–1:15 — Check the help on a new problem

Use **Try a new problem** after the lesson. Answer the different-item verification
problem `math.coordinate_geometry.003` with **C** (correct), then show: **This
approach worked on a new problem.** Point out that it is not either item that
triggered the help.

> This verifies that the teaching strategy worked on a different item before the
> tutor remembers it for a later session.

## 1:15–1:45 — Session 2 remembers what helped

End the first session and start a second one. The deterministic picker returns
`math.coordinate_geometry.001`; choose **A** again to produce the same
`slope_sign_error`. Show **This helped you before** / **Let's use the approach
that helped before**. The same successful intervention should now appear
immediately, without waiting for a second repeat.

> The tutor intervenes earlier because this same teaching strategy was validated
> by the new-problem success in Session 1.

## 1:45–2:10 — Keep learning offline

Disable the network. Continue cached practice, answer a problem, and refresh the
page. Show the session return after refresh. Restore the network and show that
progress saves automatically after reconnection.

> The student can keep practicing through a weak connection; the tutor preserves
> the session and saves progress when service returns.

## 2:10–2:35 — Open the learning record only for evaluator evidence

Open the collapsed **Learning record details** section for evaluator evidence.
Point out the same misconception, the validated intervention, the exact approved
content shown, the different-item transfer, and the recalled action in the next
session.

> This is verified interaction and transfer evidence for this controlled
> interaction: the same misunderstanding, a confirmed intervention, and success
> on a different problem. Under BridgeSAT's existing learning-memory semantics,
> that different-item success validates the intervention for later recall. It does
> not establish permanent mastery or SAT-score improvement.

## 2:35–2:45 — Close

> Every student deserves a tutor that remembers what actually helps them learn.
