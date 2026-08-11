"use strict";

// Offline client tests for the BridgeSAT PWA (SYNC_PROTOCOL.md).
// Runs under `node --test` with no dependencies: offline-core.js is
// storage-injected and transport-injected by design.
//
// Coverage (EVALUATION_SPEC section 7, plan section 11):
//   - full offline session: every event queued, batched, acknowledged;
//   - refresh recovery: in-flight records restore to pending and re-sync;
//   - weak network: retryable failures back off, non-retryable fail fast;
//   - batch cap at 100 events per upload;
//   - version-bound answer evaluation and bounded temporary mastery;
//   - envelope integrity hashes (cross-checked against the Python
//     canonical-json/hash output).

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const core = require("../offline-core.js");
const {
  buildEnvelope,
  evaluateAnswer,
  updateTemporaryMastery,
  pickNextQuestion,
  localAgentDecision,
  agentEventToView,
  createActiveSessionSnapshot,
  restoreActiveSessionSnapshot,
  pickDiagnosticQuestion,
  weakestSkill,
  minutesRemaining,
  isSessionEndingAction,
  selectRelevantAgentEvent,
  PendingEventQueue,
  OfflineSyncClient,
  resumeOrSync,
  RETRY_SCHEDULE_MS,
  nextRetryDelayMs,
  latestPackVersion,
  summarizePackCatalog,
} = core;

test("latest content pack uses semantic version order", () => {
  assert.equal(latestPackVersion(["0.2.0", "0.10.0", "0.1.9"]), "0.10.0");
  assert.equal(latestPackVersion([]), null);
});

test("homepage catalog summary is derived from pack content", () => {
  const summary = summarizePackCatalog({
    items: [
      { target_skill: "inequalities", content_type: "question" },
      { target_skill: "quadratic_equations", content_type: "question" },
    ],
    lessons: [{ target_skill: "inequalities", content_type: "worked_example" }],
  });
  assert.equal(summary.questionCount, 2);
  assert.equal(summary.lessonCount, 1);
  assert.deepEqual(summary.skills.map((skill) => skill.id), [
    "inequalities",
    "quadratic_equations",
  ]);
  assert.equal(summary.skills[0].label, "Inequalities");
});

test("micro-lesson decisions expose a renderable teaching asset", () => {
  const view = agentEventToView({
    action: "SHOW_MICRO_LESSON",
    action_payload: { skill: "inequalities" },
    reason_code: "REPEATED_SKILL_ERRORS",
    reason_text: "Review the rule before continuing.",
    policy_version: "policy-v1",
  });
  assert.equal(view.title, "Micro lesson");
  assert.equal(view.showTeachingAsset, true);
  assert.equal(view.showWorkedExample, false);
});

test("session time budget decreases before it reaches the policy", () => {
  const startedAt = Date.parse("2026-08-10T10:00:00Z");
  const now = Date.parse("2026-08-10T10:08:30Z");
  assert.equal(minutesRemaining(10, startedAt, now), 2);
  assert.equal(minutesRemaining(10, startedAt, startedAt + 20 * 60_000), 0);
});

test("offline policy closes with review when the time budget is nearly exhausted", () => {
  const decision = localAgentDecision({
    skill: "linear_equations",
    misconception: "sign_error",
    observationCount: 2,
    validatedEpisodes: [],
    minutesRemaining: 2,
  });
  assert.equal(decision.action, "END_WITH_REVIEW");
  assert.equal(decision.reason_code, "TIME_BUDGET_EXHAUSTED");
  assert.equal(isSessionEndingAction(decision), true);
  assert.equal(isSessionEndingAction({ action: "RETRY_SAME_SKILL" }), false);
});

test("a late server response cannot replace the current answer decision", () => {
  const events = [
    { source_event_id: "answer_old", action: "RETRY_SAME_SKILL" },
    { source_event_id: "answer_current", action: "SHOW_WORKED_EXAMPLE" },
  ];
  assert.equal(
    selectRelevantAgentEvent(events, "answer_current").action,
    "SHOW_WORKED_EXAMPLE"
  );
  assert.equal(selectRelevantAgentEvent(events, "answer_future"), null);
});

// ---------------------------------------------------------------------------
// In-memory store with the same shape as web/offline.js OfflineStore.
// ---------------------------------------------------------------------------

