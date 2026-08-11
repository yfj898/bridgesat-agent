# BridgeSAT Hybrid Reasoning Integration Plan

Status: implementation-ready plan; no production Hybrid runtime is claimed by this document.
Audit date: 2026-08-11
Scope: current working tree, including uncommitted Content Expansion changes.
Authority: PostgreSQL remains the only authoritative learner, event, memory, sync, and content state.

## 1. Executive Summary

BridgeSAT already has model transport, model-backed decision experiments, and an optional NVIDIA-backed derived memory index, but those capabilities do **not** currently participate in the Student PWA competition path. The real PWA path is deterministic: the browser scores locally, queues `ANSWER_SUBMITTED`, the server re-scores from the versioned approved content pack, updates PostgreSQL projections and memory, calls `decide_next_action()`, persists an `AgentEvent`, and returns it through sync.

The immediate problem is therefore not “add an LLM.” It is to establish one verified decision boundary around the real `SyncService` path. The recommended target is:

> Deterministic truth and policy guards define what happened and which actions are safe. A model is called only when semantic reasoning can distinguish multiple safe choices. Its structured proposal is grounded against current PostgreSQL episodes and approved content, then accepted or replaced by the existing deterministic fallback.

The first implementation work should be `PolicyConstraints + ReasoningGate + ProposalVerifier`, initially dark-launched with no behavior change. The first student-visible model feature should be a grounded, optional “Why this recommendation?” explanation. Multi-evidence memory/intervention ranking comes next and is enabled only if a controlled ablation beats deterministic policy without degrading latency or fallback reliability. The existing single-episode Session 2 competition proof remains deterministic and must not depend on model availability.

### Student experience principle

The learner should not be required to diagnose their own misconception, choose a teaching strategy, or explain their reasoning in order to receive personalization. The default product loop is:

```text
answer naturally
  → BridgeSAT observes validated learning behavior
  → BridgeSAT selects and verifies the teaching response
  → the learner receives the lesson/example/hint
  → transfer behavior provides the next piece of evidence
```

Hybrid reasoning therefore consumes evidence that already arises from normal learning: selected distractor, reviewed misconception mapping, correctness, hint use, streaks, mastery/confidence, session time, validated episodes, transfer outcomes, intervention effectiveness, and content metadata. The model's job is to reason about **how to teach now**, not to ask the learner to perform a meta-diagnostic task first.

Optional learner-authored reflection may remain a future research feature, but it is explicitly outside the competition main path and is not required for any Hybrid benefit described in this plan.

## 2. Current-State Architecture

### 2.1 Verified repository state

The current `bridgesat-math-0.3.0` manifest reports:

- 8 skills;
- 103 questions;
- 24 lessons: 12 worked examples and 12 micro lessons;
- 17 used misconception labels;
- `review_provenance.mode = simulated_competition_review`;
- `review_provenance.human_approved = false`.

There are 15 PostgreSQL migrations, `0001` through `0015`. The pack, event store, learner state, episodic memory, sync, and `tsvector` retrieval are PostgreSQL-backed. Nothing in this plan changes that boundary.

Audit verification performed in this review:

- model-focused Python tests: 38 passed, 0 failed;
- all Web tests: 43 passed, 0 failed;
- the existing current evidence report records 567 Python tests, but this review did not rerun the full PostgreSQL suite;
- no production files were modified by the audit.

### 2.2 Student PWA real path

The actual answer-to-AgentEvent chain is:

```text
web/app.js:submitAnswer()
  ├─ offline-core.js:evaluateAnswer()                  local exact scoring
  ├─ offline-core.js:updateTemporaryMastery()          temporary local projection
  ├─ item.misconception_map[selectedChoiceId]          reviewed mapping
  ├─ offline-core.js:localAgentDecision()               immediate offline action
  ├─ enqueueEvent("ANSWER_SUBMITTED")                  IndexedDB pending queue
  └─ app.js:attemptSync()
       ↓
offline-core.js:OfflineSyncClient.sync()
       POST /v1/sync/events
       ↓
app/sync/router.py:sync_events()
       ↓
app/sync/service.py:SyncService.process_batch()
  ├─ integrity, device sequence, dependency and idempotency checks
  ├─ _apply_event()
  └─ _apply_answer_submitted()
       ├─ VersionedAnswerKey.evaluate()                 server exact re-score
       ├─ learner mastery projection update             PostgreSQL truth
       ├─ reviewed distractor → misconception evidence
       ├─ EpisodeBuilder.complete_runtime_candidate()
       ├─ PGMemory.record_intervention_outcome()        immediate window only
       └─ _decide_and_record_agent_event()
            ├─ PGMemory.recall_episodes()
            ├─ build PolicyInput
            ├─ decide_next_action()                    current authority
            ├─ VersionedAnswerKey.teaching_asset_meta()
            ├─ EventStore.append_agent_event()
            └─ _transition_session()
       ↓
SyncResponse.server_events + memory_snapshot
       ↓
web/app.js:attemptSync()
  ├─ consumeAgentEvents()
  ├─ selectRelevantAgentEvent(source event ID)
  └─ renderAgentIntervention()
```

Every arrow above is wired. `web/tests/runtime-wiring.test.js` statically asserts server-event consumption, source-event matching, transfer constraints, memory banner, evidence disclosure, and content-governance labels. `tests/test_pg_sync.py::test_runtime_sync_builds_episode_and_memory_changes_next_session_action` exercises the public sync path and proves the two-session memory action difference without an LLM.

The local action is shown immediately. A server response replaces it only while its `source_event_id` is still the current answer. A late response for an older answer is ignored by `selectRelevantAgentEvent()`, which is the correct basis for Hybrid reconciliation.

Primary code anchors used for this call graph:

| Boundary | Evidence anchor |
| --- | --- |
| local answer, misconception, policy and queue | `web/app.js:396-477` |
| sync result and snapshot consumption | `web/app.js:229-270` |
| HTTP sync client and snapshot persistence | `web/offline-core.js:585-645` |
| public sync route | `app/sync/router.py:64-74` |
| batch lock/transaction and response | `app/sync/service.py:293-502` |
| answer truth/state/episode/stat processing | `app/sync/service.py:817-1090` |
| authoritative next-action execution | `app/sync/service.py:1092-1250` |
| deterministic policy | `app/agent/policy.py:44-246` |
| secondary Orchestrator model proposal | `app/agent/orchestrator.py:250-294` |
| version-bound scoring/content selection | `app/sync/versioned_scoring.py:62-135` |

### 2.3 `/v1/adapt` path

```text
POST /v1/adapt
  → app/main.py:adapt()
  → _adapt_mastery()
  → app/engine.py:adapt(..., llm_client=_get_llm_client())
  → app/engine.py:_adapt_with_llm()
```

This is an authenticated live API, but `web/app.js` and `web/offline-core.js` do not call it. It uses a separate lowercase action vocabulary, its own mastery update, and its own model prompt. It does not represent the competition PWA authority and must not be presented as proof that the PWA is Hybrid.

Recommendation: keep it compatible during H0–H4, mark it secondary/legacy in code and API docs, then route its decision portion through the shared Hybrid gateway if clients still require it. Do not delete it before usage and compatibility are measured. Do not let it remain a second authoritative learner-state writer long-term.

### 2.4 `SessionOrchestrator` path

`SessionOrchestrator.evaluate_answer()` can call `_decide_with_llm()`, but production code does not instantiate or call `SessionOrchestrator`. Current call sites are tests and older direct orchestration scenarios. Its LLM path lists every `BoundedAction`, parses `{action, reason}`, checks only enum membership, and maps the chosen action to a state. It does not calculate state-specific allowed actions or ground episodes/content.

The older direct golden test remains useful as component evidence, but the public sync golden test is the product proof. The class should be retained until compatibility tests migrate; it should eventually delegate decisions to the same policy/Hybrid gateway rather than own another prompt.

### 2.5 LLM-backed memory path

```text
validated PostgreSQL episode/fact
  → Transactional Outbox
  → optional build_mnemis_index()
  → NvidiaMemoryIndex / Mnemis adapter
       ├─ LLM summary
       └─ LLM rerank
```

`NvidiaMemoryIndex` is a derived, optional index. Candidate IDs originate from PostgreSQL records, and fallback code re-scopes enhanced results against the current student's validated PostgreSQL episodes. This preserves the authority boundary.

However:

- `SyncService` constructs `PGMemory` directly and never calls `NvidiaMemoryIndex`, `MnemisStudentMemory`, or `FallbackStudentMemory`;
- `MemoryProvider.recall_episodes()` also returns PostgreSQL recall directly;
- enhanced memory is used by outbox/index tests, ablation scripts, and optional infrastructure, not the PWA action path;
- the current NVIDIA rerank prompt sees ID, skill, misconception, and summary, but not full outcome, intervention effectiveness, confidence, recency, difficulty band, or `InterventionStat` support;
- reranked evidence therefore does not currently change the PWA decision.

