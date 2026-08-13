# BridgeSAT: a tutor that remembers what helps

Students in underserved public schools may not have a personal SAT tutor, a
modern device, or a stable internet connection. A practice tool that only records
wrong answers makes each new session feel like a new start, even when a teaching
strategy was beginning to help.

BridgeSAT is a browser-based SAT Math tutor that keeps the useful part of that
learning context. It notices a repeated stuck point, offers approved help, checks
that help on a different problem, and can use what helped sooner in a later
session. The current governed math pack covers 8 skills, 103 questions, and 24
lessons (12 micro lessons and 12 worked examples).

## Verified learning memory

```text
error -> evidence -> intervention -> confirmation -> different-item transfer -> record -> recall -> early personalized intervention
```

The learner does not need to diagnose the mistake before receiving help. BridgeSAT
only creates a learning record after it confirms the exact approved content was
shown and the student succeeds on a different problem. The later response is based
on that verified learning memory, not chat history or an unsupported promise of
mastery.

## Student Experience

BridgeSAT takes initiative within a safe action set: it can offer a retry, hint,
micro-lesson, worked example, prerequisite review, or a new problem. The student
sees a useful next step rather than internal system labels. Answers, mastery
calculations, and learning-state transitions remain deterministic; an optional LLM
cannot modify them.

## Accessibility and Continuity

The mobile-first PWA caches its content pack and stores the active session,
learning-memory snapshot, and pending events in IndexedDB. After initial setup,
the learner can answer, request hints, receive local bounded adaptation, refresh
and recover, then reconnect and synchronize. Stable event IDs, device sequences,
and server deduplication prevent double scoring. The core loop does not require
Mnemis, an external LLM, or a continuous connection.

## Technical Execution and Evidence

Controlled internal evaluation currently reports 24/24 policy trajectories,
10/10 offline/sync scenarios, 100% PostgreSQL similarity recall@3 and next-action
accuracy, 100% retrieval citation/license coverage, and zero restricted-source
hits. The fresh full-suite baseline is 850 passed Python tests and 57 passed Web
tests. The governed pack passes 1799/1799 automated checks. The reported +5.7
percentage-point correctness delta is a **synthetic simulation — not a real
student outcome**.

Optional grounded Hybrid explanation and summary paths can use validated learner
facts, approved content, and deterministic fallback. They are opt-in,
fail-closed, and do not change answer truth, mastery, or the state machine. H9
freezes the competition configuration to deterministic mode; H7 action ranking
is not default-enabled because the final evidence lacks repeated real-provider
latency and lock-duration measurements. No model-based learning outcome is
claimed.

The current content review ledger is a controlled/simulated review artifact, not
human-approved content. Human review remains required before student deployment.

## Potential Impact

BridgeSAT could give students who lack continuous tutoring a more persistent,
explainable form of individualized practice, while preserving learning progress
through weak connectivity. Real educational impact still requires human content
review, accessibility/usability testing, and a student study; no real SAT-score
gain is claimed.