class MemoryStore {
  constructor() {
    this.data = {};
  }
  async get(storeName, id) {
    return (this.data[storeName] || {})[id] ?? null;
  }
  async put(storeName, record) {
    (this.data[storeName] = this.data[storeName] || {})[record.id] = record;
  }
  async delete(storeName, id) {
    delete (this.data[storeName] || {})[id];
  }
  async all(storeName) {
    return Object.values(this.data[storeName] || {});
  }
}

function transportThat(fn) {
  return async (url, options) => {
    const result = await fn(url, options);
    return { ok: true, status: 200, json: async () => result };
  };
}

const ENVELOPE = () =>
  buildEnvelope({
    studentId: "student_01",
    sessionId: "session_01",
    sessionBranchId: "branch_demo_device",
    deviceId: "device_a",
    deviceSequence: 1,
    eventType: "ANSWER_SUBMITTED",
    payload: {
      question_id: "math.linear_equations.001",
      question_version: 1,
      selected_choice_id: "A",
      hint_level: 0,
      attempt_id: "att_1",
    },
    contentPackVersion: "0.1.0",
  });

// ---------------------------------------------------------------------------
// Full offline session
// ---------------------------------------------------------------------------

test("full offline session: all events sync and are acknowledged", async () => {
  const store = new MemoryStore();
  const client = new OfflineSyncClient({
    store,
    deviceId: "device_a",
    studentId: "student_01",
    transport: transportThat(async (_url, _options) => {
      return { accepted_event_ids: ["e1", "e2", "e3", "e4"], rejected_events: [] };
    }),
  });
  const events = ["e1", "e2", "e3", "e4"];
  for (const id of events) {
    await client.queue.enqueue({ ...ENVELOPE(), event_id: id });
  }
  const result = await client.sync();
  assert.equal(result.synced, true);
  const pending = await store.all("pending_events");
  const acknowledged = await store.all("acknowledged_events");
  assert.equal(pending.length, 0);
  assert.deepEqual(acknowledged.map((r) => r.id).sort(), events);
});

test("a server-reported duplicate is acknowledged after a lost response", async () => {
  const store = new MemoryStore();
  const client = new OfflineSyncClient({
    store,
    deviceId: "device_a",
    studentId: "student_01",
    transport: transportThat(async () => ({
      accepted_event_ids: [],
      duplicate_event_ids: ["e1"],
      rejected_events: [],
    })),
  });
  await client.queue.enqueue({ ...ENVELOPE(), event_id: "e1" });

  const result = await client.sync();

  assert.equal(result.synced, true);
  assert.equal((await store.all("pending_events")).length, 0);
  assert.deepEqual(
    (await store.all("acknowledged_events")).map((row) => row.id),
    ["e1"]
  );
});

test("concurrent sync calls cannot let a later device sequence overtake", async () => {
  const store = new MemoryStore();
  const batches = [];
  const completedSequences = [];
  let releaseFirst;
  const firstBlocked = new Promise((resolve) => {
    releaseFirst = resolve;
  });
  let transportCalls = 0;
  const client = new OfflineSyncClient({
    store,
    deviceId: "device_a",
    studentId: "student_01",
    transport: transportThat(async (_url, options) => {
      const events = JSON.parse(options.body).events;
      batches.push(events.map((event) => event.device_sequence));
      transportCalls += 1;
      if (transportCalls === 1) await firstBlocked;
      completedSequences.push(events[0].device_sequence);
      return {
        accepted_event_ids: events.map((event) => event.event_id),
        duplicate_event_ids: [],
        rejected_events: [],
      };
    }),
  });
  await client.queue.enqueue({ ...ENVELOPE(), event_id: "e1", device_sequence: 1 });
  const first = client.sync();
  await new Promise((resolve) => setImmediate(resolve));
  await client.queue.enqueue({ ...ENVELOPE(), event_id: "e2", device_sequence: 2 });
  const second = client.sync();
  await new Promise((resolve) => setImmediate(resolve));
  releaseFirst();

  await Promise.all([first, second]);

  assert.deepEqual(batches, [[1], [2]]);
  assert.deepEqual(completedSequences, [1, 2]);
  assert.equal((await store.all("pending_events")).length, 0);
});