## 3. Existing LLM Capabilities

| Capability | Implemented | Used by PWA main path | Verification today | Assessment |
| --- | --- | --- | --- | --- |
| NVIDIA-compatible completion transport | Yes, `LLMClient` | No direct call | timeout/status/JSON transport tests | Reusable transport, not a Hybrid system |
| LLM choice in `SessionOrchestrator` | Yes | No | unit tests only | Experimental/secondary path |
| LLM choice in `/v1/adapt` | Yes | No | API/unit tests | Live but non-authoritative path |
| NVIDIA memory summary/rerank | Yes | No | backend and ablation tests | Optional derived layer |
| Deterministic memory-aware intervention | Yes | Yes | public sync two-session test | Current product authority |
| Personalized model explanation | No | No | none | Recommended first visible feature |
| Behavioral pedagogical reasoning | Partial evidence already exists | No model use in main path | deterministic state/memory tests | Recommended after grounding foundation |
| Model session summary | No | No | none | Useful after grounding foundation |
| Proposal grounding verifier | No | No | enum-only checks in old paths | P0 prerequisite |
| State-specific allowed-actions contract | No | No | broad enum only | P0 prerequisite |

`LLMClient` currently sends one concatenated user prompt and provides connection error mapping. It does not itself declare task, prompt version, input allowlist, response schema, or task-specific timeout. Those controls belong in a Hybrid gateway; `LLMClient` should remain transport-only.

## 4. Gaps / Dead Paths / Duplication

### 4.1 Confirmed hypotheses

1. **Confirmed:** `SyncService` calls `decide_next_action()` directly. `SessionOrchestrator._decide_with_llm()` is not on the PWA path.
2. **Confirmed:** `/v1/adapt` may use an LLM, but the PWA never calls `/v1/adapt`.
3. **Confirmed:** both existing LLM decision paths validate membership in a broad action list, not `allowed_actions_for_current_state`. A syntactically valid but semantically illegal action can be accepted in those paths.
4. **Confirmed:** the PWA policy reduces recalled episode evidence to `recalled_successful_episode: bool` plus IDs. It does not reason over outcome, confidence, effectiveness, intervention comparison, recency, or `InterventionStat`.

### 4.2 Implemented but not used

- SessionOrchestrator model decision;
- NVIDIA/Mnemis reranking for real next-action decisions;
- `FallbackStudentMemory` in the production sync factory;
- short-term and delayed `InterventionStat` updates in runtime; the real transfer flow currently writes only the `immediate` window;
- rich strategy-memory stats returned to the PWA are not inputs to server policy.

### 4.3 Duplicated decision/state paths

- `app/engine.py` owns legacy adaptation and its own LLM vocabulary;
- `app/agent/orchestrator.py` owns a second LLM prompt and state mapping;
- `app/sync/service.py` owns the actual competition decision execution;
- `web/offline-core.js` necessarily owns an offline-compatible deterministic subset.

The browser subset is intentional. The three server paths are not. Server policy logic should converge on one constraints/fallback API while keeping API wrappers backward-compatible.

### 4.4 Missing verification and audit data

- no allowed-action verification;
- no episode/content grounding verification;
- no model proposal or rejection audit on `AgentEvent`;
- no structured claim references for model rationale;
- no task-specific latency budget;
- no model prompt-injection test on a real model boundary; current security tests exercise deterministic paths;
- no protection against holding the student advisory lock and PostgreSQL transaction for the full default 8-second model timeout;
- `SyncService._transition_session()` updates the state without calling `can_transition()`. Current deterministic policy makes the target predictable, but a future model path must validate transition legality before persistence.
- `PGMemory.list_episodes_for_fact()` parses `skill + misconception + intervention` from the fact key but calls `recall_episodes()` without filtering the intervention. Consequently, fact promotion/support IDs can include successful episodes for a different intervention. This must receive a regression fix before any fact/stat signal is exposed to a model as intervention-specific evidence.

### 4.5 Documentation drift found during audit

- `docs/PEDAGOGY_SPEC.md` still describes the older four-skill/55-item pack;
- `docs/ONE_PAGE_WRITEUP.md` and `docs/SUBMISSION_READINESS.md` retain an older `889/889` content-audit number while current evidence reports `1799/1799`;
- `docs/MEMORY_CONSISTENCY.md` contains stale SQLite deletion/metric names;
- late sections of `docs/ARCHITECTURE.md` mix historical planned state with current PostgreSQL implementation.

These are documentation tasks for H10. Historical material may remain only if labeled `Historical / superseded`.

## 5. Recommended LLM Use Cases

| Use case | Current capability | Does LLM add value? | Main risk / offline effect | Verification | Priority |
| --- | --- | --- | --- | --- | --- |
| Multiple-choice correctness | Exact pack key exists | No | hallucinated answer; breaks offline parity | exact version-bound key | P3: prohibit |
| Mastery numeric update | Weighted deterministic update exists | No | unstable learner state | deterministic projection tests | P3: prohibit |
| Session transition | Deterministic state machine exists | No direct control | illegal state | `can_transition()` | P3: prohibit |
| Reviewed distractor classification | High-confidence mapping exists | No override needed | contradicts governed content | exact mapping | P3: deterministic truth |
| Behavioral pedagogical reasoning over normal learner actions | State/memory evidence exists, but policy compresses it heavily | Yes when several safe teaching moves/evidence patterns compete | overfitting small samples; latency | scoped evidence + allowed actions + verifier + ablation | P0/P1, H6-H7 |
| Intervention ranking | Policy gives one result today | Yes only among multiple legal/evidence-supported actions | latency and overfitting | allowed set + outcome evidence + ablation | P0 after verifier |
| Multiple episode ranking | PG exact recall exists | Yes when candidates conflict | hallucinated/wrong-tenant episode | rehydrate candidates from PG | P0 for ambiguous cases |
| Teaching asset ranking | Pack supports multiple assets mainly for four older skills; new skills have one/type | Limited now | unnecessary latency | approved/current-pack/matching metadata | P2 now; future P1 |
| Personalized explanation | Fixed UI copy exists | Strong visible value with low control risk | invented history | structured claims + verified evidence refs | P0 |
| Hint rewrite/emphasis | Three approved hints exist | Some phrasing value | math mutation | protect hint meaning/math spans | P2 |
| Extra dynamic hint generation | Not governed | Not before production review | unreviewed instructional content | full content review required | P3 for competition runtime |
| Session summary | Deterministic summary exists | Yes for accessible synthesis | inflated claims | structured fact allowlist | P1 |
| Content authoring wording | Deterministic generators exist | Yes at authoring time | duplicated/incorrect/unlicensed draft | symbolic answer validation + formal review gate | P1 authoring-only |

### Current useful vs future useful

Immediately useful model work is grounded explanation, ambiguous multi-episode/multi-intervention reasoning from existing behavioral evidence, automatic teaching-emphasis selection, and grounded summaries. Content ranking has limited current value: each new skill has one worked example and one micro lesson, and older skills have multiple assets but weak metadata for distinguishing pedagogical context. It becomes valuable only after future content expansion adds meaningfully different approved assets and selection metadata.

The competition path should **not** add a mandatory “What were you thinking?” or strategy-choice step. Such a prompt increases learner effort and shifts diagnosis onto the student. BridgeSAT should infer from the normal learning trace first; if optional reflection is ever added later, it must be supplemental rather than required for personalization.

The current asset distribution confirms that limitation: `inequalities`, `quadratic_equations`, `exponents_radicals`, and `coordinate_geometry` each have one worked example and one micro lesson; each of the four older skills has two of each. More than one ID is therefore available in part of the pack, but that does not yet mean the alternatives encode distinct teaching strategies suitable for semantic ranking.

### Authoring-time use

`app/content_pipeline/generation.py` and `app/content_pipeline/expansion.py` currently produce seeded/deterministic content structures: symbolic answer truth, option identities, misconception mappings, transfer roles, and lesson records are defined before publication. They are not a runtime LLM content generator.

A future authoring-only model step may use:

```text
deterministic symbolic skeleton
  → LLM wording/context diversification draft
  → deterministic answer/distractor/schema/hash/duplicate validation
  → formal review records
  → approved pack publication
```

The model may vary story context or prose, but cannot choose the answer truth, silently change misconception semantics, create lineage/license facts, or promote a draft. All output remains `candidate/draft` until it passes the existing validation and review gates. A `sim.*` review remains a simulated competition fixture and never becomes human approval.

## 6. Non-LLM Responsibilities

The following remain deterministic and authoritative:

- answer correctness and option validity;
- content-pack/version binding and content hashes;
- weighted mastery and confidence updates;
- misconception truth from reviewed distractor mappings;
- event integrity, device sequence, idempotency, tenant scope, and deletion state;
- session state legality and hard time guards;
- episode validation and transfer-success calculation;
- PostgreSQL learner/event/memory/content writes;
- approved-content and license gate;
- policy fallback;
- offline local scoring, policy, queue, restore, and sync;
- technical evidence facts.

