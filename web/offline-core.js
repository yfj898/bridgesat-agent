"use strict";

// Pure offline client logic for BridgeSAT: deterministic, dependency-free,
// and storage-injected so it can be unit-tested under Node without
// IndexedDB. Mirrors the server-side policies in app/domain/learner.py and
// the sync protocol in docs/SYNC_PROTOCOL.md.

// ---------------------------------------------------------------------------
// Compact SHA-256 (FIPS 180-4) so envelopes can be integrity-hashed in the
// browser and under Node without WebCrypto availability assumptions.
// ---------------------------------------------------------------------------

const K256 = new Int32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
  0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
  0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
  0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
  0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
  0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
  0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
  0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
  0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

function sha256(bytes) {
  const msg = typeof bytes === "string" ? new TextEncoder().encode(bytes) : bytes;
  const bitLen = msg.length * 8;
  const padded = new Uint8Array((((msg.length + 8) >> 6) + 1) * 64);
  padded.set(msg);
  padded[msg.length] = 0x80;
  const view = new DataView(padded.buffer);
  view.setUint32(padded.length - 8, Math.floor(bitLen / 0x100000000), false);
  view.setUint32(padded.length - 4, bitLen >>> 0, false);

  const h = new Int32Array([
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
    0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
  ]);
  const w = new Int32Array(64);
  for (let offset = 0; offset < padded.length; offset += 64) {
    for (let i = 0; i < 16; i++) {
      w[i] = view.getInt32(offset + i * 4, false);
    }
    for (let i = 16; i < 64; i++) {
      const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
      const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) | 0;
    }
    let a = h[0], b = h[1], c = h[2], d = h[3];
    let e = h[4], f = h[5], g = h[6], hh = h[7];
    for (let i = 0; i < 64; i++) {
      const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const ch = (e & f) ^ (~e & g);
      const t1 = (hh + S1 + ch + K256[i] + w[i]) | 0;
      const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const maj = (a & b) ^ (a & c) ^ (b & c);
      const t2 = (S0 + maj) | 0;
      hh = g; g = f; f = e; e = (d + t1) | 0;
      d = c; c = b; b = a; a = (t1 + t2) | 0;
    }
    h[0] = (h[0] + a) | 0; h[1] = (h[1] + b) | 0;
    h[2] = (h[2] + c) | 0; h[3] = (h[3] + d) | 0;
    h[4] = (h[4] + e) | 0; h[5] = (h[5] + f) | 0;
    h[6] = (h[6] + g) | 0; h[7] = (h[7] + hh) | 0;
  }
  return Array.from(h, (x) => (x >>> 0).toString(16).padStart(8, "0")).join("");
}

function rotr(x, n) {
  return (x >>> n) | (x << (32 - n));
}

// ---------------------------------------------------------------------------
// Canonical JSON matching Python json.dumps(sort_keys=True,
// separators=(",", ":")).
// ---------------------------------------------------------------------------

function canonicalJson(value) {
  if (value === null || value === undefined) return "null";
  if (Array.isArray(value)) return "[" + value.map(canonicalJson).join(",") + "]";
  if (typeof value === "object") {
    return (
      "{" +
      Object.keys(value)
        .sort()
        .map((key) => JSON.stringify(key) + ":" + canonicalJson(value[key]))
        .join(",") +
      "}"
    );
  }
  return JSON.stringify(value);
}

function integrityHash(eventType, payload) {
  const digest = sha256(eventType + "\x00" + canonicalJson(payload));
  return "sha256:" + digest;
}

// ---------------------------------------------------------------------------
// Weights mirrored from app/domain/learner.py.
// ---------------------------------------------------------------------------

const DIFFICULTY_WEIGHT = { 1: 0.75, 2: 1.0, 3: 1.25 };
const HINT_MULTIPLIER = { 0: 1.0, 1: 0.8, 2: 0.55, 3: 0.3 };
const REPEAT_SAME_ITEM_MULTIPLIER = 0.35;

const OFFLINE_POLICY_VERSION = "offline-policy-v1";

// ---------------------------------------------------------------------------
// Event envelope construction (SYNC_PROTOCOL section 2).
// ---------------------------------------------------------------------------