test("empty queue reports empty without transport call", async () => {
  let calls = 0;
  const client = new OfflineSyncClient({
    store: new MemoryStore(),
    deviceId: "d",
    studentId: "s",
    transport: async () => {
      calls += 1;
      throw new Error("must not be called");
    },
  });
  const result = await client.sync();
  assert.equal(result.synced, false);
  assert.equal(result.reason, "empty");
  assert.equal(calls, 0);
});

// ---------------------------------------------------------------------------
// Refresh recovery
// ---------------------------------------------------------------------------

test("refresh recovery: in-flight records restore to pending and re-sync", async () => {
  const store = new MemoryStore();
  const client = new OfflineSyncClient({
    store,
    deviceId: "device_a",
    studentId: "student_01",
    transport: transportThat(async () => {
      return { accepted_event_ids: ["e1", "e2"], rejected_events: [] };
    }),
  });
  await client.queue.enqueue({ ...ENVELOPE(), event_id: "e1" });
  await client.queue.enqueue({ ...ENVELOPE(), event_id: "e2" });

  await client.queue.dequeue();
  const inFlight = await store.all("pending_events");
  assert.equal(inFlight.every((r) => r.status === "in_flight"), true);

  const recovered = new OfflineSyncClient({
    store,
    deviceId: "device_a",
    studentId: "student_01",
    transport: transportThat(async () => {
      return { accepted_event_ids: ["e1", "e2"], rejected_events: [] };
    }),
  });
  await recovered.queue.restore();
  const restored = await store.all("pending_events");
  assert.equal(restored.every((r) => r.status === "pending"), true);

  const result = await recovered.sync();
  assert.equal(result.synced, true);
  assert.equal((await store.all("pending_events")).length, 0);
});

test("resumeOrSync restores, pulls snapshot, and syncs", async () => {
  const store = new MemoryStore();
  const calls = [];
  const client = new OfflineSyncClient({
    store,
    deviceId: "device_a",
    studentId: "student_01",
    transport: async (url) => {
      calls.push(url);
      if (url.includes("/snapshot")) {
        return { ok: true, status: 200, json: async () => ({ snapshot_version: 7 }) };
      }
      return { ok: true, status: 200, json: async () => ({ accepted_event_ids: [], rejected_events: [] }) };
    },
  });
  await client.queue.enqueue({ ...ENVELOPE(), event_id: "e1" });
  const { snapshot, syncResult } = await resumeOrSync({ store, syncClient: client });
  assert.equal(snapshot.snapshot_version, 7);
  assert.equal(syncResult.synced, true);
  assert.ok(calls.some((url) => url.includes("/snapshot")));
  assert.ok(calls.some((url) => url.includes("/sync/events")));
});

// ---------------------------------------------------------------------------
// Weak network and retry policy
// ---------------------------------------------------------------------------

test("weak network: retryable HTTP failure backs off and keeps the event", async () => {
  const store = new MemoryStore();
  const client = new OfflineSyncClient({
    store,
    deviceId: "device_a",
    studentId: "student_01",
    transport: async () => ({ ok: false, status: 503, json: async () => ({}) }),
  });
  await client.queue.enqueue({ ...ENVELOPE(), event_id: "e1" });
  const result = await client.sync();
  assert.equal(result.synced, false);
  assert.equal(result.status, 503);
  const [record] = await store.all("pending_events");
  assert.equal(record.status, "pending");
  assert.equal(record.attempts, 1);
  assert.ok(record.next_retry_at > Date.now());
});

test("dropped connection returns queued events to retryable state", async () => {
  const store = new MemoryStore();
  const client = new OfflineSyncClient({
    store,
    deviceId: "device_a",
    studentId: "student_01",
    transport: async () => {
      throw new TypeError("network disconnected");
    },
  });
  await client.queue.enqueue({ ...ENVELOPE(), event_id: "e_network_drop" });
  const result = await client.sync();
  assert.equal(result.synced, false);
  assert.equal(result.reason, "network");
  const [record] = await store.all("pending_events");
  assert.equal(record.status, "pending");
  assert.equal(record.attempts, 1);
});