The model receives no database connection, tool execution, secrets, unrestricted rows, or write capability. It proposes; verified deterministic code executes.

## 7. Target Hybrid Architecture

```text
Student answer/event
  ↓
Deterministic Truth Layer
  exact scoring • event integrity • version binding • mastery projection
  ↓
Misconception Evidence
  reviewed mapping = high confidence
  repeated behavior = supporting evidence, not a replacement for reviewed truth
  ↓
Authoritative Evidence Retrieval
  PostgreSQL episodes + supported intervention stats + approved content
  [optional Mnemis/NVIDIA candidate expansion under timeout]
  ↓
derive_policy_constraints(PolicyInput, evidence)
  ├─ hard_action
  ├─ allowed_actions
  ├─ deterministic_fallback
  └─ policy reasons
  ↓
ReasoningGate
  ├─ deterministic fast path
  └─ semantic ambiguity path
       ↓
  HybridDecisionContext (minimal, scoped, structured)
       ↓
  LLMClient transport
       ↓
  HybridDecisionProposal
       ↓
  ProposalVerifier
       ├─ accept
       └─ reject/timeout → deterministic fallback
  ↓
Approved content selection + legal state mapping
  ↓
AgentEvent + sanitized decision trace
  ↓
SyncResponse → PWA
```

Recommended server ownership:

```text
app/agent/policy.py
    policy constraints and deterministic fallback

app/agent/hybrid.py
    gate, context assembly, model proposal, verifier orchestration

app/agent/hybrid_contracts.py
    strict input/proposal/verified models

app/agent/llm_client.py
    transport only

app/sync/service.py
    authoritative event execution and Hybrid gateway integration
```

`SessionOrchestrator` and `/v1/adapt` become adapters to the shared gateway; they do not keep independent prompts. `web/offline-core.js` stays a bounded deterministic parity subset.

## 8. Reasoning Gate

The gate must answer “can semantic reasoning materially improve this safe choice?” rather than “is a model configured?”

```python
def choose_mode(constraints, context, availability):
    if constraints.hard_action is not None:
        return DETERMINISTIC
    if context.offline or not availability.configured:
        return DETERMINISTIC
    if availability.circuit_open or availability.budget_exhausted:
        return DETERMINISTIC
    if not semantic_reasoning_needed(context, constraints):
        return DETERMINISTIC
    return HYBRID
```

### 8.1 Fast deterministic path

Do not call a model when any of these is true:

- browser is offline, no key is configured, circuit is open, or prior call timed out;
- `minutes_remaining <= 2` requires `END_WITH_REVIEW`;
- a prerequisite guard mandates `SWITCH_TO_PREREQUISITE`;
- current state allows only one action;
- a single exact validated successful episode triggers the existing competition behavior;
- first high-confidence mapped misconception has no conflicting semantic evidence and current policy says retry;
- answer/state/content truth is being calculated;
- selected action/content has already been shown or the user advanced;
- model budget or task-specific timeout would be exceeded.

Keeping single-episode recall deterministic is deliberate: it preserves the core two-session demo even when NVIDIA is unavailable.

### 8.2 Hybrid reasoning path

Call the model only when at least one audited condition is present:

- two or more legal interventions are defensible;
- two or more scoped validated episodes have conflicting relevance/outcomes;
- supported `InterventionStat` evidence disagrees with the most recent episode;
- several behavioral signals support different safe teaching responses and deterministic rules do not have a uniquely preferred action;
- grounded personalized wording or session synthesis is requested.

Each task has a separate prompt version, schema, timeout, and fallback. Explanation generation must not implicitly reopen action selection.

### 8.3 Why not every interaction

Most inputs such as `mastery=0.4, consecutive_errors=2` are already sufficient for a transparent rule. Calling a model makes that rule slower and less reproducible without adding semantic information. Sparse gating preserves offline parity, lowers free-tier instability and cost, avoids lock amplification, and makes the model-value ablation meaningful.

### 8.4 Task-specific gates

Do not implement one global `reasoning_needed` Boolean for every model feature. Share provider availability/circuit-breaker logic, but keep independent gates because the same learner state can justify different model tasks:

```text
DecisionReasoningGate
  → only ambiguous policy-approved action choices

ExplanationGate
  → may run after a deterministic single-episode action because wording can add value

SummaryGate
  → runs only at session close over validated facts
```

For example, one exact successful recalled episode keeps the action deterministic, so `DecisionReasoningGate = false`, while `ExplanationGate = true` may personalize why that already-verified action is being reused. This prevents explanation generation from accidentally reopening action selection.

## 9. Policy Constraints / Allowed Actions Design

Introduce one backward-compatible derivation API:

```python
class PolicyConstraints(BaseModel):
    hard_action: BoundedAction | None = None
    allowed_actions: tuple[BoundedAction, ...]
    preferred_fallback: AgentDecision
    next_states: dict[BoundedAction, SessionState]
    reasons: tuple[str, ...]
    policy_version: str

def derive_policy_constraints(inputs: PolicyInput, evidence: PolicyEvidence) -> PolicyConstraints:
    ...

def decide_next_action(inputs: PolicyInput) -> PolicyResult:
    constraints = derive_policy_constraints(inputs, PolicyEvidence.empty())
    return constraints.preferred_fallback_as_result()
```

This keeps current callers and tests intact while moving policy logic into one source. Do not implement a second allowed-action ruleset in `hybrid.py`.

Examples:

| Situation | Hard/allowed | Existing fallback | Model? |
| --- | --- | --- | --- |
| <=2 minutes | hard `END_WITH_REVIEW` | same | Never |
| missing prerequisite | hard `SWITCH_TO_PREREQUISITE` | same | Never |
| one exact successful recalled episode | hard `SHOW_WORKED_EXAMPLE` for MVP proof | same | Explanation only |
| first mapped misconception, no competing evidence | hard/current `RETRY_SAME_SKILL` | same | Never |
| repeated misconception, two supported interventions | allowed retry/worked/micro | current worked or micro | Yes |
| multiple relevant episodes with conflicting outcomes | allowed evidence-supported subset | current deterministic choice | Yes |
| no misconception and stable mastery | allowed current difficulty/raise/lower as policy permits | current policy | Usually no |

Verifier checks both `proposal.action in allowed_actions` and `can_transition(current_state, next_states[action])`. A broad `BoundedAction` membership check is insufficient.

## 10. Hybrid Contracts

Contracts should be strict Pydantic models with `extra="forbid"`, bounded strings/lists, and no raw arbitrary database objects.

```python
class RecalledEpisodeEvidence(BaseModel):
    episode_id: str
    skill: str
    misconception: str | None
    intervention: BoundedAction
    outcome_correct: bool
    different_item: bool
    effectiveness: float
    confidence: float
    status: Literal["validated"]
    recency_bucket: Literal["recent", "medium", "older"]
    teaching_content_id: str | None
    difficulty_band: str | None  # derived from content/stat; not currently on Episode

class InterventionEvidence(BaseModel):
    intervention: BoundedAction
    difficulty_band: str
    immediate_attempts: int
    short_term_attempts: int
    delayed_attempts: int
    blended_effectiveness: float | None
    support: Literal["insufficient", "supported"]

class ContentCandidate(BaseModel):
    content_id: str
    content_type: Literal["worked_example", "micro_lesson"]
    skill: str
    misconceptions: tuple[str, ...]
    pack_version: str
    content_hash: str
    review_status: Literal["approved"]
    human_approved: bool

class HybridDecisionContext(BaseModel):
    task: Literal["intervention_ranking"]
    context_version: str
    skill: str
    subskill: str | None
    difficulty: int
    mastery: float
    mastery_confidence: float
    consecutive_errors: int
    correct_streak: int
    active_misconception: str | None
    misconception_evidence_count: int
    misconception_confidence: Literal["low", "medium", "high"]
    hints_used: int
    minutes_remaining: int
    current_state: SessionState
    allowed_actions: tuple[BoundedAction, ...]
    deterministic_fallback: AgentDecision
    recalled_episodes: tuple[RecalledEpisodeEvidence, ...]
    intervention_stats: tuple[InterventionEvidence, ...]
    content_candidates: tuple[ContentCandidate, ...]
```

Do not include a student identifier unless a future task proves it is technically necessary; current ranking/explanation tasks do not need one. Also exclude name/email, raw complete history, unrelated skills, prompt secrets, database tenant configuration, or unbounded free text.

Model return:

