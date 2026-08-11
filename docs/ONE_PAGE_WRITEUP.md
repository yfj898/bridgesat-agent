# BridgeSAT: an offline-first SAT learning agent

## Problem

Students in underserved public schools often cannot rely on a private SAT tutor,
a modern device, or a continuous internet connection. Ordinary practice tools
record that an answer was wrong, but usually lose the teaching context between
sessions: what misconception occurred, what help was tried, and whether that
help actually worked.

## Solution

BridgeSAT is a browser-based adaptive learning companion. It diagnoses a weak
math skill, builds a short study plan, chooses bounded teaching actions, tracks
progress, and continues practice during a disconnection. Its distinctive memory
is about learning response, not chat history: BridgeSAT remembers which
intervention helped this learner recover from a misconception and can reuse that
evidence in a later session.

## How the Agent Works

```text
diagnose
→ track answers, hints, time, and mastery
→ detect a misconception
→ choose an intervention
→ observe a distinct transfer item
→ validate a Learning Episode
→ recall it in a later session
→ change and explain the next teaching action
```

For example, two `sign_error` observations can trigger
`SHOW_WORKED_EXAMPLE`. If the learner then succeeds on a different item, the
system validates an Episode. On the first similar error in a new session, that
Episode can make BridgeSAT show the worked example earlier. The decision retains
the Episode ID, reason code, and policy version.

## Why It Is More Than a Chatbot

The learner does not need to know what to ask. BridgeSAT takes initiative within
a safe action set: retry, offer a hint, show a micro-lesson or worked example,
adjust difficulty, review a prerequisite, or schedule review. Answers, mastery,
and state transitions are deterministic; an optional LLM cannot modify them.

## Accessibility

The mobile-first PWA caches its content pack and stores the active session,
skill/memory snapshot, and pending events in IndexedDB. After initial setup, the
learner can answer, request hints, receive local bounded adaptation, refresh and
recover, then reconnect and synchronize. Stable event IDs, device sequences,
and server deduplication prevent double scoring. The core loop does not require
Mnemis or an external LLM.

## Evidence

Controlled internal evaluation currently reports 24/24 policy trajectories,
10/10 offline/sync scenarios, 100% PostgreSQL similarity recall@3 and next-action
accuracy, 100% retrieval citation/license coverage, and zero restricted-source
hits. The governed pack passes 889/889 automated checks. The reported +5.7
percentage-point correctness delta is a **synthetic simulation — not a real
student outcome**.

## Potential Impact

BridgeSAT could give students who lack continuous tutoring a more persistent,
explainable form of individualized practice, while preserving learning progress
through weak connectivity. Real educational impact still requires human content
review, accessibility/usability testing, and a student study; no real SAT-score
gain is claimed.