function buildEnvelope({
  studentId,
  sessionId,
  sessionBranchId,
  deviceId,
  deviceSequence,
  eventType,
  payload,
  contentPackVersion,
  questionId = null,
  questionVersion = null,
  dependsOnEventIds = [],
  occurredAt = null,
}) {
  const envelope = {
    event_id: "evt_" + uuidHex(),
    student_id: studentId,
    session_id: sessionId,
    session_branch_id: sessionBranchId,
    device_id: deviceId,
    device_sequence: deviceSequence,
    event_type: eventType,
    payload,
    content_pack_version: contentPackVersion,
    question_id: questionId,
    question_version: questionVersion,
    policy_version: OFFLINE_POLICY_VERSION,
    depends_on_event_ids: dependsOnEventIds || [],
    device_occurred_at: occurredAt || new Date().toISOString(),
    integrity_hash: integrityHash(eventType, payload),
  };
  return envelope;
}

function uuidHex() {
  if (typeof crypto !== "undefined" && crypto.randomUUID) {
    return crypto.randomUUID().replaceAll("-", "");
  }
  return "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx".replace(/x/g, () =>
    Math.floor(Math.random() * 16).toString(16)
  );
}

// ---------------------------------------------------------------------------
// Objective-answer evaluation against the installed pack (version-bound).
// ---------------------------------------------------------------------------

function evaluateAnswer(item, selectedChoiceId) {
  const choices = item.choices || [];
  for (const choice of choices) {
    if (choice.id === selectedChoiceId) {
      return { correct: choice.id === item.answer_choice_id };
    }
  }
  return { correct: false };
}

// ---------------------------------------------------------------------------
// Temporary mastery: bounded Beta update, mirrors SkillState.record_attempt.
// ---------------------------------------------------------------------------

function updateTemporaryMastery(state, { correct, difficulty, hintLevel, repeated = false }) {
  const difficultyWeight = DIFFICULTY_WEIGHT[difficulty] ?? 1.0;
  const hintMultiplier = HINT_MULTIPLIER[hintLevel] ?? 1.0;
  const repeatMultiplier = repeated ? REPEAT_SAME_ITEM_MULTIPLIER : 1.0;
  const weight = difficultyWeight * hintMultiplier * repeatMultiplier;

  const next = {
    alpha: (state.alpha ?? 2.0) + (correct ? weight : 0),
    beta: (state.beta ?? 2.0) + (correct ? 0 : weight),
    evidence_count: (state.evidence_count ?? 0) + 1,
  };
  next.mastery = next.alpha / (next.alpha + next.beta);
  next.confidence = Math.min(1.0, next.evidence_count / 8.0);
  return next;
}

// ---------------------------------------------------------------------------
// Bounded offline adaptation policy: pick the next question from the
// installed pack for the weakest skill, preferring unanswered items.
// ---------------------------------------------------------------------------

function pickNextQuestion(packItems, skillStates, answeredIds, maxDifficulty = 3) {
  const eligible = packItems.filter(
    (item) => item.content_type === "question" && !answeredIds.has(item.id)
  );
  if (eligible.length === 0) return null;

  const bySkill = {};
  for (const item of eligible) {
    const skill = item.target_skill || "unknown";
    (bySkill[skill] = bySkill[skill] || []).push(item);
  }

  let weakest = null;
  let weakestMastery = Infinity;
  for (const skill of Object.keys(bySkill)) {
    const mastery = skillStates[skill]?.mastery ?? 0.5;
    if (mastery < weakestMastery) {
      weakestMastery = mastery;
      weakest = skill;
    }
  }
  const pool = bySkill[weakest];
  const sortable = pool.filter((item) => (item.difficulty || 2) <= maxDifficulty);
  const candidates = sortable.length > 0 ? sortable : pool;
  return candidates[Math.floor(Math.random() * candidates.length)];
}

// ---------------------------------------------------------------------------
// Retry schedule (SYNC_PROTOCOL section 8).
// ---------------------------------------------------------------------------

const RETRY_SCHEDULE_MS = [0, 5000, 15000, 60000, 300000, 900000];

function nextRetryDelayMs(attempt) {
  return RETRY_SCHEDULE_MS[Math.min(attempt, RETRY_SCHEDULE_MS.length - 1)];
}

// ---------------------------------------------------------------------------
// Pending-event queue (storage-injected).
// ---------------------------------------------------------------------------

class PendingEventQueue {
  constructor(store) {
    this.store = store; // { get, put, all, delete }
  }

  async all() {
    return this.store.all("pending_events");
  }