test("non-retryable rejection marks the event failed without backoff", async () => {
  const store = new MemoryStore();
  const client = new OfflineSyncClient({
    store,
    deviceId: "device_a",
    studentId: "student_01",
    transport: async () => ({
      ok: false,
      status: 401,
      json: async () => ({ detail: "device not registered" }),
    }),
  });
  await client.queue.enqueue({ ...ENVELOPE(), event_id: "e1" });
  await client.sync();
  const [record] = await store.all("pending_events");
  assert.equal(record.status, "failed");
  assert.equal(record.failure_code, "HTTP_401");
});

test("server-side rejection codes map to queue states", async () => {
  const store = new MemoryStore();
  const client = new OfflineSyncClient({
    store,
    deviceId: "device_a",
    studentId: "student_01",
    transport: transportThat(async () => ({
      accepted_event_ids: ["e1"],
      rejected_events: [
        { event_id: "e2", code: "QUESTION_VERSION_UNKNOWN", retryable: false },
        { event_id: "e3", code: "MISSING_DEPENDENCY", retryable: true },
      ],
    })),
  });
  for (const id of ["e1", "e2", "e3"]) {
    await client.queue.enqueue({ ...ENVELOPE(), event_id: id });
  }
  await client.sync();
  const records = (await store.all("pending_events")).sort((a, b) =>
    a.id.localeCompare(b.id)
  );
  assert.equal(records.length, 2);
  assert.equal(records[0].status, "failed");
  assert.equal(records[0].failure_code, "QUESTION_VERSION_UNKNOWN");
  assert.equal(records[1].status, "pending");
  assert.ok(records[1].attempts >= 1);
  assert.equal((await store.all("acknowledged_events")).length, 1);
});

test("retry schedule follows the protocol sequence", () => {
  assert.deepEqual(RETRY_SCHEDULE_MS, [0, 5000, 15000, 60000, 300000, 900000]);
  assert.equal(nextRetryDelayMs(0), 0);
  assert.equal(nextRetryDelayMs(1), 5000);
  assert.equal(nextRetryDelayMs(5), 900000);
  assert.equal(nextRetryDelayMs(99), 900000);
});

// ---------------------------------------------------------------------------
// Batch bounds
// ---------------------------------------------------------------------------

test("dequeue batches at most 100 events", async () => {
  const store = new MemoryStore();
  const queue = new PendingEventQueue(store);
  for (let i = 0; i < 150; i++) {
    await queue.enqueue({ ...ENVELOPE(), event_id: `e${i}` });
  }
  const batch = await queue.dequeue();
  assert.equal(batch.length, 100);
});

test("dequeue preserves device sequence even when records arrive out of order", async () => {
  const store = new MemoryStore();
  const queue = new PendingEventQueue(store);
  for (const sequence of [3, 1, 2]) {
    await queue.enqueue({
      ...ENVELOPE(),
      event_id: `event_${sequence}`,
      device_sequence: sequence,
    });
  }

  const batch = await queue.dequeue();

  assert.deepEqual(
    batch.map((event) => event.device_sequence),
    [1, 2, 3]
  );
});

// ---------------------------------------------------------------------------
// Version-bound evaluation and mastery
// ---------------------------------------------------------------------------

const ITEM = {
  id: "math.linear_equations.001",
  content_type: "question",
  answer_choice_id: "A",
  target_skill: "linear_equations",
  difficulty: 2,
  choices: [
    { id: "A", text: "11" },
    { id: "B", text: "-11" },
    { id: "C", text: "704" },
    { id: "D", text: "12" },
  ],
};

test("evaluateAnswer is version-bound and exact", () => {
  assert.equal(evaluateAnswer(ITEM, "A").correct, true);
  assert.equal(evaluateAnswer(ITEM, "B").correct, false);
  assert.equal(evaluateAnswer(ITEM, "Z").correct, false);
});

test("updateTemporaryMastery bounds and weights by hint and difficulty", () => {
  const plain = updateTemporaryMastery({}, { correct: true, difficulty: 2, hintLevel: 0 });
  assert.equal(plain.evidence_count, 1);
  assert.ok(plain.mastery > 0.5);

  const hinted = updateTemporaryMastery({}, { correct: true, difficulty: 2, hintLevel: 3 });
  assert.ok(hinted.mastery < plain.mastery);

  const hard = updateTemporaryMastery({}, { correct: true, difficulty: 3, hintLevel: 0 });
  assert.ok(hard.mastery > plain.mastery);

  const repeated = updateTemporaryMastery({}, { correct: true, difficulty: 2, hintLevel: 0, repeated: true });
  assert.ok(repeated.mastery < plain.mastery);
  assert.equal(repeated.confidence, 0.125);
});

