# AceSAT Submission Readiness

Status reflects repository evidence, not intent. `READY` means reproducible in
code; `BLOCKED` means a required human or submission asset is absent.

| Official Requirement | Status | Evidence | Remaining Work |
|---|---|---|---|
| original AI education agent | READY | deterministic diagnostic/policy plus runtime Learning Episodes | keep claims limited to math MVP |
| underserved public schools relevance | READY IN NARRATIVE | offline-first PWA and tutoring-continuity problem statement | validate with target students after competition |
| beyond chatbot | READY | system initiates bounded teaching actions without a learner prompt | show this before discussing technology |
| adaptive behavior | READY | answer → misconception/mastery → next action sync integration test | perform final browser walkthrough |
| progress tracking | READY | PostgreSQL mastery, events, misconception evidence, episodes | none for demo |
| autonomous decisions | READY | persisted action, reason code/text, policy version, Episode IDs | none for demo |
| cross-session memory changes experience | READY | memory path shows earlier worked example; no-memory path retries | capture both outcomes in the video |
| poor-network continuity | READY IN TESTS | IndexedDB snapshot/queue, local scoring/policy, 10/10 offline/sync scenarios | manually verify on target browser/device |
| working demo | CODE READY | FastAPI serves the PWA and public sync path returns decisions | complete timed clean-profile walkthrough and host it |
| real student interaction flow | CODE READY | diagnostic → plan → practice → intervention → new session | record an unedited successful run |
| three-minute video | BLOCKED | `docs/DEMO_SCRIPT.md` exists | record, trim to ≤3:00, upload, verify link |
| GitHub repository | LOCAL READY | source, tests, docs, reproducible commands | push approved final diff and verify public access |
| clear README | READY | one setup order, current PostgreSQL facts, honest limitations | insert final URLs/counts after freeze |
| one-page write-up | READY | `docs/ONE_PAGE_WRITEUP.md` | attach or paste into submission form |
| governed student content | BLOCKED FOR DEPLOYMENT | 889/889 automated checks; current reviewer IDs are `sim.*` | real humans must sign educational, answer, license, accessibility review |
| Impact | READY IN NARRATIVE | tutoring-access and continuity value is visible in first 20 seconds | do not claim measured SAT improvement |
| Innovation | READY | validated intervention outcome changes later timing | make Session 2 the demo climax |
| Technical Execution | READY WITH RISK | PostgreSQL authority, sync integration, reproducible tests/evals | final clean run and browser walkthrough |
| Accessibility | READY WITH RISK | browser-only/mobile/offline/refresh/reconnect design and tests | manual keyboard, screen-reader status, small-device check |

## Release blockers

The code can support a competition demo, but the complete submission is not
ready until the video/link package exists. The pack must not be represented as
human-approved: its reviewer ledger is simulated. Human review is mandatory
before real student deployment.

## Manual final gate

- New browser profile: create learner and finish the two-item diagnostic.
- Session 1: choose `B` on two distinct linear-equation questions, observe
  retry then worked example; choose `A` on the transfer item and observe episode
  validation.
- End the session. Session 2: choose `B` once and observe
  `RECALLED_SUCCESSFUL_EPISODE`, policy version, and Episode ID.
- Disconnect: select the next cached question, request a hint, answer, refresh,
  and confirm current state returns.
- Reconnect twice: queue reaches zero and mastery changes only once.
- Keyboard through every control; verify focus visibility and live status text.
- Run the single verification order in `README.md`; copy only fresh metrics.