```python
class EvidenceClaim(BaseModel):
    claim_code: Literal[
        "SAME_MISCONCEPTION",
        "SUCCESSFUL_TRANSFER",
        "SUPPORTED_INTERVENTION_EFFECT",
        "STUDENT_REASONING_SIGNAL",
    ]
    evidence_refs: tuple[str, ...]

class HybridDecisionProposal(BaseModel):
    proposed_action: BoundedAction
    selected_episode_id: str | None
    selected_content_id: str | None
    rationale_code: str
    rationale: str = Field(max_length=320)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_claims: tuple[EvidenceClaim, ...]

class VerifiedHybridDecision(BaseModel):
    accepted: bool
    final_action: BoundedAction
    model_used: bool
    fallback_used: bool
    fallback_reason: str | None
    verification_checks: tuple[str, ...]
    selected_episode_id: str | None
    selected_content_id: str | None
    safe_student_explanation: str | None
    model_task: str | None
    model_name: str | None
    prompt_version: str | None
    latency_ms: int | None
```

Only the sanitized verified result reaches `AgentEvent`. Raw prompts/responses are not sent to the PWA or normal logs.

## 11. Proposal Verifier

Verification is deterministic and fail-closed. Any failure yields the precomputed fallback; verification failure must never reject the student's answer event.

### 11.1 Action and state

- proposed action is a known `BoundedAction`;
- proposed action is in `constraints.allowed_actions`;
- mapped next state is in the constraint map;
- `can_transition(current_state, next_state)` is true;
- hard action, time guard, prerequisite guard, and session-completed status are unchanged;
- selected action does not contradict offline/reconciliation cutoff.

### 11.2 Episode grounding

- referenced ID occurs in the exact current candidate set;
- rehydrated PostgreSQL row belongs to current tenant/student;
- status is `validated`, not candidate/contradicted/archived/deleted;
- skill/misconception relation matches the context or an explicit safe relation rule;
- if proposal claims success, `outcome.correct`, `outcome.different_item`, confidence, and effectiveness support it;
- evidence event IDs exist and are scoped;
- model may not raise or rewrite stored effectiveness/confidence.

### 11.3 Content grounding

- content ID exists in the current installed pack and PostgreSQL registry;
- content hash equals the manifest/registry hash;
- `review_status == approved`;
- skill and misconception metadata support the action;
- content type matches action;
- license and source lineage fields are complete;
- simulated review remains labeled simulated; `human_approved=false` may not become “human approved.”

### 11.4 Rationale grounding

Prefer structured `claim_code + evidence_refs` over a general claim checker. The student-facing sentence is initially assembled by deterministic templates from verified claims. For example:

```text
claim: SUCCESSFUL_TRANSFER(ep_123)
template: A worked example helped you solve a different item after this
          misconception in an earlier session.
```

Claims such as “worked three times,” “your score improved,” or “this is a permanent weakness” require exact structured evidence and are otherwise rejected. This is safer and smaller than introducing another model as a judge.

### 11.5 Mathematical grounding

- the model never receives authority to change answer keys, formulas, choice text, or core worked-example steps;
- for explanation wording, send immutable math/step spans as IDs/placeholders and only expose approved prose slots;
- returned placeholders must match exactly and in order;
- any missing/changed placeholder rejects model prose and uses deterministic copy;
- do not generate new student-facing teaching content outside the review gate.

## 12. Memory + InterventionStat Reasoning

### 12.1 Actual available fields

`Episode` currently contains: episode/student/session IDs, skill, optional misconception, intervention, outcome JSON, effectiveness, evidence event IDs, summary, confidence, status, and timestamps. Runtime outcome JSON contains correctness, hint level, trigger/teaching/outcome content IDs, `different_item`, and pending-transfer status. Difficulty is not a first-class episode field and must be derived from versioned content or the related stat band.

`InterventionStat` contains skill, optional misconception, intervention, difficulty band, and immediate/short-term/delayed correct, attempts, and weights. `blended_effectiveness()` uses 0.50/0.30/0.20 window weights. The real runtime currently records only `immediate` when a candidate completes; short-term and delayed values are populated only by direct/test callers. They must not be presented as real longitudinal evidence until runtime update rules exist.

The current fact aggregation also needs a narrow correctness fix: `list_episodes_for_fact()` must filter by the intervention encoded in the normalized key. Until that regression is fixed and existing facts are rebuilt from authoritative episodes, semantic facts are not safe evidence for comparing interventions. This does not affect PostgreSQL episode authority or the existing exact-episode demo path.

### 12.2 Candidate pipeline

Recommended pipeline:

```text
PG exact scoped recall
  → deterministic status/skill/misconception/evidence filters
  → optional Mnemis/NVIDIA candidate IDs under timeout
  → rehydrate every ID from PostgreSQL
  → attach supported InterventionStat and versioned content facts
  → cap/top-k deterministically
  → optional model ranking/proposal
  → verifier
```

### 12.3 Option comparison

| Option | Strengths | Weaknesses | Recommendation |
| --- | --- | --- | --- |
| A. `PGMemory` only | authoritative, fast, transparent, already main path | basic ranking, rich evidence unused | Always the base and fallback |
| B. Current `NvidiaMemoryIndex` rerank | existing optional derived layer and tests | not main-wired; prompt omits important evidence; adds summary/rerank latency | Keep for optional candidate expansion/eval, not decision authority |
| C. SyncService builds scoped evidence for shared Hybrid reasoner | no duplicated store; transparent candidates; full episode/stat/content facts | integration and transaction latency must be controlled | Recommended action-reasoning design |

Do not make `NvidiaMemoryIndex` the owner of next-action logic. If it returns candidate IDs, the Hybrid context must rehydrate and verify them from PostgreSQL.

### 12.4 Minimum evidence gate

Do not expose false precision. Recommended rule, aligned with the current pedagogy preference thresholds:

- fewer than 3 relevant attempts: `support=insufficient`; do not rank by numeric effectiveness;
- at least 3 attempts, confidence/support >=0.60, and no active contradiction: expose a rounded effectiveness band rather than excessive decimals;
- require a >=0.15 effectiveness difference before using the stat as a preference claim;
- one successful episode remains valid episode evidence, but is not described as a stable intervention preference;
- contradictory/failed outcomes remain visible to ranking, not discarded.

The first stat implementation must also define and test when short-term and delayed windows are updated. Until then, Hybrid input must label those windows unavailable rather than zero.

## 13. Behavioral Pedagogical Reasoning

The competition product should personalize automatically from normal learner behavior. The student should not have to state a misconception, choose an explanation style, or provide a free-text self-diagnosis before receiving benefit.

### 13.1 Evidence already available without extra learner work

The current runtime already produces enough structured evidence for a useful reasoning layer:

- selected answer and exact correctness;
- reviewed `misconception_map` label for the chosen distractor;
- item skill, subskill, difficulty, and content version;
- hint level;
- consecutive errors and correct streak;
- mastery and mastery confidence;
- misconception observation count and distinct-item support;
- minutes remaining;
- validated recalled episodes;
- prior intervention type, effectiveness, confidence, and transfer outcome;
- `InterventionStat` support when enough observations exist;
- whether the learner succeeded on a distinct transfer item after an intervention.

The model should reason over these verified facts only when the policy permits more than one defensible teaching response. Example:

```text
Current evidence
  sign_error on current item
  mastery 0.44
  first occurrence this session

Prior evidence
  worked example → validated transfer success
  retry → later repeated error

Policy
  allowed: SHOW_WORKED_EXAMPLE, SHOW_MICRO_LESSON, RETRY_SAME_SKILL

Hybrid reasoner
  proposes SHOW_WORKED_EXAMPLE

Verifier
  checks action, episode, outcome, content, transition

Student
  simply receives the worked example and continues learning
```

### 13.2 Teaching emphasis, not learner labeling

The model should produce a **situational teaching decision**, not a permanent characterization of the learner. Prefer statements such as:

```text
Current evidence favors a worked example because the same teaching move was
followed by transfer success for a similar error.
```

Avoid internal or student-facing claims such as:

```text
This student is a sign-error learner.
This student always needs step-by-step teaching.
```

Sparse behavior is evidence about the present teaching context, not a stable personality or ability label.

### 13.3 Optional passive signals

Future evaluation may test low-friction signals that arise during ordinary use, for example answer latency, answer changes, hint timing, time spent on an intervention, or transfer response latency. These signals are **not required for the first Hybrid release** and must not be interpreted as psychological truth. For example, a fast answer does not deterministically mean carelessness.

Any new telemetry must have a clear pedagogical hypothesis, minimal retention, offline-safe semantics, and an ablation showing that it improves decisions beyond the existing answer/hint/memory evidence. Do not add tracking simply to create more model features.

### 13.4 Student interaction rule

No competition-path Hybrid feature may require the learner to answer questions such as:

- “What misconception do you think you made?”
- “Which explanation style do you prefer?”
- “Why did you choose that answer?”

The learner may voluntarily open `Why this recommendation?`, but that is explanatory transparency, not input required to unlock personalization.

Free-text self-explanation may be revisited after the competition as an optional research feature for learners who explicitly want reflection practice. It is not part of the H0–H10 implementation path in this plan.

## 14. Personalized Explanation

This is the preferred first student-visible model feature because it demonstrates semantic personalization without controlling correctness or state.

Input is limited to:

- final verified action and deterministic reason code;
- approved lesson prose slots, never mutable math truth;
- current high/medium-confidence misconception evidence;
- verified selected episode claims;
- a minimal learner-state summary;
- a list of allowed phrasing claims.

Output:

```python
class ExplanationProposal(BaseModel):
    student_explanation: str = Field(max_length=320)
    emphasis: Literal["process", "sign", "setup", "transfer", "review"]
    evidence_refs: tuple[str, ...]
```

Recommended decisions:

- worked-example core steps: **never generate at runtime**;
- fixed hint core meaning: **never change**;
- hint emphasis/rewrite around protected approved content: P2, not first release;
- new analogy: P2/P3 because it becomes new teaching content and needs review;
- recommendation explanation: P0 after grounding verifier.

If the explanation times out, fails schema/grounding, changes placeholders, or cites unavailable evidence, the PWA shows the existing deterministic “Based on what helped you before” and “Why this recommendation?” copy.

## 15. Session Summary

Use the model only as a grounded renderer over validated facts:

```text
skills practiced
questions attempted
misconception evidence with confidence labels
interventions actually shown
validated episodes
distinct transfer outcomes
sync status
  → model summary
  → structured claim verification
  → student-facing text
```

The summary may say what was practiced, what strategy was used, and what the next session will review. It must not claim SAT score gain, permanent weakness, clinical diagnosis, human approval, or real-world educational effect. On failure/offline, keep the current deterministic summary. Summary timeout may be wider because it does not block an answer decision.

## 16. PWA / Technical Evidence Integration

Do not redesign the front end. Extend the existing intervention card and collapsed technical evidence.

Student view:

```text
Based on what helped you before
[grounded personalized sentence, if verified]
```

Collapsed technical evidence:

```text
Decision mode: Hybrid | Deterministic
Model used: Yes | No
Allowed actions: ...
Model proposal: ...
Final action: ...
Selected memory: ep_...
Selected content: ...
Verification: ✓ action ✓ episode ✓ content ✓ claims
Fallback used: No
```

Rejected proposal:

```text
Model proposal rejected
Fallback: deterministic policy
Reason: ungrounded_episode | action_not_allowed | timeout | ...
```

Return only sanitized fields; do not expose prompts, model raw output, student free text, secrets, tenant IDs, or detailed security errors. Preserve existing simulated-review disclosure and default-collapsed behavior.

## 17. Offline / Failure Semantics

The invariant remains:

```text
offline
  → exact local scoring
  → local bounded deterministic policy
  → IndexedDB queue
  → refresh/restore
  → reconnect
  → server re-score/reconcile
  → idempotent sync
```

Hybrid behavior:

- offline, missing key, timeout, malformed response, rate limit, or circuit open always uses deterministic fallback;
- model failure must not reject or roll back an accepted student answer;
- a server Hybrid result may replace local feedback only when its source answer is still current and no subsequent intervention/content-presentation event has occurred;
- after the learner advances, the server may record reconciliation evidence but must not retroactively replace what the learner already saw;
- reconnect does not rewrite a completed offline episode based on a later model opinion;
- policy/content/model/prompt versions are retained in evidence;
- duplicate sync does not produce a second model-driven `AgentEvent` or double-count mastery/stat outcomes.

### Transaction constraint

Today `process_batch()` holds a student advisory lock and PostgreSQL transaction while `_decide_and_record_agent_event()` runs. A synchronous model call there would hold both for its duration. For the competition-scale first release:

- do **not** put shadow reasoning or personalized explanation calls inside the authoritative answer transaction;
- derive only the minimal sanitized evidence/fallback needed for Hybrid while the transaction is active;
- commit the deterministic answer/state/AgentEvent first, then run H4/H5 shadow or explanation work after the long transaction has released its lock;
- cap one post-commit model task per relevant source event and make idempotent source-event lookup win;
- use task-specific timeouts so an explanation failure delays only optional enrichment, never answer acceptance;
- measure total response latency independently from PostgreSQL transaction duration.

Action-changing H7 is different because the proposal can affect persisted session state. If H6 earns a Go, use the bounded two-phase revalidation design described in H7 rather than silently moving an external network call into the existing long transaction. Do not add an asynchronous microservice; keep the design inside the existing application/request boundary. If the two-phase path cannot be made race-safe and simple enough for the competition, action ranking remains disabled and the shipped Hybrid capability is grounded explanation/summary over the deterministic core.

## 18. Security / Prompt Injection / Grounding

- use a fixed system message and structured JSON data; never concatenate student/content text into instructions;
- label every untrusted field and state that it cannot modify rules;
- `extra="forbid"`, bounded arrays/strings, strict enum/confidence parsing;
- no tools, function execution, URLs, retrieval initiated by model, or database writes;
- allowlist context fields and redact PII;
- current tenant/student scope is applied before model input and rechecked after output;
- PostgreSQL IDs are opaque evidence refs, not user-controlled lookup keys;
- candidate cap and payload/token caps prevent context flooding;
- the competition main path contains no required learner-authored prompt text; if any untrusted text enters later, it is serialized strictly as data and cannot modify system rules;
- approved external content is data, never a system instruction;
- raw model responses are excluded from ordinary logs; structured error codes and latency are logged with pseudonymous refs;
- deletion propagation removes authoritative text/evidence first and derived model/memory artifacts through the outbox;
- Mnemis/NVIDIA remains rebuildable and non-authoritative;
- simulated reviewers may never be rewritten as human reviewers by model text.

The real Hybrid adversarial suite must test injected phrases inside episode summaries, lesson prose, content metadata, and any future untrusted learner-authored field. Existing deterministic prompt-injection tests are not sufficient evidence for this new boundary.

## 19. Model / Latency Strategy

Current configuration uses:

- `BRIDGESAT_LLM_MODEL`, default `deepseek-ai/deepseek-v4-flash-0731`;
- `BRIDGESAT_LLM_TIMEOUT_MS`, default 8000 ms;
- NVIDIA-compatible endpoint.

Worklog measurements are historical local observations, not SLAs: the default model had roughly sub-second math responses but decision responses varied around 1–9 seconds; 70B models showed cold/warm latency too high for interactive use; some reasoning models returned `content=None` under small token budgets.

Recommended task budgets to validate, not claim as measured SLAs:

| Task | Preferred deadline | Fallback |
| --- | ---: | --- |
| intervention/memory ranking | <=2 s | deterministic action |
| personalized phrasing | <=3 s | deterministic explanation |
| session summary | <=5 s | deterministic summary |

Use task-specific timeout/token/prompt settings rather than the global 8-second default. Do not put 70B or fragile reasoning models on the answer path. Add a simple in-process failure counter/circuit breaker only if evaluation demonstrates repeated free-tier delays; do not introduce a new service.

## 20. Test Matrix

### 20.1 Deterministic invariants

- no model configured;
- offline flag/path;
- timeout/rate limit/connection failure;
- malformed JSON and missing fields;
- unknown action;
- no memory candidates;
- exactly one recalled successful episode preserves current action;
- time hard guard always wins;
- prerequisite hard guard always wins;
- same source event replay does not rerun/duplicate decision;
- illegal state transition cannot persist;
- current two-session public sync golden test stays unchanged.

### 20.2 Verifier adversarial tests

- action outside allowed set;
- hallucinated episode ID;
- other learner/tenant episode;
- candidate/failed/contradicted episode claimed successful;
- same skill but wrong misconception without relation evidence;
- hallucinated content ID;
- unapproved/withdrawn/stale-pack content;
- wrong skill/wrong misconception/wrong content type lesson;
- content hash mismatch or missing lineage/license;
- invented evidence count or fake transfer success;
- malformed/out-of-range confidence;
- arbitrary tool-like output;
- prompt injection in episode summary, content prose/metadata, and any future untrusted text field;
- model calls simulated review human-approved;
- math placeholder deleted, reordered, or altered.

### 20.3 Behavioral pedagogical reasoning tests

- same reviewed misconception but different mastery/streak histories produce different **allowed reasoning contexts**, not different correctness truth;
- isolated first error does not get over-personalized from weak history;
- repeated error plus validated prior transfer success exposes stronger worked-example evidence;
- supported intervention evidence is ignored when sample support is insufficient;
- current hint use and time budget may change teaching emphasis without rewriting misconception truth;
- model may not infer permanent traits such as “careless,” “weak student,” or “always needs examples” from sparse behavior;
- unavailable/offline behavior remains deterministic;
- any future passive telemetry is absent from the context unless explicitly enabled and validated.

### 20.4 Memory/stat reasoning tests

- multiple same-student episodes;
- same skill but different misconception;
- successful older episode vs failed newer episode;
- insufficient stat sample is labeled/suppressed;
- supported stat preference with >=3 attempts and material gap;
- missing short/delayed windows are not treated as zero failure;
- hallucinated ID rejected;
- optional Mnemis timeout returns PG candidates and acceptable action.

### 20.5 Explanation/summary tests

