"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const {
  sha256,
  canonicalJson,
  integrityHash,
  buildEnvelope,
  evaluateAnswer,
  updateTemporaryMastery,
  pickNextQuestion,
  nextRetryDelayMs,
  PendingEventQueue,
  OfflineSyncClient,
  OFFLINE_POLICY_VERSION,
} = require("../../web/offline-core.js");

function inMemoryStore() {
  const data = {
    pending_events: new Map(),
    acknowledged_events: new Map(),
    memory_snapshot: new Map(),
  };
  return {
    async get(storeName, id) {
      const record = data[storeName] && data[storeName].get(id);
      return record ? { ...record } : undefined;
    },
    async put(storeName, record) {
      if (!data[storeName]) data[storeName] = new Map();
      data[storeName].set(record.id, { ...record });
    },
    async delete(storeName, id) {
      if (data[storeName]) data[storeName].delete(id);
    },
    async all(storeName) {
      return [...data[storeName].values()].map((record) => ({ ...record }));
    },
    raw(storeName) {
      return data[storeName];
    },
  };
}

function packItems() {
  return [
    {
      id: "sync.linear.001",
      version: 1,
      content_type: "question",
      target_skill: "linear_equations",
      difficulty: 2,
      choices: [
        { id: "A", text: "11" },
        { id: "B", text: "-11" },
      ],
      answer_choice_id: "A",
      misconception_map: { B: "sign_error" },
      hints: [
        { level: 1, text: "Add 1 to both sides." },
        { level: 2, text: "8x = 88." },
        { level: 3, text: "Divide by 8." },
      ],
    },
    {
      id: "sync.ratios.001",
      version: 2,
      content_type: "question",
      target_skill: "ratios_percentages",
      difficulty: 1,
      choices: [
        { id: "A", text: "80" },
        { id: "B", text: "40" },
      ],
      answer_choice_id: "A",
      misconception_map: { B: "ratio_inversion" },
      hints: [{ level: 1, text: "Parts per minute." }],
    },
  ];
}

