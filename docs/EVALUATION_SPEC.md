# BridgeSAT Evaluation Specification

## 1. Evaluation layers

BridgeSAT must be evaluated at six levels:

```text
policy correctness
educational behavior
knowledge retrieval
long-term memory
offline and synchronization
security, fairness, and accessibility
```

No single retrieval metric is sufficient to support a learning-effect claim.

---

## 2. Dataset separation

Use separate sets for development, policy golden tests, retrieval golden queries, memory scenarios, the final scripted demo, and optional human usability testing.

Do not tune thresholds on the final evaluation scenarios.

Every result is labeled as one of:

```text
synthetic simulation
controlled internal test
human usability test
real educational outcome
```

The competition submission must not describe synthetic simulation as real student improvement.

---

## 3. Policy evaluation

Minimum 20 deterministic scenarios covering:

- repeated misconception;
- difficulty increase with sufficient confidence;
- no increase after high-level hints;
- low confidence requests more evidence;
- prerequisite review;
- overdue review;
- insufficient remaining time;
- memory conflict with recent evidence;
- unavailable content;
- offline fallback;
- stale memory;
- student-corrected memory.

Targets:

```text
overall pass rate >= 90%
all safety-critical scenarios = 100%
```

---

## 4. Educational behavior evaluation

### 4.1 Immediate transfer

After an intervention, evaluate on a different item with the same target skill and no copied surface form.

### 4.2 Short-term stability

Evaluate the next two valid items in the same session.

### 4.3 Delayed retention

Evaluate the first valid relevant item in a later session.

### 4.4 Hint dependency

Report correctness and highest hint level separately.

### 4.5 Difficulty control

Comparisons between interventions must use equivalent difficulty bands.

Required reported measures:

```text
correctness
hint level
mastery change
confidence change
transfer success
retention success
intervention selected
```

---

## 5. RAG evaluation

Baselines:

```text
metadata only
PostgreSQL tsvector
PostgreSQL tsvector + hierarchy
PostgreSQL tsvector + hierarchy + embedding
LightRAG adapter, if enabled
```

Metrics:

```text
Recall@1
Recall@3
MRR
skill-match accuracy
misconception-match accuracy
prerequisite-root-cause accuracy
citation coverage
license coverage
restricted-source exclusion
latency
context size
```

Go/No-Go for LightRAG:

- baseline evaluation exists;
- measurable improvement on at least one important retrieval metric;
- no unacceptable citation or license regression;
- latency remains within the complex-query budget;
- deployment remains reliable.

Otherwise LightRAG remains an experiment, not a demo dependency.

---

## 6. Memory evaluation

Baselines:

```text
no memory
recent N episodes
similarity retrieval
Mnemis System-1
Mnemis dual route
```

Scenarios include:

- second-session recall;
- paraphrased but equivalent misconception;
- shared prerequisite behind different surface errors;
- irrelevant old episode;
- contradictory new evidence;
- corrected learner memory;
- archived memory;
- backend outage;
- deletion verification.

Metrics:

```text
Episode Recall@5
memory precision
cross-session fact coverage
root-cause accuracy
intervention-selection accuracy
next-action accuracy
unsupported-memory rate
latency
fallback success rate
```

Go/No-Go for Mnemis in the live demo:

- PostgreSQL two-session memory loop already passes;
- Mnemis returns traceable episode IDs;
- at least one complex scenario improves over similarity-only retrieval;
- timeout and fallback tests pass;
- no unsupported fact controls an action.

---

## 7. Offline and synchronization evaluation

Required scenarios:

- full offline session;
- refresh recovery;
- server restart;
- duplicate batch upload;
- out-of-order upload;
- late event after summary;
- old known content version;
- unknown content version;
- parallel device branches;
- pending event retention after failure.

Targets:

```text
offline core-flow completion = 100%
duplicate scoring incidents = 0
restart recovery = 100%
known-version scoring consistency = 100%
unacknowledged-event loss = 0
```

---

## 8. Fairness evaluation

Compare outputs under equivalent educational evidence with simulated slow network, simulated slow device, screen-reader navigation timing, keyboard-only input, incomplete profile information, and different optional language preferences.

Acceptance:

- network or device delay does not lower mastery;
- assistive-technology timing does not lower mastery;
- missing demographic data does not block personalization;
- low evidence lowers confidence, not mastery;
- learner-facing language remains neutral and constructive.

---

## 9. Accessibility evaluation

Required checks:

- keyboard completion of the full core flow;
- visible focus;
- accessible names for controls;
- contrast review;
- 200% zoom;
- reduced motion;
- screen-reader labels for progress and sync state;
- accessible mathematical text;
- touch target size;
- no color-only status.

All core-flow accessibility blockers must be fixed before submission.

---

## 10. Security evaluation

Required tests are defined in `THREAT_MODEL.md`, including cross-learner isolation, prompt injection, memory poisoning, forged sync events, crawler SSRF, XSS, deletion propagation, and optional-service timeout fallback.

Security-critical tests require 100% pass.

---

## 11. Performance evaluation

Measure:

```text
PWA shell size
offline pack size
local answer latency
local policy latency
PostgreSQL tsvector latency
session restoration latency
Mnemis latency and timeout rate
sync throughput
memory usage on low-end simulation
```

Use p50 and p95 where the number of runs permits.

---

## 12. Competition evidence package

The current evidence layout is generated by `evals/run_all.py` and indexed in
`docs/EVIDENCE_PACK.md`:

| Artifact | Purpose |
|---|---|
| `evals/policy/`, `evals/educational/`, `evals/memory/`, `evals/retrieval/`, `evals/offline_sync/` | versioned golden fixtures and evaluation reports |
| `evals/hybrid/golden.jsonl` and `evals/hybrid/REPORT.md` | Hybrid shadow/final-gate fixtures and interpretation |
| `evals/content_audit/REPORT.md` | content-audit contract and interpretation |
| `evals/run_all.py` | reproducible orchestration of tests, evaluations, and report generation |
| `reports/hybrid_eval.json` | 15-case/22-variant controlled Hybrid evaluation, including H8 summary split |
| `reports/hybrid_final_gate.json` | H9 final mode, frozen flags, No-Go decision, rollback profile, and limitations |
| `reports/python_tests.json` | fresh full Python suite count and command |
| `reports/content_audit_eval.json` | published-pack audit: 1799/1799 checks |
| `reports/performance_eval.json` | local policy, PostgreSQL retrieval, restore, throughput, and memory gates |
| `reports/web_tests.json` | Node/PWA core-flow test result |
| `reports/final_summary.md` | generated cross-layer summary with labels and limitations |
| `docs/EVIDENCE_PACK.md` | human-readable evidence index and reproduction commands |

The supporting commands are the scripts referenced by the evidence pack, notably
`scripts/run_hybrid_ablation.py`, `scripts/run_hybrid_final_gate.py`,
`scripts/run_content_audit.py`, `scripts/run_performance_evals.py`, and
`node --test web/tests/*.test.js`. The final summary must distinguish measured
results from design targets and must not turn controlled or synthetic evidence
into a real student outcome claim.