- every claim ref exists;
- no invented learner history/count;
- no math mutation;
- no SAT-score or real-outcome claim;
- no simulated-to-human review misstatement;
- timeout/schema failure uses deterministic copy;
- summary facts exactly match event snapshot.

### 20.6 PWA tests

- Hybrid technical evidence renders and is collapsed;
- deterministic fallback evidence renders;
- rejected proposal shows safe reason code;
- existing Session 2 memory banner is unchanged;
- explanation omission/failure is graceful;
- old source-event Hybrid response cannot replace current feedback;
- offline answer/refresh/reconnect remains unchanged;
- duplicate sync does not duplicate UI action/stat.

Likely new test files:

```text
tests/test_policy_constraints.py
tests/test_hybrid_reasoning_gate.py
tests/test_hybrid_verifier.py
tests/test_hybrid_sync.py
tests/security/test_hybrid_prompt_injection.py
tests/golden/test_hybrid_memory_ranking.py
evals/hybrid/run.py
evals/hybrid/cases.jsonl
web/tests/hybrid-evidence.test.js
```

## 21. Hybrid Evaluation / Ablation

Evaluate at least:

```text
Deterministic policy
vs Hybrid (same deterministic guards/fallback)
```

Add a static baseline only if it can reuse the same cases without architecture work. Results must be labeled `controlled internal evaluation` and, where learner actions are simulated, `synthetic learner simulation — not real student outcome`.

Metrics:

- intervention selection accuracy against adjudicated expected allowed choices;
- accepted allowed-action violation rate: target 0%;
- accepted hallucinated episode/content rate: target 0%;
- evidence-grounding and rationale-claim accuracy;
- deterministic fallback success under unavailable/malformed/timeout conditions: target 100%;
- model availability degradation and call rate after gate;
- decision p50/p95 latency and transaction duration;
- offline scenario pass rate, unchanged from baseline;
- behavioral ambiguous-case selection accuracy on a reviewed synthetic gold set;
- explanation grounding accuracy and math-placeholder preservation;
- action difference rate and verified beneficial difference rate;
- cost/token counts as operational evidence, not product impact.

### 21.1 Meaningful benchmark cases

1. Worked example succeeded once; micro lesson succeeded several supported times. Both actions are legal. Hybrid must prefer stronger supported evidence or state why not.
2. Three episodes: same misconception successful older, different misconception successful newer, same misconception recent failed. Hybrid must rank semantic relation and outcome, not recency alone.
3. Two learners choose distractors mapping to the same misconception, but one has an isolated first error while the other has repeated errors and a validated prior worked-example transfer success. The misconception truth stays identical; the teaching response may differ only within policy-allowed actions.
4. Multiple plausible actions but <=2 minutes. Hard `END_WITH_REVIEW` wins without model execution.
5. Model hallucinates an attractive episode/content ID. Verifier rejects and fallback executes.
6. Model unavailable/offline. Core interaction, memory recall, transfer, recovery, and sync remain usable.
7. Single exact validated episode. Hybrid action equals deterministic core proof; only wording may differ.
8. Model chooses correct action but invents “three prior successes.” Action may be retained only if independently valid; model rationale is rejected/replaced by deterministic grounded copy.

Go criteria for Hybrid action ranking:

- zero accepted grounding/allowed-action violations in adversarial set;
- 100% fallback execution in availability suite;
- statistically/operationally meaningful accuracy improvement over deterministic baseline on ambiguous cases;
- no regression in two-session golden, offline/sync, idempotency, content gate, or tenant isolation;
- interactive latency and lock-duration thresholds agreed before enablement.

No-Go means keep deterministic actions and ship only grounded explanation/summary if those pass their independent gates.

## 22. Implementation Phases

### H0 — Correctness, source-of-truth, and baseline freeze

**Goal:** freeze the real runtime authority, fix the known intervention-specific memory aggregation bug, and establish a full pre-Hybrid baseline before changing behavior.
**Why now:** Hybrid must not learn from evidence that is already incorrectly aggregated, and later regressions are hard to attribute without a frozen baseline.
**Files likely touched:** `app/memory/pg_memory.py`, regression tests, `docs/ARCHITECTURE.md`, `docs/API_AND_OPERATIONS.md`, `app/main.py` comments/deprecation metadata, characterization tests.
**Contracts:** runtime authority statement; compatibility inventory; intervention-specific fact support invariant; feature flags default off.
**Migration:** no by default. Rebuild derived facts from authoritative episodes if the regression test proves existing aggregates can contain cross-intervention support.
**PWA impact:** none.
**Offline impact:** none.
**Risks:** undocumented external `/v1/adapt` consumers; existing derived facts may need deterministic rebuild.
**Tests:** prove PWA posts only sync; prove SyncService calls shared deterministic policy; add `list_episodes_for_fact()` intervention-filter regression; capture current engine/orchestrator compatibility outputs; run full Python, Web, content audit/import, core evals, two-session golden, and `git diff --check` to freeze the implementation baseline.
**Acceptance:** one documented authority; intervention-specific facts cannot include episodes from another intervention; full baseline numbers are freshly recorded; current public two-session golden passes unchanged.
**Rollback:** revert the narrow memory-read fix only if it contradicts the normalized-key contract; documentation/test changes are independently reversible.
**Competition value:** prevents a demo claim based on unused model code and prevents Hybrid from amplifying contaminated intervention evidence.

### H1 — Policy constraints API

**Goal:** derive hard action, allowed actions, legal next states, and current deterministic fallback from one policy source.
**Why now:** no model proposal is safe without this boundary.
**Files:** `app/agent/policy.py`, `app/domain/sessions.py` usage, `tests/test_policy_constraints.py`, existing policy tests.
**Contracts:** `PolicyConstraints`, `PolicyEvidence`, backward-compatible `decide_next_action()`.
**Migration:** no.
**PWA:** none.
**Offline:** local policy stays unchanged; parity cases documented.
**Risks:** subtly changing existing trajectory order.
**Tests:** table/trajectory tests for every existing branch and legal transition.
**Acceptance:** every current policy test/golden output unchanged; every constraint has a valid fallback; hard guards produce a single action.
**Rollback:** wrapper remains on old implementation until parity is exact.
**Competition value:** makes model initiative demonstrably bounded.

### H2 — Hybrid contracts and Reasoning Gate (dark)

**Goal:** implement strict context/proposal/result contracts and decide when a model could add value, without calling the model in the main path.
**Why now:** separates semantic need from provider availability.
**Files:** new `app/agent/hybrid_contracts.py`, `app/agent/hybrid.py`, `app/config.py` or existing settings, tests.
**Contracts:** Section 10 plus task settings and reason codes.
**Migration:** no.
**PWA:** none.
**Offline:** always deterministic.
**Risks:** gate too broad, context includes excess data.
**Tests:** all gate branches, field allowlist, bounds, serialization, PII absence.
**Acceptance:** deterministic/hybrid eligibility is reproducible; single-episode demo never requires model.
**Rollback:** disable flag.
**Competition value:** sparse, explainable model use rather than “LLM everywhere.”

### H3 — Proposal verifier, adversarial first

**Goal:** reject unsafe, ungrounded, or semantically illegal proposals before any execution.
**Why now:** must precede provider/main-path wiring.
**Files:** `app/agent/hybrid.py`, `app/content_registry.py` or existing registry query surface, `app/domain/sessions.py`, verifier/security tests.
**Contracts:** `verify_proposal(context, constraints, authoritative_evidence)`.
**Migration:** no.
**PWA:** none.
**Offline:** none.
**Risks:** verifier duplicates content/policy rules.
**Tests:** full adversarial matrix in Section 20.2.
**Acceptance:** zero accepted illegal action, hallucinated ID, wrong-tenant evidence, unapproved content, fake claim, or math mutation; every reject returns fallback.
**Rollback:** Hybrid remains disabled.
**Competition value:** supports the claim “every model-driven intervention is verified.”

### H4 — Real-path Hybrid shadow integration

**Goal:** place the Hybrid gateway on the real PWA request path without allowing the model to change the executed teaching action.
**Why now:** the project needs proof that the model is reachable from the actual `/v1/sync/events` product path, but external model latency should not become part of the correctness transaction before value is demonstrated.
**Files:** `app/sync/router.py` request-scoped gateway wiring, `app/sync/service.py` sanitized context extraction, `app/agent/llm_client.py` task timeout support, Hybrid tests.
**Contracts:** post-commit/shadow context; verified shadow proposal; sanitized observation `{fallback_action, model_proposal, accepted, would_change, rejection_reason, latency_ms}`.
**Migration:** no for the first shadow release. Prefer test/eval capture or additive response-only/internal observation before adding persistent trace columns.
**PWA:** no action change. Technical evidence may remain hidden until H5.
**Offline:** deterministic; no queued-event schema break.
**Risks:** accidental model call while the student advisory lock/transaction is still held; duplicate shadow calls on replay; provider latency delaying HTTP response.
**Tests:** assert the authoritative answer/AgentEvent is committed deterministically before any shadow result can affect behavior; timeout/malformed responses leave the returned action unchanged; duplicate sync does not create a second authoritative decision; two-session golden unchanged.
**Acceptance:** real sync traffic can produce a verified shadow proposal after deterministic decision formation, but `model_proposal != execution`; no model failure can roll back an accepted answer; transaction duration remains at deterministic baseline aside from bounded context assembly.
**Rollback:** disable shadow task flag; no database rollback required.
**Competition value:** closes the “model exists but not in real PWA path” gap without prematurely granting action authority.

