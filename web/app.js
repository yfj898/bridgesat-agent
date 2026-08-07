"use strict";

const profileCard = document.querySelector("#profile-card");
const sessionCard = document.querySelector("#session-card");
const networkStatus = document.querySelector("#network-status");
const syncStatus = document.querySelector("#sync-status");
const pendingCount = document.querySelector("#pending-count");
const questionArea = document.querySelector("#question-area");
const masteryList = document.querySelector("#mastery-list");
const sessionSummary = document.querySelector("#session-summary");

let store = null;
let client = null;
let deviceId = null;
let studentId = null;
let sessionId = null;
let branchId = null;
let deviceSeq = 0;
let pack = null;
let skillStates = {};
let answered = new Set();
let currentQuestion = null;
let hintLevel = 0;
let sessionStarted = false;

function updateNetworkStatus() {
  networkStatus.textContent = navigator.onLine
    ? "Online — progress can sync"
    : "Offline — cached questions remain available";
}

window.addEventListener("online", () => {
  updateNetworkStatus();
  attemptSync();
});
window.addEventListener("offline", updateNetworkStatus);

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => undefined);
}

async function request(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `Request failed: ${response.status}`);
  }
  return response.json();
}

function transport(url, options) {
  return fetch(url, options).then((response) => ({
    ok: response.ok,
    status: response.status,
    json: () => response.json(),
  }));
}

function renderMastery() {
  masteryList.replaceChildren();
  for (const [skill, state] of Object.entries(skillStates)) {
    const row = document.createElement("div");
    row.className = "mastery-row";
    const mastery = Math.round((state.mastery ?? 0.5) * 100);
    const label = document.createElement("span");
    label.textContent = skill.replaceAll("_", " ");
    const value = document.createElement("strong");
    value.textContent = `${mastery}%`;
    row.append(label, value);
    masteryList.append(row);
  }
}

function renderPendingCount() {
  pendingCount.textContent = String(pendingTotal);
}

let pendingTotal = 0;

async function refreshPendingCount() {
  if (!store) return;
  const records = await store.all("pending_events");
  pendingTotal = records.filter((record) => record.status !== "failed").length;
  renderPendingCount();
}

function setSyncStatus(text) {
  syncStatus.textContent = text;
}

async function enqueueEvent(eventType, payload, extra = {}) {
  deviceSeq += 1;
  const envelope = buildEnvelope({
    studentId,
    sessionId,
    sessionBranchId: branchId,
    deviceId,
    deviceSequence: deviceSeq,
    eventType,
    payload,
    contentPackVersion: pack.pack_version,
    ...extra,
  });
  const record = await client.queue.enqueue(envelope);
  await store.put("sync_state", {
    id: "state",
    device_id: deviceId,
    student_id: studentId,
    session_id: sessionId,
    session_branch_id: branchId,
    last_device_sequence: deviceSeq,
  });
  await refreshPendingCount();
  return record;
}

async function attemptSync() {
  if (!client || !navigator.onLine) return;
  const result = await client.sync();
  if (result.synced) {
    const accepted = result.body.accepted_event_ids?.length || 0;
    const rejected = result.body.rejected_events?.length || 0;
    setSyncStatus(
      `Synced — ${accepted} accepted, ${rejected} rejected`
    );
    const snapshot = await client.pullSnapshot();
    if (snapshot) {
      seedSkillStates(snapshot);
      renderMastery();
    }
  } else if (result.reason === "http") {
    setSyncStatus(`Sync failed (HTTP ${result.status}) — will retry`);
  }
  await refreshPendingCount();
}

function seedSkillStates(snapshot) {
  skillStates = {};
  for (const row of snapshot.skill_states || []) {
    skillStates[row.skill] = {
      mastery: row.mastery,
      confidence: row.confidence,
      evidence_count: row.evidence_count,
    };
  }
}

function resetSession() {
  sessionId = "sess_" + uuidHex().slice(0, 16);
  branchId = sessionId;
  answered = new Set();
  hintLevel = 0;
  sessionStarted = false;
  sessionSummary.classList.add("hidden");
  questionArea.classList.remove("hidden");
}

function renderQuestion() {
  const item = currentQuestion;
  hintLevel = 0;
  const hintLabel = document.querySelector("#hint-label");
  hintLabel.textContent = "";

  questionArea.querySelector(".question-prompt").textContent = item.prompt;
  const choices = questionArea.querySelector(".question-choices");
  choices.replaceChildren();
  for (const choice of item.choices) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "choice";
    button.textContent = `${choice.id}. ${choice.text}`;
    button.addEventListener("click", () => submitAnswer(choice.id));
    choices.append(button);
  }
  document.querySelector("#hint-button").disabled = false;
}

function presentQuestion() {
  const item = pickNextQuestion(
    pack.items.filter((entry) => entry.content_type === "question"),
    skillStates,
    answered
  );
  if (!item) {
    questionArea.classList.add("hidden");
    finishSession();
    return;
  }
  currentQuestion = item;
  if (!sessionStarted) {
    sessionStarted = true;
    enqueueEvent("DIAGNOSTIC_STARTED", { student_id: studentId });
  }
  enqueueEvent("CONTENT_PRESENTED", {
    question_id: item.id,
    question_version: item.version,
  });
  renderQuestion();
}

