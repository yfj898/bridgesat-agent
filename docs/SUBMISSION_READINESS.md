# AceSAT Submission Readiness

Status reflects repository and browser evidence, not intent. `READY` means the
competition path has been reproduced. `BLOCKED` is reserved for an official
submission artifact that is still absent; deployment and scoring risks are
labeled separately.

| Official Requirement | Status | Evidence | Remaining Work |
|---|---|---|---|
| original AI education agent | READY | deterministic diagnostic/policy plus runtime Learning Episodes | keep claims limited to math MVP |
| underserved public schools relevance | READY IN NARRATIVE | offline-first PWA and tutoring-continuity problem statement | validate with target students after competition |
| beyond chatbot | READY | system initiates bounded teaching actions without a learner prompt | show this before discussing technology |
| adaptive behavior | READY | clean Chrome walkthrough reproduced first-error retry → repeated-error worked example → different-item success | none for demo path |
| progress tracking | READY | PostgreSQL mastery, events, misconception evidence, episodes | none for demo |
| autonomous decisions | READY | persisted action, reason code/text, policy version, Episode IDs | none for demo |
| cross-session memory changes experience | READY IN BROWSER | Session 1 created a validated `slope_sign_error` + `SHOW_WORKED_EXAMPLE` episode; Session 2 first same error immediately returned `RECALLED_SUCCESSFUL_EPISODE` | capture the Session 2 recall in the video |
| poor-network continuity | READY IN BROWSER | real Chrome: offline answer/hint queued, offline refresh restored item/feedback/hint/queue, reconnect automatically cleared pending events; controlled eval remains 10/10 | repeat once in the recording browser before capture |
| working demo | READY IN BROWSER | clean Chrome profile reproduced the full deterministic student path against the FastAPI/PostgreSQL runtime | record the verified route |
| real student interaction flow | READY IN BROWSER | runtime route `A/B → A/A/C → A`: diagnostic → retry → worked example → different-item validation → later-session recall | record and edit the successful run |
| public deployment and working-demo URL | OPTIONAL / OPEN | local FastAPI/PWA working demo is browser-verified; official rules require a working demo but do not make a hosted URL a separate listed artifact | add a hosted URL if available; do not block the code freeze on hosting |
| three-minute video | BLOCKED | `docs/DEMO_SCRIPT.md` exists | record, trim to ≤3:00, upload, verify link |
| GitHub repository | READY | public `yfj898/bridgesat-agent`, default branch `main`; anonymous repository/README access verified | push this final demo-hardening commit after verification |
| clear README | READY | student-value-first opening, one setup order, PostgreSQL facts, honest limitations | insert final video/demo links after upload |
| one-page write-up | READY | `docs/ONE_PAGE_WRITEUP.md` | attach or paste into submission form |
| governed student content | BLOCKED FOR DEPLOYMENT | 1799/1799 automated checks; current reviewer IDs are `sim.*`; pack is 8 math skills, 103 questions, 24 lessons | real humans must sign educational, answer, license, accessibility review |
| Impact | READY IN NARRATIVE | tutoring-access and continuity value is visible in first 20 seconds | do not claim measured SAT improvement |
| Innovation | READY | validated intervention outcome changes later timing | make Session 2 the demo climax |
| Technical Execution | READY | PostgreSQL authority, clean-checkout evidence, real-browser sync/memory path, unsupported practice `SESSION_STARTED` removed after browser audit | fresh final evidence regenerated after the Web regression |
| Accessibility | READY WITH MANUAL RISK | real Chrome at 360×640: no horizontal overflow, 48px minimum visible button, visible 3px keyboard focus; 200% emulated page scale had no horizontal overflow; offline/refresh/reconnect verified | perform a real screen-reader pass; CDP accessibility-tree inspection is not a screen-reader test |
| H9 Hybrid configuration freeze | READY | `reports/hybrid_final_gate.json`; deterministic final mode; all five flags frozen at `0`; H7 action ranking **No-Go** | keep the default disabled unless a separately verified opt-in run is explicitly requested |

## Release blockers

The **official submission artifact still missing is the ≤3-minute video**. The
public GitHub repository, one-page write-up, and working local demo path are
ready. A hosted demo URL remains useful but is treated here as a quality option,
not as a separate code blocker. A real screen-reader walkthrough remains an
Accessibility scoring risk. The pack must not be represented as human-approved:
its reviewer ledger is simulated, and real human content review remains a
student-deployment blocker. Real student outcome evidence remains an Impact
evidence gap, not a hackathon eligibility claim.

## Manual final gate

- New browser profile: diagnostic `math.coordinate_geometry.001` → **A**
  (`slope_sign_error`), then `math.exponents_radicals.001` → **B** (correct).
- Session 1: `coordinate_geometry.001` → **A** (first error),
  `coordinate_geometry.002` → **A** (same error → worked example), then
  `coordinate_geometry.003` → **C** (correct different-item check). Confirm a
  validated `SHOW_WORKED_EXAMPLE` episode and an empty sync queue.
- End Session 1. Start Session 2 and choose **A** on
  `coordinate_geometry.001`; confirm `This helped you before`,
  `RECALLED_SUCCESSFUL_EPISODE`, and the prior Episode ID on the first error.
- Disconnect: move to a cached question, request a hint, answer, and verify the
  pending events remain on-device. Refresh while the POST path is unreachable;
  confirm the same item, feedback, opened-hint state, and queue return from
  Service Worker + IndexedDB recovery.
- Reconnect: confirm the browser `online` handler automatically drains the
  pending queue to zero.
- At 360×640, verify no horizontal overflow and ≥44px touch targets. Keyboard
  through visible controls and verify the focus outline. Repeat at 200% zoom.
- Run the verification order in `README.md`; copy only fresh metrics. A real
  screen-reader pass remains separate from the Chrome accessibility-tree check.