### H5 — Grounded personalized explanation

**Goal:** add verified student-facing wording to existing “Why this recommendation?” without changing action/math.
**Why now:** highest visible value and lower risk than action ranking.
**Files:** `app/agent/hybrid.py`, explanation templates/contracts, post-commit sync response enrichment, `web/app.js`, `web/offline-core.js` fallback mapping, `web/tests/hybrid-evidence.test.js`.
**Contracts:** `ExplanationProposal`, structured evidence claims, protected placeholders.
**Migration:** no required migration. Persist a trace only if later action-changing Hybrid needs auditable query history.
**PWA:** the learner receives the chosen teaching content directly; one optional personalized sentence appears in the existing explanation surface, with technical evidence collapsed.
**Offline:** existing deterministic copy.
**Risks:** invented history, reading complexity, simulated-review claim drift.
**Tests:** grounding, math immutability, accessibility, timeout fallback.
**Acceptance:** 100% gold explanations grounded; zero protected-span mutations; UI remains useful without model.
**Rollback:** omit personalized text and retain current copy.
**Competition value:** delivers immediate model benefit without requiring the student to diagnose themselves or choose a teaching strategy.

### H6 — Shadow Hybrid ablation and behavioral-value proof

**Goal:** prove that model reasoning over normal learner behavior and multiple evidence sources adds decision quality before it is allowed to alter actions.
**Why now:** `mastery + error_count` alone often makes the model an expensive if/else. The model earns authority only on cases where richer behavioral/memory evidence changes the best safe teaching move.
**Files:** `evals/hybrid/`, Hybrid context/gateway, `app/memory/pg_memory.py` read surfaces, `app/memory/nvidia_backend.py` only if evaluated as candidate expansion, gold cases and reports.
**Contracts:** versioned ambiguous-case fixtures; deterministic result; verified shadow proposal; evidence-support labels.
**Migration:** no.
**PWA:** no action change; H5 explanation can remain enabled independently.
**Offline:** deterministic baseline is the reference behavior.
**Risks:** benchmark cases tailored to the model, small-sample overfit, unsupported `InterventionStat` precision, latency variance.
**Tests/eval:** multiple episodes, competing interventions, isolated vs repeated errors, hint use, time budget, supported vs insufficient stats, hallucinated evidence, model unavailable, single exact episode invariant.
**Acceptance:** zero unsafe acceptance; 100% fallback; a measured improvement over deterministic baseline on adjudicated ambiguous cases; no benefit claim from cases where policy already has one obvious action.
**Rollback:** keep Hybrid in explanation-only mode.
**Competition value:** demonstrates incremental intelligence from automatic evidence reasoning rather than requiring extra learner input.

### H7 — Behavioral multi-evidence action ranking (conditional Go)

**Goal:** when H6 proves value, allow the model to choose among `PolicyConstraints.allowed_actions` using verified behavioral history, episodes, supported stats, and approved content candidates.
**Why now:** this is the point where the model can improve teaching automatically while the student simply answers and receives instruction.
**Files:** `app/sync/service.py`, `app/sync/router.py`, `app/agent/hybrid.py`, `app/domain/events.py`/event persistence only if an auditable decision trace is justified, `app/memory/pg_memory.py`, gold/security/sync tests.
**Contracts:** `HybridDecisionContext`, `HybridDecisionProposal`, `VerifiedHybridDecision`, idempotent decision token/source-event binding, deterministic fallback.
**Migration:** conditional. Add an additive decision trace only if action-changing Hybrid is enabled; do not add schema solely for shadow/explanation mode.
**PWA:** no new learner choice or self-diagnosis UI. The learner receives the verified intervention; `Why this recommendation?` explains it optionally.
**Offline:** deterministic local/PG behavior remains the only offline authority.
**Risks:** external call inside a database transaction, stale proposal after learner advances, small-sample preference overfit, divergence from offline immediate feedback.
**Execution boundary:** do not simply insert an 8-second network call inside the current advisory-lock transaction. Prefer a bounded two-phase design within the same request if action ranking is enabled: (A) commit answer/evidence and derive a deterministic fallback plus decision token; release the long transaction; (B) call model with a strict task timeout; (C) open a short revalidation transaction, verify source event/session state/content have not advanced, then persist the verified action or the already-derived fallback. If this cannot be made simple and race-safe, action-changing Hybrid is No-Go for competition and H5 remains the shipped model capability.
**Tests:** stale token/source event, learner advanced before Phase C, timeout, duplicate sync, state transition legality, fallback persistence, memory/stat matrix, lock-duration regression.
**Acceptance:** H6 Go criteria met; no model call holds the original long sync transaction; zero illegal/stale action persistence; fallback remains 100%; core one-episode demo stays deterministic.
**Rollback:** disable action-ranking task and keep explanation/shadow evidence.
**Competition value:** automatic personalized teaching from observed behavior, with no extra cognitive burden on the learner.

### H8 — Session summary personalization

**Goal:** render a concise grounded summary from validated session facts.
**Why now:** useful demo closure after claims verifier exists.
**Files:** summary contract/gateway, `app/sync/service.py` or snapshot service, `web/app.js`, tests.
**Contracts:** fact allowlist, claim refs, deterministic fallback.
**Migration:** no unless summaries must persist; prefer derived response first.
**PWA:** replaces/augments current deterministic prose only when verified.
**Offline:** current deterministic summary.
**Risks:** overclaiming improvement/permanence.
**Tests:** exact fact grounding, prohibited-claim suite.
**Acceptance:** no unsupported claims; unavailable model has no UX loss.
**Rollback:** deterministic summary.
**Competition value:** clear learner-facing continuity without controlling pedagogy.

### H9 — Final system evaluation and enablement freeze

**Goal:** rerun the complete Hybrid/deterministic evaluation after the actually enabled feature set is known and freeze the competition configuration.
**Why now:** H6 answers whether action ranking deserves a Go; H9 verifies the integrated system after H7/H8 choices, including regressions, latency, explanation grounding, and offline behavior.
**Files:** `evals/hybrid/`, `evals/run_all.py`, `reports/hybrid_eval.json`, evidence docs and final configuration.
**Contracts:** versioned gold cases, deterministic/shadow/enabled-Hybrid runners, honest labels.
**Migration:** no.
**PWA/offline:** includes end-to-end regression scenarios for whichever features are enabled.
**Risks:** synthetic cases reflect implementation rules instead of real ambiguity; optional provider instability can make one run misleading.
**Tests:** Section 21 benchmark cases, repeated availability/latency runs, full two-session/offline/sync/content/security regression, explanation/summary grounding.
**Acceptance:** reproducible final report; zero unsafe acceptance; 100% deterministic fallback; explicit final Go/No-Go for action ranking; no real-student outcome claim; feature flags frozen for demo.
**Rollback:** ship explanation-only or deterministic mode if final action-ranking criteria regress.
**Competition value:** turns “uses AI” into measured incremental capability and records exactly which model authority was earned.

### H10 — Demo and documentation closeout

**Goal:** align claims, setup, demo, and evidence with the actually enabled mode.
**Why now:** stale claims undermine technical credibility.
**Files:** README and docs listed in H0/drift section, `docs/DEMO_SCRIPT.md`, `docs/EVIDENCE_PACK.md`, `docs/SUBMISSION_READINESS.md`, `reports/final_summary.md`.
**Contracts:** one reproducible startup/eval order; Hybrid feature flag and fallback disclosure.
**Migration:** no.
**PWA:** technical evidence demo polish only.
**Offline:** demo must explicitly show deterministic mode.
**Risks:** synthetic evaluation described as learning gain or simulated review as human.
**Tests:** docs grep/checks, full Python/Web/evals/content import/audit rerun.
**Acceptance:** no stale counts/SQLite claims, actual test/eval numbers frozen, three-minute story demonstrates verified main-path behavior.
**Rollback:** remove unenabled Hybrid claim.
**Competition value:** clear, honest final proof.

## 23. Files Expected to Change

