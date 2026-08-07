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
  PendingEventQueue,
  OfflineSyncClient,
  resumeOrSync,
  RETRY_SCHEDULE_MS,
  nextRetryDelayMs,
} = core;

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