function showHint() {
  const item = currentQuestion;
  if (hintLevel >= (item.hints?.length || 0)) {
    document.querySelector("#hint-button").disabled = true;
    return;
  }
  const hint = item.hints[hintLevel];
  document.querySelector("#hint-label").textContent =
    `Hint (${hint.level}): ${hint.text}`;
  hintLevel = hint.level;
  enqueueEvent("HINT_REQUESTED", {
    question_id: item.id,
    question_version: item.version,
    hint_level: hint.level,
  });
  if (hintLevel >= (item.hints?.length || 0)) {
    document.querySelector("#hint-button").disabled = true;
  }
}

async function submitAnswer(selectedChoiceId) {
  const item = currentQuestion;
  const result = evaluateAnswer(item, selectedChoiceId);
  const skill = item.target_skill || "unknown";
  const previous = skillStates[skill] || { mastery: 0.5 };
  const repeated = answered.has(item.id);
  skillStates[skill] = updateTemporaryMastery(previous, {
    correct: result.correct,
    difficulty: item.difficulty || 2,
    hintLevel,
    repeated,
  });
  answered.add(item.id);
  renderMastery();

  await enqueueEvent(
    "ANSWER_SUBMITTED",
    {
      question_id: item.id,
      question_version: item.version,
      selected_choice_id: selectedChoiceId,
      attempt_id: "att_" + uuidHex().slice(0, 20),
      hint_level: hintLevel,
    },
    { questionId: item.id, questionVersion: item.version }
  );

  const feedback = questionArea.querySelector(".feedback");
  const misconception = !result.correct
    ? item.misconception_map?.[selectedChoiceId]
    : null;
  feedback.replaceChildren();
  const verdict = document.createElement("strong");
  verdict.textContent = result.correct ? "Correct" : "Not quite";
  feedback.append(verdict);
  const explanation = document.createTextNode(
    ` — ${item.worked_explanation || ""}${
      misconception ? ` (${misconception.replaceAll("_", " ")})` : ""
    }`
  );
  feedback.append(explanation);

  const nextButton = questionArea.querySelector(".next-button");
  nextButton.classList.remove("hidden");
  questionArea.querySelector(".question-choices").replaceChildren();
  document.querySelector("#hint-button").disabled = true;
  attemptSync();
}

async function finishSession() {
  await enqueueEvent("SESSION_COMPLETED", { student_id: studentId });
  await attemptSync();
  const snapshot = await client.pullSnapshot().catch(() => null);
  if (snapshot) {
    seedSkillStates(snapshot);
    renderMastery();
  }
  const stats = snapshot?.strategy_memory?.intervention_stats || [];
  const summaryText = sessionSummary.querySelector(".summary-text");
  await refreshPendingCount();
  const pending = pendingTotal;
  summaryText.textContent = `Session complete. ${stats.length} recorded intervention signal${
    stats.length === 1 ? "" : "s"
  }; ${
    pending > 0
      ? `${pending} event${pending === 1 ? "" : "s"} queued for sync`
      : "all events synced"
  }.`;
  sessionSummary.classList.remove("hidden");
  questionArea.classList.add("hidden");
}

async function init() {
  store = await OfflineStore.open();
  await refreshPendingCount();
  const state = await new SyncStateAccess(store).load();
  if (state?.device_id && state?.student_id) {
    deviceId = state.device_id;
    studentId = state.student_id;
    sessionId = state.session_id || "sess_" + uuidHex().slice(0, 16);
    branchId = state.session_branch_id || sessionId;
    deviceSeq = state.last_device_sequence || 0;
    pack = await store.get("content_packs", "0.1.0");
    client = new OfflineSyncClient({ store, transport, deviceId, studentId });
    await client.queue.restore();
    const snapshot = await client.pullSnapshot().catch(() => null);
    if (snapshot) seedSkillStates(snapshot);
    if (pack) {
      profileCard.classList.add("hidden");
      sessionCard.classList.remove("hidden");
      renderMastery();
      resetSession();
      presentQuestion();
    } else {
      installPack();
    }
    attemptSync();
  }
  updateNetworkStatus();
}

async function createProfile(name, minutes) {
  studentId = (await request("/v1/students", {
    method: "POST",
    body: JSON.stringify({
      name,
      daily_minutes: minutes,
      target_score: 1200,
    }),
  })).id;
  const device = await request("/v1/sync/devices", {
    method: "POST",
    body: JSON.stringify({ student_id: studentId, device_name: "browser" }),
  });
  deviceId = device.device_id;
  await store.put("sync_state", {
    id: "state",
    device_id: deviceId,
    student_id: studentId,
    last_device_sequence: 0,
  });
  client = new OfflineSyncClient({ store, transport, deviceId, studentId });
  await installPack();
}

async function installPack() {
  const listing = await request("/v1/content-packs");
  const version = listing.packs[0];
  pack = await request(`/v1/content-packs/${version}`);
  await store.put("content_packs", { id: pack.pack_version, ...pack });
  profileCard.classList.add("hidden");
  sessionCard.classList.remove("hidden");
  resetSession();
  presentQuestion();
}

document.querySelector("#profile-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = profileCard.querySelector("button");
  button.disabled = true;
  try {
    await createProfile(
      document.querySelector("#name").value,
      Number(document.querySelector("#minutes").value)
    );
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
  }
});

document.querySelector("#hint-button").addEventListener("click", showHint);
document.querySelector(".next-button").addEventListener("click", () => {
  presentQuestion();
});
document.querySelector("#new-session-button").addEventListener("click", () => {
  resetSession();
  presentQuestion();
});

init();