  async enqueue(envelope, { dependsOn = [] } = {}) {
    const record = {
      id: envelope.event_id,
      envelope,
      status: "pending",
      attempts: 0,
      next_retry_at: Date.now(),
      created_at: new Date().toISOString(),
    };
    await this.store.put("pending_events", record);
    return record;
  }

  async dequeue() {
    const records = await this.all();
    const now = Date.now();
    const ready = records.filter(
      (record) => record.status === "pending" && record.next_retry_at <= now
    );
    const batched = ready.slice(0, 100);
    for (const record of batched) {
      await this.store.put("pending_events", { ...record, status: "in_flight" });
    }
    return batched.map((record) => record.envelope);
  }

  async markAcknowledged(envelopeId) {
    await this.store.delete("pending_events", envelopeId);
    await this.store.put("acknowledged_events", {
      id: envelopeId,
      acknowledged_at: new Date().toISOString(),
    });
  }

  async markRejected(envelopeId, code, retryable) {
    const record = await this.store.get("pending_events", envelopeId);
    if (!record) return;
    if (!retryable) {
      record.status = "failed";
      record.failure_code = code;
      await this.store.put("pending_events", record);
      return;
    }
    record.status = "pending";
    record.attempts += 1;
    record.next_retry_at = Date.now() + nextRetryDelayMs(record.attempts);
    await this.store.put("pending_events", record);
  }

  async restore() {
    const records = await this.all();
    for (const record of records) {
      if (record.status === "in_flight") {
        record.status = "pending";
        record.next_retry_at = Date.now() + nextRetryDelayMs(record.attempts);
        await this.store.put("pending_events", record);
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Sync client (storage-injected transport).
// ---------------------------------------------------------------------------

class OfflineSyncClient {
  constructor({ store, transport, deviceId, studentId }) {
    this.store = store;
    this.transport = transport; // async (url, options) => {ok, json}
    this.deviceId = deviceId;
    this.studentId = studentId;
    this.queue = new PendingEventQueue(store);
  }

  async sync({ signal } = {}) {
    if (signal && signal.aborted) return { synced: false, reason: "aborted" };
    const envelopes = await this.queue.dequeue();
    if (envelopes.length === 0) return { synced: false, reason: "empty" };

    const batch = {
      device_id: this.deviceId,
      student_id: this.studentId,
      content_pack_versions: ["0.1.0"],
      events: envelopes,
    };
    const response = await this.transport("/v1/sync/events", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(batch),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      const retryable = !(body.detail && body.detail.includes("not registered"));
      for (const envelope of envelopes) {
        await this.queue.markRejected(envelope.event_id, "HTTP_" + response.status, retryable);
      }
      return { synced: false, reason: "http", status: response.status };
    }
    const body = await response.json();
    for (const id of body.accepted_event_ids) {
      await this.queue.markAcknowledged(id);
    }
    for (const rejected of body.rejected_events) {
      await this.queue.markRejected(rejected.event_id, rejected.code, rejected.retryable);
    }
    return { synced: true, body };
  }

  async pullSnapshot() {
    const response = await this.transport(
      `/v1/sync/snapshot?student_id=${encodeURIComponent(this.studentId)}`,
      { method: "GET" }
    );
    if (!response.ok) return null;
    const snapshot = await response.json();
    await this.store.put("memory_snapshot", {
      id: "state",
      snapshot,
      fetched_at: new Date().toISOString(),
    });
    return snapshot;
  }
}

// ---------------------------------------------------------------------------
// Client bootstrap: load local snapshot, resume, sync when possible
// (SYNC_PROTOCOL section 9).
// ---------------------------------------------------------------------------

async function resumeOrSync({ store, syncClient }) {
  await syncClient.queue.restore();
  const snapshot = await syncClient.pullSnapshot();
  if (snapshot) {
    await store.put("profile_snapshot", { id: "state", snapshot, fetched_at: new Date().toISOString() });
  }
  const result = await syncClient.sync();
  return { snapshot, syncResult: result };
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    sha256,
    canonicalJson,
    integrityHash,
    buildEnvelope,
    evaluateAnswer,
    updateTemporaryMastery,
    pickNextQuestion,
    RETRY_SCHEDULE_MS,
    nextRetryDelayMs,
    PendingEventQueue,
    OfflineSyncClient,
    resumeOrSync,
    OFFLINE_POLICY_VERSION,
  };
}