test("pickNextQuestion prefers the weakest skill, unanswered, within difficulty", () => {
  const pack = [
    ITEM,
    { ...ITEM, id: "q2", target_skill: "ratios_percentages", difficulty: 3 },
    { ...ITEM, id: "q3", target_skill: "ratios_percentages", difficulty: 1 },
  ];
  const states = { linear_equations: { mastery: 0.8 }, ratios_percentages: { mastery: 0.3 } };
  const picked = pickNextQuestion(pack, states, new Set([ITEM.id]), 2);
  assert.equal(picked.id, "q3");
});

test("pickNextQuestion is deterministic for the same learner state", () => {
  const pack = [
    { ...ITEM, id: "linear-easy", difficulty: 1 },
    { ...ITEM, id: "linear-hard", difficulty: 2 },
  ];
  const originalRandom = Math.random;
  Math.random = () => 0.99;
  try {
    assert.equal(pickNextQuestion(pack, {}, new Set()).id, "linear-easy");
  } finally {
    Math.random = originalRandom;
  }
});

test("bounded action skill and difficulty constrain the next question", () => {
  const items = [
    { id: "weak.easy", content_type: "question", target_skill: "weak", difficulty: 1 },
    { id: "target.easy", content_type: "question", target_skill: "target", difficulty: 1 },
    { id: "target.hard", content_type: "question", target_skill: "target", difficulty: 3 },
  ];
  const states = { weak: { mastery: 0.1 }, target: { mastery: 0.8 } };

  const selected = pickNextQuestion(items, states, new Set(), 3, {
    skill: "target",
    difficulty: 3,
  });

  assert.equal(selected.id, "target.hard");
});

test("worked-example constraint selects the declared transfer item", () => {
  const items = [
    {
      id: "trigger",
      content_type: "question",
      target_skill: "inequalities",
      difficulty: 1,
      author_metadata: { transfer_group: "ineq-sign", instruction_role: "trigger" },
    },
    {
      id: "ordinary",
      content_type: "question",
      target_skill: "inequalities",
      difficulty: 1,
      author_metadata: { transfer_group: "other", instruction_role: "practice" },
    },
    {
      id: "transfer",
      content_type: "question",
      target_skill: "inequalities",
      difficulty: 2,
      author_metadata: { transfer_group: "ineq-sign", instruction_role: "transfer" },
    },
  ];

  const selected = pickNextQuestion(
    items,
    { inequalities: { mastery: 0.2 } },
    new Set(["trigger"]),
    3,
    { skill: "inequalities", transfer_group: "ineq-sign", instruction_role: "transfer" }
  );

  assert.equal(selected.id, "transfer");
});

test("memory changes the first-error offline intervention students see", () => {
  const baseline = localAgentDecision({
    skill: "linear_equations",
    misconception: "sign_error",
    observationCount: 1,
    validatedEpisodes: [],
  });
  assert.equal(baseline.action, "RETRY_SAME_SKILL");
  assert.equal(baseline.reason_code, "MISCONCEPTION_OBSERVED");

  const recalled = localAgentDecision({
    skill: "linear_equations",
    misconception: "sign_error",
    observationCount: 1,
    validatedEpisodes: [
      {
        episode_id: "ep_success_1",
        skill: "linear_equations",
        misconception: "sign_error",
        intervention: "SHOW_WORKED_EXAMPLE",
      },
    ],
  });
  assert.equal(recalled.action, "SHOW_WORKED_EXAMPLE");
  assert.equal(recalled.reason_code, "RECALLED_SUCCESSFUL_EPISODE");
  assert.deepEqual(recalled.episode_ids, ["ep_success_1"]);

  const view = agentEventToView(recalled);
  assert.equal(view.showWorkedExample, true);
  assert.equal(view.memoryBased, true);
  assert.equal(view.memoryBanner, "Based on what helped you before");
  assert.match(view.why, /similar misconception/i);
  assert.match(view.why, /different item/i);
  assert.equal(view.episodeLabel, "Episode ep_success_1");
});

