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
  eventId = null,
}) {
  const envelope = {
    event_id: eventId || "evt_" + uuidHex(),
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

const SKILL_PRESENTATION = {
  linear_equations: ["Linear equations", "Signs, operations, and solving for variables"],
  systems_equations: ["Systems of equations", "Substitution, elimination, and paired relationships"],
  ratios_percentages: ["Ratios & percentages", "Rates, proportions, percent change, and units"],
  functions_models: ["Functions & models", "Function values, slope, and algebraic models"],
  inequalities: ["Inequalities", "Solution boundaries, sign direction, and constraints"],
  quadratic_equations: ["Quadratic equations", "Factoring, roots, and quadratic relationships"],
  exponents_radicals: ["Exponents & radicals", "Exponent rules, reciprocals, and radical forms"],
  coordinate_geometry: ["Coordinate geometry", "Slope, distance, midpoint, and line equations"],
};

function latestPackVersion(versions) {
  if (!Array.isArray(versions) || versions.length === 0) return null;
  const parts = (version) => String(version).split(".").map((part) => Number(part) || 0);
  const sorted = [...versions].sort((left, right) => {
    const a = parts(left);
    const b = parts(right);
    for (let index = 0; index < Math.max(a.length, b.length); index++) {
      const difference = (a[index] || 0) - (b[index] || 0);
      if (difference) return difference;
    }
    return String(left).localeCompare(String(right));
  });
  return sorted[sorted.length - 1];
}

function summarizePackCatalog(contentPack) {
  const items = (contentPack?.items || []).filter(
    (item) => !item.content_type || item.content_type === "question"
  );
  const lessons = contentPack?.lessons || [];
  const skillIds = [...new Set(items.map((item) => item.target_skill).filter(Boolean))].sort();
  const skills = skillIds.map((id) => {
    const presentation = SKILL_PRESENTATION[id] || [
      id.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase()),
      "Targeted SAT Math practice and adaptive instruction",
    ];
    return { id, label: presentation[0], description: presentation[1] };
  });
  return { questionCount: items.length, lessonCount: lessons.length, skills };
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

function pickNextQuestion(
  packItems,
  skillStates,
  answeredIds,
  maxDifficulty = 3,
  constraint = null
) {
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
  let pool = bySkill[constraint?.skill] || bySkill[weakest];
  if (constraint?.transfer_group) {
    const transferPool = pool.filter(
      (item) =>
        item.author_metadata?.transfer_group === constraint.transfer_group &&
        item.author_metadata?.instruction_role === (constraint.instruction_role || "transfer")
    );
    if (transferPool.length > 0) pool = transferPool;
  }
  if (Number.isFinite(constraint?.difficulty)) {
    return [...pool].sort(
      (a, b) =>
        Math.abs((a.difficulty || 2) - constraint.difficulty) -
          Math.abs((b.difficulty || 2) - constraint.difficulty) ||
        String(a.id).localeCompare(String(b.id))
    )[0];
  }
  const sortable = pool.filter((item) => (item.difficulty || 2) <= maxDifficulty);
  const candidates = sortable.length > 0 ? sortable : pool;
  return [...candidates].sort(
    (a, b) =>
      (a.difficulty || 2) - (b.difficulty || 2) ||
      String(a.id).localeCompare(String(b.id))
  )[0];
}

function pickDiagnosticQuestion(packItems, sampledSkills) {
  return (
    packItems.find(
      (item) =>
        item.content_type === "question" &&
        !sampledSkills.has(item.target_skill || "unknown")
    ) || null
  );
}

function weakestSkill(skillStates) {
  const ranked = Object.entries(skillStates).sort(
    ([skillA, stateA], [skillB, stateB]) =>
      (stateA.mastery ?? 0.5) - (stateB.mastery ?? 0.5) ||
      skillA.localeCompare(skillB)
  );
  return ranked.length ? ranked[0][0] : null;
}

function localAgentDecision({
  skill,
  misconception = null,
  observationCount = 0,
  validatedEpisodes = [],
  minutesRemaining = 20,
}) {
  if (minutesRemaining <= 2) {
    return {
      action: "END_WITH_REVIEW",
      action_payload: { review: "time_budget" },
      reason_code: "TIME_BUDGET_EXHAUSTED",
      reason_text:
        "Only a few minutes remain, so BridgeSAT closes with a short review instead of starting a new item.",
      episode_ids: [],
      policy_version: OFFLINE_POLICY_VERSION,
    };
  }
  const recalled = misconception
    ? validatedEpisodes.find(
        (episode) =>
          episode.skill === skill &&
          episode.misconception === misconception &&
          episode.intervention === "SHOW_WORKED_EXAMPLE"
      )
    : null;
  if (recalled) {
    return {
      action: "SHOW_WORKED_EXAMPLE",
      action_payload: { skill, misconception },
      reason_code: "RECALLED_SUCCESSFUL_EPISODE",
      reason_text:
        "A worked example helped this learner recover from the same misconception in an earlier session.",
      episode_ids: [recalled.episode_id],
      policy_version: OFFLINE_POLICY_VERSION,
    };
  }
  if (misconception && observationCount >= 2) {
    return {
      action: "SHOW_WORKED_EXAMPLE",
      action_payload: { skill, misconception },
      reason_code: "REPEATED_MISCONCEPTION",
      reason_text:
        "Repeated errors map to the same misconception, so a worked example is shown before more practice.",
      episode_ids: [],
      policy_version: OFFLINE_POLICY_VERSION,
    };
  }
  return {
    action: "RETRY_SAME_SKILL",
    action_payload: { skill },
    reason_code: misconception ? "MISCONCEPTION_OBSERVED" : "CONTINUE_PRACTICE",
    reason_text: misconception
      ? "The error maps to a known misconception; the next item checks the same skill again."
      : "More evidence is needed before changing the learning path.",
    episode_ids: [],
    policy_version: OFFLINE_POLICY_VERSION,
  };
}

function isSessionEndingAction(event) {
  return ["END_WITH_REVIEW", "END_SESSION"].includes(event?.action);
}

function agentEventToView(event) {
  const episodeId = (event.episode_ids || [])[0] || null;
  const recalled = event.reason_code === "RECALLED_SUCCESSFUL_EPISODE";
  return {
    title:
      event.action === "SHOW_WORKED_EXAMPLE"
        ? "Worked example"
        : event.action === "SHOW_MICRO_LESSON"
          ? "Micro lesson"
        : "Next teaching move",
    showWorkedExample: event.action === "SHOW_WORKED_EXAMPLE",
    showTeachingAsset: ["SHOW_WORKED_EXAMPLE", "SHOW_MICRO_LESSON"].includes(
      event.action
    ),
    memoryBased: recalled,
    memoryBanner: recalled ? "Based on what helped you before" : "",
    why: recalled
      ? "You had a similar misconception before, and a worked example was followed by success on a different item. BridgeSAT is reusing that strategy earlier this time."
      : event.reason_text,
    personalized: event.personalized_explanation || "",
    personalizedEmphasis: event.personalized_emphasis || "",
    reasonCode: event.reason_code,
    episodeLabel: episodeId ? `Episode ${episodeId}` : "",
    policyVersion: event.policy_version || OFFLINE_POLICY_VERSION,
  };
}

function minutesRemaining(budgetMinutes, startedAtMs, nowMs = Date.now()) {
  const elapsedMinutes = Math.max(0, Math.floor((nowMs - startedAtMs) / 60_000));
  return Math.max(0, budgetMinutes - elapsedMinutes);
}

function selectRelevantAgentEvent(events, expectedSourceEventId) {
  if (!expectedSourceEventId) return null;
  return (
    events.find((event) => event.source_event_id === expectedSourceEventId) || null
  );
}

function createActiveSessionSnapshot({
  sessionId,
  branchId,
  currentQuestionId = null,
  answeredIds = new Set(),
  hintLevel = 0,
  skillStates = {},
  misconceptionCounts = {},
  stage = "question_active",
  phase = "practice",
  feedbackState = null,
  diagnosticAnswers = [],
  nextActionConstraint = null,
  sessionStartedAtMs = Date.now(),
}) {
  return {
    id: "state",
    session_id: sessionId,
    branch_id: branchId,
    current_question_id: currentQuestionId,
    answered_ids: [...answeredIds],
    hint_level: hintLevel,
    skill_states: skillStates,
    misconception_counts: misconceptionCounts,
    stage,
    phase,
    feedback_state: feedbackState,
    diagnostic_answers: diagnosticAnswers,
    next_action_constraint: nextActionConstraint,
    session_started_at_ms: sessionStartedAtMs,
    saved_at: new Date().toISOString(),
  };
}

function restoreActiveSessionSnapshot(snapshot) {
  if (!snapshot || !snapshot.session_id) return null;
  return {
    sessionId: snapshot.session_id,
    branchId: snapshot.branch_id || snapshot.session_id,
    currentQuestionId: snapshot.current_question_id || null,
    answeredIds: new Set(snapshot.answered_ids || []),
    hintLevel: snapshot.hint_level || 0,
    skillStates: snapshot.skill_states || {},
    misconceptionCounts: snapshot.misconception_counts || {},
    stage: snapshot.stage || "question_active",
    phase: snapshot.phase || "practice",
    feedbackState: snapshot.feedback_state || null,
    diagnosticAnswers: snapshot.diagnostic_answers || [],
    nextActionConstraint: snapshot.next_action_constraint || null,
    sessionStartedAtMs:
      snapshot.session_started_at_ms || Date.parse(snapshot.saved_at) || Date.now(),
  };
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
    const ready = records
      .filter(
        (record) => record.status === "pending" && record.next_retry_at <= now
      )
      .sort(
        (a, b) =>
          (a.envelope.device_sequence ?? Number.MAX_SAFE_INTEGER) -
            (b.envelope.device_sequence ?? Number.MAX_SAFE_INTEGER) ||
          String(a.envelope.event_id).localeCompare(String(b.envelope.event_id))
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
    this._syncInFlight = null;
  }

  async sync({ signal } = {}) {
    while (this._syncInFlight) {
      await this._syncInFlight.catch(() => undefined);
    }
    const run = this._syncOnce({ signal });
    this._syncInFlight = run;
    try {
      return await run;
    } finally {
      if (this._syncInFlight === run) this._syncInFlight = null;
    }
  }

  async _syncOnce({ signal } = {}) {
    if (signal && signal.aborted) return { synced: false, reason: "aborted" };
    const envelopes = await this.queue.dequeue();
    if (envelopes.length === 0) return { synced: false, reason: "empty" };

    const batch = {
      device_id: this.deviceId,
      student_id: this.studentId,
      content_pack_versions: [
        ...new Set(envelopes.map((event) => event.content_pack_version).filter(Boolean)),
      ],
      events: envelopes,
    };
    let response;
    try {
      response = await this.transport("/v1/sync/events", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(batch),
      });
    } catch (_error) {
      for (const envelope of envelopes) {
        await this.queue.markRejected(envelope.event_id, "NETWORK_UNAVAILABLE", true);
      }
      return { synced: false, reason: "network" };
    }
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      const retryable = !(body.detail && body.detail.includes("not registered"));
      for (const envelope of envelopes) {
        await this.queue.markRejected(envelope.event_id, "HTTP_" + response.status, retryable);
      }
      return { synced: false, reason: "http", status: response.status };
    }
    const body = await response.json();
    const acknowledgedIds = new Set([
      ...(body.accepted_event_ids || []),
      ...(body.duplicate_event_ids || []),
    ]);
    for (const id of acknowledgedIds) {
      await this.queue.markAcknowledged(id);
    }
    for (const rejected of body.rejected_events || []) {
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
    pickDiagnosticQuestion,
    weakestSkill,
    localAgentDecision,
    isSessionEndingAction,
    agentEventToView,
    minutesRemaining,
    selectRelevantAgentEvent,
    createActiveSessionSnapshot,
    restoreActiveSessionSnapshot,
    RETRY_SCHEDULE_MS,
    nextRetryDelayMs,
    latestPackVersion,
    summarizePackCatalog,
    PendingEventQueue,
    OfflineSyncClient,
    resumeOrSync,
    OFFLINE_POLICY_VERSION,
  };
}