| File | Expected change |
| --- | --- |
| `app/agent/policy.py` | derive constraints/fallback without changing current outputs |
| `app/agent/hybrid_contracts.py` | new strict context/proposal/verified contracts |
| `app/agent/hybrid.py` | gate, prompt task adapter, verifier orchestration, fallback |
| `app/agent/llm_client.py` | transport accepts task timeout/messages; no policy logic |
| `app/domain/events.py` | sanitized decision mode/trace fields only if action-changing Hybrid requires persistence |
| `app/domain/sessions.py` | reused legal-transition validation, not model-controlled |
| `app/infrastructure/event_store.py` | persist/read additive Hybrid trace only if H7 action ranking is enabled |
| `app/infrastructure/migrations_pg/0016_hybrid_decision_trace.py` | conditional additive audit fields; not required for shadow/explanation-only mode |
| `app/memory/pg_memory.py` | scoped episode/stat read surface for context |
| `app/memory/nvidia_backend.py` | optional richer candidate contract only if H6 chooses reuse |
| `app/sync/router.py` | inject shared gateway/provider configuration |
| `app/sync/service.py` | authoritative gated call, verify, persist, respond |
| `app/sync/protocol.py` | additive sanitized response fields only where the PWA needs verified Hybrid evidence |
| `app/engine.py` | compatibility delegation/deprecation after shared gateway stable |
| `app/agent/orchestrator.py` | delegate to shared gateway; remove independent prompt later |
| `web/app.js` | render verified explanation/decision trace without adding a required learner-input step |
| `web/offline-core.js` | preserve deterministic fallback and parse additive evidence |
| `web/index.html`, `web/styles.css` | minimal evidence/explanation rows only if needed, accessible/mobile-first |
| `tests/...` and `web/tests/...` | matrices in Section 20 |
| `evals/hybrid/...` | controlled deterministic-vs-Hybrid benchmark |
| README/docs/reports | current authority, limitations, measured evidence only |

Do not modify the content pack to enable runtime-generated lessons or hints. Authoring-time LLM experiments use draft outputs and the existing validation/review/publish pipeline.

## 24. Migration / Compatibility Plan

1. Never edit `0001–0015`; add a migration only when persistent action-changing Hybrid trace is actually enabled.
2. Shadow/explanation mode should prefer response-only or existing extensible payload fields and does not require `0016` by default. If H7 requires persistent trace, `0016` must be additive with deterministic defaults so old AgentEvents and clients remain valid.
3. `SyncResponse.server_events` gains only optional sanitized fields; PWA ignores unknown fields and old servers still render.
4. Feature flags default off: `BRIDGESAT_HYBRID_ENABLED`, plus task-level flags for shadow decision, explanation, action ranking, and summary.
5. Current `decide_next_action()` signature remains supported through H1.
6. `/v1/adapt` response shape remains compatible. It delegates only after characterization and may emit a deprecation header/documented status; do not remove abruptly.
7. `SessionOrchestrator` keeps its public tests until its prompt is replaced by shared gateway delegation.
8. Policy/model/prompt/context versions are explicit; a version change does not rewrite earlier events.
9. Offline envelopes require no Hybrid fields and no new learner-authored reasoning event is required for competition Hybrid behavior.
10. Rollback is flag-based and returns to deterministic policy without database restoration. Derived Mnemis/NVIDIA state remains rebuildable.

Compatibility decision: server-side decision logic should be unified; API entry points need not be physically merged. `SyncService` remains the competition execution authority, while `/v1/adapt` and `SessionOrchestrator` become wrappers around shared constraints/gateway where retained.

## 25. Risks

| Risk | Consequence | Mitigation / fallback |
| --- | --- | --- |
| Simulated content review | Not production/student deployment ready | preserve `human_approved=false`; require educational/answer/license/accessibility human review before production |
| External model latency/free-tier instability | slow answer sync and locks | sparse gate, task timeout, metrics, deterministic fallback, No-Go action ranking |
| Model unavailable or `content=None` | missing proposal | strict transport/schema handling; fallback |
| Prompt injection | instruction hijack or data leak | system/data separation, no tools, field allowlist, adversarial tests |
| Hallucinated evidence | false personalization | PG rehydration, structured refs, fail-closed verifier |
| Small-sample over-personalization | unstable intervention preference | >=3 support gate, effect-gap rule, label uncertainty |
| Missing short/delayed runtime stats | false longitudinal claims | mark unavailable; implement window semantics before use |
| No real student learning-effect evidence | impact overclaim | label all current results controlled/synthetic; do not claim SAT gains |
| Duplicated old decision APIs | inconsistent behavior/claims | shared constraints/gateway; compatibility wrappers; documented authority |
| Offline parity | online action differs unpredictably | hard deterministic core, source-event cutoff, action ranking gated/ablated |
| Model call inside transaction | lock/contention/rollback coupling | shadow/explanation run after deterministic decision; action ranking requires a short revalidation phase or is No-Go |
| Behavioral over-interpretation | sparse actions treated as stable learner traits | reason about current teaching context only; support gates; prohibit permanent-trait language |
| Model-generated teaching text | incorrect/unreviewed instruction | prose-only protected slots; no core steps/hints; authoring uses review pipeline |
| Technical trace leaks internals | privacy/security exposure | sanitized allowlisted trace; raw output server-only or not retained |

Production/student deployment remains blocked until the 127 simulated review records receive real human educational, answer, license, and accessibility review. Competition demo use must say “simulated competition review,” not “human approved.”

## 26. Recommended Final Competition Story

> BridgeSAT is a memory-grounded Hybrid learning agent. Deterministic systems establish what happened, maintain progress, and define which teaching actions are safe. When additional reasoning can add value, the model compares verified evidence from the learner's normal practice history, reasons about which safe teaching move is most appropriate now, and personalizes the explanation. Every model proposal is checked against scoped PostgreSQL memory and approved content before it can affect teaching. Offline, BridgeSAT continues with the same deterministic learning core and syncs safely later.

This story is valid only after H4/H5 are implemented and verified. Before then, the honest story is that BridgeSAT is a deterministic memory-aware agent with optional model experiments outside the main PWA path.

### Explicit exclusions

- no microservice or new agent framework;
- no multi-agent system;
- no LangChain/LangGraph platform introduction;
- no vector database or new frontend framework;
- no LLM database writes, tools, answer scoring, mastery update, or state control;
- no unrestricted event history/PII in prompts;
- no unreviewed generated student content;
- no cloud-only core loop or Mnemis dependency;
- no replacement of PostgreSQL authority or deterministic fallback;
- no giant/reasoning model on every answer;
- no generated fake evidence or human-review claim.

## 27. Final Decisions

1. **Is model capability present but partly absent from the PWA main path?** Yes. LLM decision and NVIDIA memory code exist, but the real PWA decision is deterministic SyncService policy.
2. **Which decision path is the competition runtime authority?** `/v1/sync/events → SyncService._decide_and_record_agent_event() → decide_next_action()`.
3. **Should `engine.py`, `SessionOrchestrator`, and `SyncService` be unified?** Unify their server-side constraints/gateway, not the API entry points. Preserve compatibility and avoid abrupt deletion.
4. **What is the first Hybrid change?** Implement state-specific `PolicyConstraints`, ReasoningGate, and fail-closed ProposalVerifier in dark mode. This is one safety foundation, not a visible feature.
5. **What is the second change?** Put the gateway on the real sync path in shadow/post-commit mode, then expose a grounded personalized “Why this recommendation?” explanation while the executed action remains deterministic.
6. **Should the competition experience ask students for free-text self-diagnosis or teaching-style choices?** No. Personalization should come from normal answer/hint/transfer/memory evidence. Optional reflection can be researched later, but it is not part of H0–H10.
7. **Is memory reranking worth doing now?** Only for multiple/conflicting candidates in a controlled evaluation. Single exact recall should stay deterministic; current NVIDIA rerank is not rich or wired enough to enable directly.
8. **Should personalized explanation precede intervention ranking?** Yes. It has stronger demo visibility, lower control risk, and exercises the same grounding/verifier foundation.
9. **Which features add competition value vs AI decoration?** Automatic multi-evidence memory/intervention reasoning, behavior-aware teaching emphasis, verified personalized explanation, and grounded summary add value. LLM correctness, numeric mastery, state transitions, mandatory learner self-diagnosis, one-rule decisions, generic hint generation, and model calls on every answer are decoration or risk.
10. **What is the strongest BridgeSAT without new architecture?** One PostgreSQL-backed SyncService execution path; deterministic truth/guards/fallback; sparse model reasoning over scoped episodes/stats; verified approved content and claims; transparent PWA evidence; full offline deterministic continuity.

## 28. Recommended First Continuous Implementation Stage

Execute H0–H3 as one bounded stage before any user-visible behavior:

```text
fix intervention-specific memory aggregation
→ freeze the full current baseline
→ characterize current outputs
→ derive PolicyConstraints
→ add strict Hybrid contracts
→ implement ReasoningGate
→ implement adversarial ProposalVerifier
→ dark-mode tests
```

Exit only when current deterministic trajectories and two-session sync golden test are unchanged, every adversarial proposal falls back safely, and the context contains no unnecessary PII. The next stage can then wire H4/H5 into the main path without repeating this architecture audit.