test("sha256 matches known vectors", () => {
  assert.equal(sha256(""), "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
  assert.equal(sha256("abc"), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  assert.equal(sha256("hello"), "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824");
});

test("canonicalJson sorts keys and matches python separators", () => {
  assert.equal(
    canonicalJson({ b: 1, a: [2, { z: 3, y: 4 }] }),
    '{"a":[2,{"y":4,"z":3}],"b":1}'
  );
});

test("integrityHash produces server-compatible prefix", () => {
  const hash = integrityHash("ANSWER_SUBMITTED", { question_id: "x.001", version: 1 });
  assert.ok(hash.startsWith("sha256:"));
  assert.equal(hash.length, "sha256:".length + 64);
});

test("buildEnvelope sets all protocol fields", () => {
  const envelope = buildEnvelope({
    studentId: "stu_1",
    sessionId: "sess_1",
    sessionBranchId: "sess_1",
    deviceId: "dev_1",
    deviceSequence: 1,
    eventType: "ANSWER_SUBMITTED",
    payload: { a: 1 },
    contentPackVersion: "0.1.0",
    questionId: "q.1",
    questionVersion: 2,
  });
  assert.equal(envelope.student_id, "stu_1");
  assert.equal(envelope.session_id, "sess_1");
  assert.equal(envelope.session_branch_id, "sess_1");
  assert.equal(envelope.device_sequence, 1);
  assert.equal(envelope.question_id, "q.1");
  assert.equal(envelope.question_version, 2);
  assert.equal(envelope.policy_version, OFFLINE_POLICY_VERSION);
  assert.ok(envelope.event_id.startsWith("evt_"));
  assert.ok(envelope.integrity_hash.startsWith("sha256:"));
});

test("evaluateAnswer marks correct and wrong", () => {
  const [item] = packItems();
  assert.equal(evaluateAnswer(item, "A").correct, true);
  assert.equal(evaluateAnswer(item, "B").correct, false);
  assert.equal(evaluateAnswer(item, "Z").correct, false);
});

test("updateTemporaryMastery applies difficulty and hint weights", () => {
  const base = { alpha: 2, beta: 2, evidence_count: 0 };
  const d2h0 = updateTemporaryMastery(base, {
    correct: true,
    difficulty: 2,
    hintLevel: 0,
    repeated: false,
  });
  assert.equal(d2h0.alpha, 2 + 1.0);
  assert.equal(d2h0.mastery, 3 / 5);

  const d3h1 = updateTemporaryMastery(base, {
    correct: false,
    difficulty: 3,
    hintLevel: 1,
    repeated: false,
  });
  assert.equal(d3h1.beta, 2 + 1.25 * 0.8);
});

test("updateTemporaryMastery applies same-item repeat discount", () => {
  const base = { alpha: 2, beta: 2, evidence_count: 0 };
  const repeated = updateTemporaryMastery(base, {
    correct: true,
    difficulty: 2,
    hintLevel: 0,
    repeated: true,
  });
  assert.equal(repeated.alpha, 2 + 0.35);
});

test("pickNextQuestion prefers weakest skill and skips answered", () => {
  const items = packItems();
  const skillStates = {
    linear_equations: { mastery: 0.9 },
    ratios_percentages: { mastery: 0.2 },
  };
  const choice = pickNextQuestion(items, skillStates, new Set());
  assert.equal(choice.id, "sync.ratios.001");

  const answered = new Set(["sync.ratios.001"]);
  const next = pickNextQuestion(items, skillStates, answered);
  assert.equal(next.id, "sync.linear.001");

  assert.equal(pickNextQuestion(items, skillStates, new Set(["sync.ratios.001", "sync.linear.001"])), null);
});

test("pickNextQuestion works with empty skill states (fresh student)", () => {
  const items = packItems();
  const choice = pickNextQuestion(items, {}, new Set());
  assert.ok(choice);
  assert.ok(["sync.linear.001", "sync.ratios.001"].includes(choice.id));
});

test("retry schedule matches SYNC_PROTOCOL", () => {
  assert.equal(nextRetryDelayMs(0), 0);
  assert.equal(nextRetryDelayMs(1), 5000);
  assert.equal(nextRetryDelayMs(2), 15000);
  assert.equal(nextRetryDelayMs(3), 60000);
  assert.equal(nextRetryDelayMs(4), 300000);
  assert.equal(nextRetryDelayMs(5), 900000);
  assert.equal(nextRetryDelayMs(99), 900000);
});

test("PendingEventQueue enqueues, dequeues eagerly, acks on success", async () => {
  const store = inMemoryStore();
  const queue = new PendingEventQueue(store);
  const env = buildEnvelope({ studentId: "s", sessionId: "ss", sessionBranchId: "ss", deviceId: "d", deviceSequence: 1, eventType: "CONTENT_PRESENTED", payload: {}, contentPackVersion: "0.1.0" });
  await queue.enqueue(env);
  assert.equal((await queue.all()).length, 1);

  const batched = await queue.dequeue();
  assert.equal(batched.length, 1);
  assert.equal(batched[0].event_id, env.event_id);

  await queue.markAcknowledged(env.event_id);
  assert.equal((await queue.all()).length, 0);
  assert.ok(await store.get("acknowledged_events", env.event_id));
});

test("PendingEventQueue retryable rejects back off; permanent fails", async () => {
  const store = inMemoryStore();
  const queue = new PendingEventQueue(store);
  const env = buildEnvelope({ studentId: "s", sessionId: "ss", sessionBranchId: "ss", deviceId: "d", deviceSequence: 1, eventType: "ANSWER_SUBMITTED", payload: {}, contentPackVersion: "0.1.0" });
  await queue.enqueue(env);
  await queue.dequeue();

  await queue.markRejected(env.event_id, "MISSING_DEPENDENCY", true);
  const pending = (await queue.all())[0];
  assert.equal(pending.status, "pending");
  assert.equal(pending.attempts, 1);
  assert.ok(pending.next_retry_at > Date.now() - 1);

  await queue.markRejected("nonexistent", "X", true);
  assert.equal((await queue.all()).length, 1);
});

test("OfflineSyncClient acknowledges accepted events", async () => {
  const store = inMemoryStore();
  const client = new OfflineSyncClient({
    store,
    transport: async () => ({ ok: true, status: 200, json: () => ({ accepted_event_ids: ["evt_1", "evt_2"], duplicate_event_ids: [], rejected_events: [], new_snapshot_version: 2, new_server_cursor: "cursor_2" }) }),
    deviceId: "d",
    studentId: "s",
  });
  const e1 = buildEnvelope({ studentId: "s", sessionId: "ss", sessionBranchId: "ss", deviceId: "d", deviceSequence: 1, eventType: "CONTENT_PRESENTED", payload: {}, contentPackVersion: "0.1.0" });
  e1.event_id = "evt_1";
  const e2 = buildEnvelope({ studentId: "s", sessionId: "ss", sessionBranchId: "ss", deviceId: "d", deviceSequence: 2, eventType: "CONTENT_PRESENTED", payload: {}, contentPackVersion: "0.1.0" });
  e2.event_id = "evt_2";
  await client.queue.enqueue(e1);
  await client.queue.enqueue(e2);

  const result = await client.sync();
  assert.equal(result.synced, true);
  assert.equal(result.body.accepted_event_ids.length, 2);
  assert.equal((await client.queue.all()).length, 0);
  assert.equal(pendingInStore(store), 0);
});

function pendingInStore(store) {
  return [...store.raw("pending_events").values()].length;
}

test("OfflineSyncClient schedules retry on HTTP failure", async () => {
  const store = inMemoryStore();
  let calls = 0;
  const client = new OfflineSyncClient({
    store,
    transport: async () => {
      calls += 1;
      return { ok: false, status: 500, json: async () => ({}) };
    },
    deviceId: "d",
    studentId: "s",
  });
  const env = buildEnvelope({ studentId: "s", sessionId: "ss", sessionBranchId: "ss", deviceId: "d", deviceSequence: 1, eventType: "CONTENT_PRESENTED", payload: {}, contentPackVersion: "0.1.0" });
  await client.queue.enqueue(env);

  const res = await client.sync();
  assert.equal(res.synced, false);
  assert.equal(res.reason, "http");
  assert.equal(res.status, 500);

  const [pending] = await client.queue.all();
  assert.equal(pending.status, "pending");
  assert.equal(pending.attempts, 1);
  assert.ok(pending.next_retry_at > Date.now());
  assert.equal(calls, 1);
});

test("OfflineSyncClient permanently fails on auth rejection", async () => {
  const store = inMemoryStore();
  const client = new OfflineSyncClient({
    store,
    transport: async () => ({ ok: false, status: 403, json: async () => ({ detail: "Device dev not registered" }) }),
    deviceId: "d",
    studentId: "s",
  });
  const env = buildEnvelope({ studentId: "s", sessionId: "ss", sessionBranchId: "ss", deviceId: "d", deviceSequence: 1, eventType: "CONTENT_PRESENTED", payload: {}, contentPackVersion: "0.1.0" });
  await client.queue.enqueue(env);

  const res = await client.sync();
  assert.equal(res.synced, false);
  assert.equal(res.reason, "http");

  const [pending] = await client.queue.all();
  assert.equal(pending.status, "failed");
  assert.equal(pending.failure_code, "HTTP_403");
});