# BridgeSAT Competition Roadmap

Detailed contracts and acceptance gates are in `IMPLEMENTATION_PLAN.md` and the six normative specifications in `docs/`. This file is the execution checklist. A gate is complete only when its implementation and corresponding acceptance tests pass.

## Gate 0 — frozen design

- [x] Independent project skeleton
- [x] Complete architecture baseline
- [x] Separate educational knowledge from learner memory
- [x] Select SQLite/FTS5 as reliable baseline
- [x] Select Mnemis as advanced long-term-memory retrieval
- [x] Define LightRAG, A-RAG, and RAG-Anything as conditional adapters
- [x] Freeze eight-skill taxonomy and prerequisite graph contract
- [x] Freeze mastery and confidence policy draft
- [x] Freeze memory consistency and Mnemis fallback contract
- [x] Freeze offline synchronization conflict semantics
- [x] Freeze threat model, API, operations, and evaluation contracts

## Gate 1 — event-driven learning loop

- [ ] Database migration system
- [ ] Student, session, attempt, and immutable event tables
- [ ] Versioned learner projections
- [ ] Session state machine
- [ ] Bounded agent action schema
- [ ] Difficulty-aware question selection
- [ ] Three-level hint interaction
- [ ] Misconception taxonomy and distractor mapping
- [ ] Session summary and next-session recommendation
- [ ] Weighted Beta mastery implementation
- [ ] Golden policy trajectory tests

## Gate 2 — cross-session memory proof

- [ ] Learning episode builder
- [ ] Episode validation rules
- [ ] SQLite episodic memory backend
- [ ] Transactional memory outbox
- [ ] Semantic learner facts with confidence and contradiction
- [ ] Intervention-effectiveness aggregates
- [ ] Memory-aware policy decision
- [ ] Two-session demonstration fixture
- [ ] Learner memory correction and deletion
- [ ] Memory/no-memory ablation
- [ ] Mnemis adapter with strict timeout
- [ ] Mnemis Global Selection complex case
- [ ] Mnemis rebuild and parity verification
- [ ] Verified SQLite fallback

## Gate 3 — governed educational retrieval

- [ ] Content registry and source records
- [ ] License gate
- [ ] Content review lifecycle and immutable versions
- [ ] Skill hierarchy and prerequisite expansion
- [ ] FTS5 retrieval backend
- [ ] Citation and license validator
- [ ] Retrieval golden set
- [ ] Optional embedding/reranker evaluation
- [ ] Optional LightRAG adapter only after baseline passes
- [ ] Optional RAG-Anything importer for approved multimodal content

## Gate 4 — offline-first proof

- [ ] Versioned content pack
- [ ] IndexedDB schema
- [ ] Offline objective-answer evaluation
- [ ] Offline bounded adaptation policy
- [ ] Pending-event queue
- [ ] Idempotent event synchronization
- [ ] Parallel branch and late-event handling
- [ ] Version-bound answer scoring
- [ ] Refresh/restart recovery
- [ ] Throttled-network and fully offline tests

## Gate 5 — content, quality, and evidence

- [ ] 8–10 skills and reviewed prerequisite graph
- [ ] 80–120 original or clearly licensed questions
- [ ] 15–25 reviewed micro-lessons/worked examples
- [ ] Three hints per question
- [ ] Misconception mappings
- [ ] 20+ policy scenarios
- [ ] Educational transfer and retention tests
- [ ] Memory retrieval evaluation
- [ ] RAG retrieval evaluation
- [ ] Prompt-injection and memory-poisoning tests
- [ ] Cross-learner isolation tests
- [ ] Accessibility audit
- [ ] Low-end mobile and page-weight measurements
- [ ] Backup and restore test
- [ ] Honest result report

## Gate 6 — submission

- [ ] Clean-install verification
- [ ] Public demo deployment
- [ ] README and data provenance
- [ ] Architecture diagram
- [ ] Screenshots
- [ ] One-page project description
- [ ] Sub-three-minute demo video
- [ ] Final Devpost submission with time buffer