test("agent event view keeps deterministic copy and passes through verified personalized explanation", () => {
  const without = agentEventToView({
    action: "SHOW_WORKED_EXAMPLE",
    reason_code: "REPEATED_MISCONCEPTION",
    reason_text: "Repeated errors map to the same misconception.",
    policy_version: "policy-0.1.0",
    episode_ids: [],
  });
  assert.equal(without.personalized, "");
  assert.equal(without.personalizedEmphasis, "");
  assert.equal(without.why, "Repeated errors map to the same misconception.");

  const withPersonalized = agentEventToView({
    action: "SHOW_WORKED_EXAMPLE",
    reason_code: "REPEATED_MISCONCEPTION",
    reason_text: "Repeated errors map to the same misconception.",
    personalized_explanation:
      "Because 3 sign error mistakes were recorded this session, review the pattern before more practice.",
    personalized_emphasis: "process",
    policy_version: "policy-0.1.0",
    episode_ids: [],
  });
  assert.equal(withPersonalized.personalized,
    "Because 3 sign error mistakes were recorded this session, review the pattern before more practice.");
  assert.equal(withPersonalized.personalizedEmphasis, "process");
  assert.equal(withPersonalized.why, "Repeated errors map to the same misconception.");
});

test("active learning session survives a browser refresh", () => {
  const stored = createActiveSessionSnapshot({
    sessionId: "sess_keep",
    branchId: "branch_keep",
    currentQuestionId: "math.linear_equations.002",
    answeredIds: new Set(["math.linear_equations.001"]),
    hintLevel: 2,
    skillStates: { linear_equations: { mastery: 0.4, evidence_count: 1 } },
    misconceptionCounts: { "linear_equations:sign_error": 1 },
    stage: "question_active",
    phase: "practice",
    nextActionConstraint: { skill: "linear_equations", difficulty: 1 },
  });
  const restored = restoreActiveSessionSnapshot(stored);
  assert.equal(restored.sessionId, "sess_keep");
  assert.equal(restored.currentQuestionId, "math.linear_equations.002");
  assert.deepEqual([...restored.answeredIds], ["math.linear_equations.001"]);
  assert.equal(restored.hintLevel, 2);
  assert.equal(restored.skillStates.linear_equations.mastery, 0.4);
  assert.equal(restored.misconceptionCounts["linear_equations:sign_error"], 1);
  assert.equal(restored.stage, "question_active");
  assert.deepEqual(restored.nextActionConstraint, {
    skill: "linear_equations",
    difficulty: 1,
  });
});

test("short diagnostic samples distinct skills and identifies the weakest", () => {
  const items = [
    ITEM,
    { ...ITEM, id: "ratio-1", target_skill: "ratios_percentages" },
    { ...ITEM, id: "linear-2", target_skill: "linear_equations" },
  ];
  assert.equal(pickDiagnosticQuestion(items, new Set()).id, ITEM.id);
  assert.equal(
    pickDiagnosticQuestion(items, new Set(["linear_equations"])).id,
    "ratio-1"
  );
  assert.equal(
    weakestSkill({
      linear_equations: { mastery: 0.4 },
      ratios_percentages: { mastery: 0.6 },
    }),
    "linear_equations"
  );
});

// ---------------------------------------------------------------------------
// Envelope integrity (cross-checked against the Python implementation)
// ---------------------------------------------------------------------------

test("integrity hash matches the Python canonical hash for the same payload", () => {
  const envelope = buildEnvelope({
    studentId: "student_01",
    sessionId: "session_01",
    sessionBranchId: "branch_demo_device",
    deviceId: "device_a",
    deviceSequence: 1,
    eventType: "ANSWER_SUBMITTED",
    payload: { a: 1, b: ["x", "y"], c: null },
    contentPackVersion: "0.1.0",
  });
  const pythonDigest = "66967ecd2c0d594cae47610fde369386adea443bc8ba246f458d43108c19de9a";
  const expected = "sha256:" + pythonDigest;
  assert.equal(envelope.integrity_hash, expected);
});

test("offline policy version is pinned", () => {
  assert.equal(ENVELOPE().policy_version, "offline-policy-v1");
});
