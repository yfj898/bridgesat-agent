"use strict";

const welcomeCard = document.querySelector("#welcome-card");
const profileCard = document.querySelector("#profile-card");
const sessionCard = document.querySelector("#session-card");
const networkStatus = document.querySelector("#network-status");
const syncStatus = document.querySelector("#sync-status");
const questionArea = document.querySelector("#question-area");
const masteryList = document.querySelector("#mastery-list");
const sessionSummary = document.querySelector("#session-summary");
const diagnosticSummary = document.querySelector("#diagnostic-summary");
const learningPhase = document.querySelector("#learning-phase");
const learningFocus = document.querySelector("#learning-focus");
const agentIntervention = document.querySelector("#agent-intervention");
const catalogQuestionCount = document.querySelector("#catalog-question-count");
const catalogLessonCount = document.querySelector("#catalog-lesson-count");
const catalogSkillCount = document.querySelector("#catalog-skill-count");
const catalogSkillGrid = document.querySelector("#catalog-skill-grid");

let store = null;
let client = null;
let deviceId = null;
let studentId = null;
let authToken = null;
let sessionId = null;
let branchId = null;
let deviceSeq = 0;
let pack = null;
let skillStates = {};
let answered = new Set();
let currentQuestion = null;
let hintLevel = 0;
let sessionStarted = false;
let sessionPhase = "diagnostic";
let strategyMemory = { validated_episodes: [] };
let misconceptionCounts = {};
let diagnosticAnswers = [];
let currentAnswerEventId = null;
let feedbackState = null;
let nextActionConstraint = null;
const presentedInterventionSources = new Set();
let sessionStartedAtMs = Date.now();
let dailyMinutes = 20;

const DIAGNOSTIC_ITEM_COUNT = 2;

function authHeaders(extra) {
  return {
    ...(authToken ? { Authorization: `Bearer ${authToken}` } : {}),
    ...(extra || {}),
  };
}

function updateNetworkStatus() {
  networkStatus.textContent = navigator.onLine
    ? "Connected. Your learning progress will save automatically."
    : "Your connection is down, but you can keep learning. Your progress will save automatically when it returns.";
}

function renderWelcomeCatalog(contentPack) {
  const summary = summarizePackCatalog(contentPack);
  catalogQuestionCount.textContent = String(summary.questionCount);
  catalogLessonCount.textContent = String(summary.lessonCount);
  catalogSkillCount.textContent = String(summary.skills.length);
  catalogSkillGrid.replaceChildren();
  for (const skill of summary.skills) {
    const tile = document.createElement("article");
    tile.className = "skill-tile";
    const label = document.createElement("strong");
    label.textContent = skill.label;
    const description = document.createElement("span");
    description.textContent = skill.description;
    tile.append(label, description);
    catalogSkillGrid.append(tile);
  }
}

async function hydrateWelcomeCatalog() {
  const cached = await store.all("content_packs");
  const cachedVersion = latestPackVersion(cached.map((entry) => entry.pack_version || entry.id));
  const cachedPack = cached.find(
    (entry) => (entry.pack_version || entry.id) === cachedVersion
  );
  if (cachedPack) renderWelcomeCatalog(cachedPack);
  try {
    const listing = await request("/v1/content-packs");
    const version = latestPackVersion(listing.packs);
    if (!version) return;
    renderWelcomeCatalog(await request(`/v1/content-packs/${version}`));
  } catch (_error) {
    // The static/cached catalog remains usable when the welcome screen is offline.
  }
}

window.addEventListener("online", () => {
  networkStatus.textContent = "You're connected again. Saving the progress from this session…";
  setSyncStatus("Saving your recent learning progress…");
  attemptSync();
});
window.addEventListener("offline", () => {
  updateNetworkStatus();
  setSyncStatus(
    "You can keep learning. Your progress will save automatically when the connection returns."
  );
});

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => undefined);
}

async function request(url, options = {}) {
  const response = await fetch(url, {
    headers: {
      "Content-Type": "application/json",
      ...authHeaders(),
      ...(options.headers || {}),
    },
    ...options,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `Request failed: ${response.status}`);
  }
  return response.json();
}

function transport(url, options) {
  return fetch(url, {
    ...options,
    headers: { ...authHeaders(), ...(options.headers || {}) },
  }).then((response) => ({
    ok: response.ok,
    status: response.status,
    json: () => response.json(),
  }));
}

function studentSkillLabel(skill) {
  const label = String(skill || "SAT foundations").replaceAll("_", " ");
  return label.charAt(0).toUpperCase() + label.slice(1);
}

function studentProgressLabel(state) {
  if ((state?.evidence_count || 0) < 2) return "Getting started";
  return (state?.mastery || 0) >= 0.65 ? "Getting steadier" : "Keep practicing";
}

function isTeachingAction(event) {
  return ["SHOW_WORKED_EXAMPLE", "SHOW_MICRO_LESSON"].includes(event?.action);
}

function renderMastery() {
  masteryList.replaceChildren();
  for (const [skill, state] of Object.entries(skillStates)) {
    const row = document.createElement("div");
    row.className = "mastery-row";
    const label = document.createElement("span");
    label.textContent = studentSkillLabel(skill);
    const value = document.createElement("strong");
    value.textContent = studentProgressLabel(state);
    row.append(label, value);
    masteryList.append(row);
  }
  const weakest = weakestSkill(skillStates);
  learningFocus.textContent = weakest
    ? `Let's work on ${studentSkillLabel(weakest)} next.`
    : "Answer two questions and BridgeSAT will find a useful place to begin.";
}

function renderPhase() {
  learningPhase.textContent =
    sessionPhase === "diagnostic" ? "Find your starting point" : "Practice that adapts to you";
}

let pendingTotal = 0;
let failedEventTotal = 0;

async function refreshPendingCount() {
  if (!store) return;
  const records = await store.all("pending_events");
  pendingTotal = records.filter((record) => record.status !== "failed").length;
  failedEventTotal = records.filter((record) => record.status === "failed").length;
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
    token: authToken,
    daily_minutes: dailyMinutes,
    content_pack_version: pack.pack_version,
  });
  await refreshPendingCount();
  return record;
}

async function persistActiveSession(stage = "question_active") {
  if (!store || !sessionId) return;
  await store.put(
    "active_session",
    createActiveSessionSnapshot({
      sessionId,
      branchId,
      currentQuestionId: currentQuestion?.id || null,
      answeredIds: answered,
      hintLevel,
      skillStates,
      misconceptionCounts,
      stage,
      phase: sessionPhase,
      feedbackState,
      diagnosticAnswers,
      nextActionConstraint,
      sessionStartedAtMs,
    })
  );
}

async function attemptSync() {
  if (!client || !navigator.onLine) return null;
  try {
    await refreshPendingCount();
    if (pendingTotal > 0) {
      setSyncStatus("Saving your recent learning progress…");
    }
    const result = await client.sync();
    const rejected = result.body?.rejected_events?.length || 0;
    if (result.synced) {
      if (result.body.memory_snapshot) {
        strategyMemory = result.body.memory_snapshot;
      }
      consumeAgentEvents(result.body.server_events || []);
      const snapshot = await client.pullSnapshot();
      if (snapshot) {
        seedSkillStates(snapshot);
        strategyMemory = snapshot.strategy_memory || strategyMemory;
        consumeAgentEvents(strategyMemory.recent_agent_events || []);
        renderMastery();
      }
    }
    await refreshPendingCount();
    if (failedEventTotal > 0) {
      setSyncStatus(
        "Some progress could not be saved yet. Keep learning; recent work remains on this device."
      );
    } else if (result.synced) {
      setSyncStatus(
        rejected > 0
          ? "Some saved progress needs another try. Keep learning; nothing has been lost."
          : "Your recent learning progress is saved."
      );
    } else if (result.reason === "empty" && pendingTotal > 0) {
      setSyncStatus(
        "Some saved progress needs another try. Keep learning; nothing has been lost."
      );
    } else if (result.reason === "empty") {
      setSyncStatus("Your progress is saved.");
    } else if (result.reason === "http" || result.reason === "network") {
      setSyncStatus(
        "Connection interrupted — progress is safe on this device and will retry"
      );
    }
    return result;
  } catch (_error) {
    setSyncStatus(
      "Connection interrupted — progress is safe on this device and will retry"
    );
    await client.queue.restore();
    await refreshPendingCount();
    return null;
  }
}

function seedSkillStates(snapshot) {
  if (!snapshot.skill_states?.length) return;
  skillStates = {};
  for (const row of snapshot.skill_states || []) {
    skillStates[row.skill] = {
      mastery: row.mastery,
      confidence: row.confidence,
      evidence_count: row.evidence_count,
    };
  }
}

function resetSession(phase = "practice") {
  sessionId = "sess_" + uuidHex().slice(0, 16);
  branchId = sessionId;
  answered = new Set();
  hintLevel = 0;
  sessionStarted = false;
  sessionPhase = phase;
  misconceptionCounts = {};
  currentAnswerEventId = null;
  feedbackState = null;
  nextActionConstraint = null;
  sessionStartedAtMs = Date.now();
  if (phase === "diagnostic") diagnosticAnswers = [];
  sessionSummary.classList.add("hidden");
  diagnosticSummary.classList.add("hidden");
  questionArea.classList.remove("hidden");
  document.querySelector("#end-session-button").classList.toggle(
    "hidden",
    phase === "diagnostic"
  );
  renderPhase();
}

function renderQuestion({ preserveState = false } = {}) {
  const item = currentQuestion;
  if (!preserveState) hintLevel = 0;
  const hintLabel = document.querySelector("#hint-label");
  hintLabel.textContent = hintLevel
    ? "You had already opened this hint before refresh."
    : "";
  questionArea.querySelector(".feedback").textContent = "";
  agentIntervention.classList.add("hidden");
  for (const details of agentIntervention.querySelectorAll("details")) {
    details.open = false;
  }
  const nextButton = questionArea.querySelector(".next-button");
  nextButton.classList.add("hidden");
  nextButton.textContent = "Next question";

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

async function presentQuestion() {
  const questions = pack.items.filter((entry) => entry.content_type === "question");
  const sampledSkills = new Set(diagnosticAnswers.map((answer) => answer.skill));
  const item =
    sessionPhase === "diagnostic"
      ? pickDiagnosticQuestion(questions, sampledSkills)
      : pickNextQuestion(
          questions,
          skillStates,
          answered,
          3,
          nextActionConstraint
        );
  if (!item) {
    if (sessionPhase === "diagnostic") await completeDiagnostic();
    else await finishSession();
    return;
  }
  currentQuestion = item;
  nextActionConstraint = null;
  currentAnswerEventId = null;
  feedbackState = null;
  if (!sessionStarted) {
    sessionStarted = true;
    if (sessionPhase === "diagnostic") {
      await enqueueEvent("DIAGNOSTIC_STARTED", { student_id: studentId });
    }
  }
  await enqueueEvent("CONTENT_PRESENTED", {
    question_id: item.id,
    question_version: item.version,
  });
  renderQuestion();
  await persistActiveSession("question_active");
}

async function showHint() {
  const item = currentQuestion;
  if (hintLevel >= (item.hints?.length || 0)) {
    document.querySelector("#hint-button").disabled = true;
    return;
  }
  const hint = item.hints[hintLevel];
  document.querySelector("#hint-label").textContent =
    `Hint (${hint.level}): ${hint.text}`;
  hintLevel = hint.level;
  await enqueueEvent("HINT_REQUESTED", {
    question_id: item.id,
    question_version: item.version,
    hint_level: hint.level,
  });
  if (hintLevel >= (item.hints?.length || 0)) {
    document.querySelector("#hint-button").disabled = true;
  }
  await persistActiveSession("question_active");
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
  const selectedChoice = item.choices.find((choice) => choice.id === selectedChoiceId);
  if (sessionPhase === "diagnostic") {
    diagnosticAnswers.push({
      question_id: item.id,
      selected_answer: selectedChoice?.text || "",
      hint_level: hintLevel,
      skill,
    });
  }

  const misconception = !result.correct
    ? item.misconception_map?.[selectedChoiceId] || null
    : null;
  if (misconception) {
    const key = `${skill}:${misconception}`;
    misconceptionCounts[key] = (misconceptionCounts[key] || 0) + 1;
  }
  const remainingMinutes = minutesRemaining(dailyMinutes, sessionStartedAtMs);
  const localEvent = localAgentDecision({
    skill,
    misconception,
    observationCount: misconception
      ? misconceptionCounts[`${skill}:${misconception}`]
      : 0,
    validatedEpisodes: strategyMemory.validated_episodes || [],
    minutesRemaining: remainingMinutes,
  });
  const shouldTransfer = ["SHOW_WORKED_EXAMPLE", "SHOW_MICRO_LESSON"].includes(
    localEvent.action
  );
  nextActionConstraint = {
    ...(localEvent.action_payload || {}),
    ...(shouldTransfer && item.author_metadata?.transfer_group
      ? {
          transfer_group: item.author_metadata.transfer_group,
          instruction_role: "transfer",
        }
      : {}),
  };
  feedbackState = {
    correct: result.correct,
    misconception,
    localAgentEvent: localEvent,
    selectedChoiceId,
  };
  renderMastery();

  const queued = await enqueueEvent(
    "ANSWER_SUBMITTED",
    {
      question_id: item.id,
      question_version: item.version,
      selected_choice_id: selectedChoiceId,
      attempt_id: "att_" + uuidHex().slice(0, 20),
      hint_level: hintLevel,
      minutes_remaining: remainingMinutes,
    },
    { questionId: item.id, questionVersion: item.version }
  );
  currentAnswerEventId = queued.envelope.event_id;
  feedbackState.sourceEventId = currentAnswerEventId;
  feedbackState.localAgentEvent = {
    ...localEvent,
    source_event_id: currentAnswerEventId,
  };
  renderAnswerFeedback(feedbackState);
  const answeredWhileOnline = navigator.onLine;
  if (!answeredWhileOnline) {
    await recordPresentedIntervention(feedbackState.localAgentEvent);
  }
  await persistActiveSession("feedback");
  const syncResult = await attemptSync();
  if (
    answeredWhileOnline &&
    (!syncResult?.synced ||
      !(syncResult.body?.server_events || []).some(
        (event) => event.source_event_id === currentAnswerEventId
      ))
  ) {
    await recordPresentedIntervention(feedbackState.localAgentEvent);
    await persistActiveSession("feedback");
  }
}

function renderAnswerFeedback(state) {
  const item = currentQuestion;
  const feedback = questionArea.querySelector(".feedback");
  feedback.replaceChildren();
  const agentEvent = state.serverAgentEvent || state.localAgentEvent;
  const verdict = document.createElement("strong");
  verdict.textContent = state.correct ? "Nice work" : "Not quite";
  feedback.append(verdict);
  const explanationText = state.correct
    ? ` — ${item.worked_explanation || ""}`
    : isTeachingAction(agentEvent)
      ? " — Let's switch approaches before you do more practice."
      : " — Try one more similar problem so I can see whether the same step is getting in the way.";
  const explanation = document.createTextNode(explanationText);
  feedback.append(explanation);

  const nextButton = questionArea.querySelector(".next-button");
  nextButton.textContent =
    sessionPhase === "diagnostic" && diagnosticAnswers.length >= DIAGNOSTIC_ITEM_COUNT
      ? "See where to start"
      : isSessionEndingAction(agentEvent)
        ? "Finish with a review"
        : isTeachingAction(agentEvent)
          ? "Try a new problem"
          : "Next question";
  nextButton.classList.remove("hidden");
  questionArea.querySelector(".question-choices").replaceChildren();
  document.querySelector("#hint-button").disabled = true;
  if (sessionPhase === "practice") {
    renderAgentIntervention(state.serverAgentEvent || state.localAgentEvent);
  }
}

function findTeachingAsset(event) {
  const targetSkill = event.action_payload?.skill || currentQuestion?.target_skill;
  const targetType =
    event.action === "SHOW_MICRO_LESSON" ? "micro_lesson" : "worked_example";
  const candidates = (pack.lessons || []).filter(
    (entry) =>
      entry.content_type === targetType &&
      entry.target_skill === targetSkill &&
      entry.review_status === "approved"
  );
  return (
    candidates.find((entry) => entry.id === event.action_payload?.content_id) ||
    candidates.find((entry) =>
      (entry.target_misconceptions || []).includes(event.action_payload?.misconception)
    ) ||
    candidates[0] ||
    null
  );
}

async function recordPresentedIntervention(event) {
  if (
    !event ||
    !["SHOW_WORKED_EXAMPLE", "SHOW_MICRO_LESSON"].includes(event.action)
  ) {
    return false;
  }
  const lesson = findTeachingAsset(event);
  const sourceEventId = event.source_event_id || currentAnswerEventId;
  if (!lesson || !sourceEventId) return false;
  const presentationKey = `${sourceEventId}:${lesson.id}`;
  if (presentedInterventionSources.has(presentationKey)) return false;
  presentedInterventionSources.add(presentationKey);
  const presentationEventType =
    event.action === "SHOW_MICRO_LESSON"
      ? "MICRO_LESSON_PRESENTED"
      : "WORKED_EXAMPLE_PRESENTED";
  await enqueueEvent(
    presentationEventType,
    {
      source_answer_event_id: sourceEventId,
      content_id: lesson.id,
      content_version: lesson.version,
      skill: lesson.target_skill,
      misconception: event.action_payload?.misconception,
      intervention: event.action,
    },
    {
      eventId: `evt_presented_${sha256(presentationKey).slice(0, 24)}`,
      questionId: lesson.id,
      questionVersion: lesson.version,
      dependsOnEventIds: [sourceEventId],
    }
  );
  return true;
}

function renderAgentIntervention(event) {
  if (!event) return;
  const view = agentEventToView(event);
  const memoryBanner = agentIntervention.querySelector(".memory-banner");
  memoryBanner.textContent = view.memoryBanner;
  memoryBanner.classList.toggle("hidden", !view.memoryBased);
  agentIntervention.querySelector(".intervention-title").textContent =
    event.validated_episode_id ? "This approach worked on a new problem." : view.title;
  agentIntervention.querySelector(".intervention-why").textContent = view.why;
  agentIntervention.querySelector(".recommendation-detail").textContent = view.why;
  const content = agentIntervention.querySelector(".intervention-content");
  content.replaceChildren();
  let lesson = null;
  if (view.showTeachingAsset) {
    lesson = findTeachingAsset(event);
    if (lesson) {
      const guide = document.createElement("p");
      guide.className = "intervention-guide";
      guide.textContent = view.lessonLead;
      const heading = document.createElement("strong");
      heading.textContent = lesson.title;
      const body = document.createElement("p");
      body.textContent = lesson.body;
      content.append(guide, heading, body);
    }
  }
  const meta = [view.reasonCode, view.policyVersion];
  if (view.episodeLabel) meta.push(view.episodeLabel);
  if (view.personalized) {
    meta.push(`personalized: ${view.personalizedEmphasis || "explanation"}`);
  }
  if (event.validated_episode_id) meta.push(`Validated Episode ${event.validated_episode_id}`);
  if (lesson) {
    const simulatedReview = Object.values(lesson.reviewers || {}).some((reviewer) =>
      String(reviewer).startsWith("sim.")
    );
    meta.push(
      `content gate: ${lesson.review_status}${simulatedReview ? " (simulated reviewers)" : ""}`
    );
    meta.push(`license: ${lesson.license?.id || "missing"}`);
    meta.push(`source: ${lesson.source_lineage?.source_id || "missing"}`);
  }
  agentIntervention.querySelector(".intervention-meta").textContent = meta
    .filter(Boolean)
    .join(" · ");
  agentIntervention.classList.remove("hidden");
}

function consumeAgentEvents(events) {
  if (sessionPhase !== "practice" || !events.length) return;
  const expectedSourceEventId =
    currentAnswerEventId || feedbackState?.sourceEventId || null;
  if (!expectedSourceEventId) return;
  const relevant = selectRelevantAgentEvent(events, expectedSourceEventId);
  if (!relevant) return;
  if (
    feedbackState?.serverAgentEvent?.source_event_id === relevant.source_event_id &&
    feedbackState.serverAgentEvent.hybrid_ranked &&
    !relevant.hybrid_ranked
  ) {
    return;
  }
  if (
    feedbackState?.serverAgentEvent?.source_event_id === relevant.source_event_id &&
    feedbackState.serverAgentEvent.validated_episode_id &&
    !relevant.validated_episode_id
  ) {
    return;
  }
  feedbackState = { ...(feedbackState || {}), serverAgentEvent: relevant };
  nextActionConstraint = {
    ...(nextActionConstraint || {}),
    ...(relevant.action_payload || {}),
  };
  if (isTeachingAction(relevant) && currentQuestion?.author_metadata?.transfer_group) {
    nextActionConstraint = {
      ...nextActionConstraint,
      transfer_group: currentQuestion.author_metadata.transfer_group,
      instruction_role: "transfer",
    };
  }
  renderAgentIntervention(relevant);
  if (isSessionEndingAction(relevant)) {
    questionArea.querySelector(".next-button").textContent = "Finish with a review";
  } else if (isTeachingAction(relevant)) {
    questionArea.querySelector(".next-button").textContent = "Try a new problem";
  }
  persistActiveSession("feedback");
  recordPresentedIntervention(relevant).then((recorded) => {
    if (recorded) attemptSync();
  });
}

async function completeDiagnostic() {
  await enqueueEvent("DIAGNOSTIC_COMPLETED", {
    answered_question_ids: diagnosticAnswers.map((answer) => answer.question_id),
  });
  await attemptSync();
  let result = null;
  if (navigator.onLine) {
    result = await request("/v1/diagnostics", {
      method: "POST",
      body: JSON.stringify({
        answers: diagnosticAnswers.map(({ question_id, selected_answer, hint_level }) => ({
          question_id,
          selected_answer,
          hint_level,
        })),
      }),
    }).catch(() => null);
  }
  const focus =
    result?.weakest_skills?.[0] ||
    weakestSkill(skillStates) ||
    diagnosticAnswers[0]?.skill ||
    "SAT foundations";
  const plan = result?.plan || [
    {
      activity: "worked example",
      skill: focus,
      minutes: Math.max(2, Math.round(dailyMinutes * 0.25)),
      reason: "Build the missing concept before more practice.",
    },
    {
      activity: "targeted practice",
      skill: focus,
      minutes: Math.max(3, Math.round(dailyMinutes * 0.6)),
      reason: "Use new items on the current weak area.",
    },
  ];
  const diagnosticResult = {
    focus,
    plan,
    explanation:
      result?.agent_explanation ||
      `BridgeSAT prioritized ${focus?.replaceAll("_", " ")} from the local diagnostic evidence.`,
  };
  feedbackState = { diagnosticResult };
  renderDiagnosticResult(diagnosticResult);
  await persistActiveSession("diagnostic_complete");
}

function renderDiagnosticResult(result) {
  questionArea.classList.add("hidden");
  diagnosticSummary.classList.remove("hidden");
  const focus = result.focus || diagnosticAnswers[0]?.skill || "SAT foundations";
  learningFocus.textContent = `Start with ${studentSkillLabel(focus)}.`;
  diagnosticSummary.querySelector(".diagnostic-explanation").textContent =
    `We'll begin with ${studentSkillLabel(focus)} and adjust as we learn what helps you.`;
  const planList = diagnosticSummary.querySelector(".diagnostic-plan");
  planList.replaceChildren();
  for (const item of result.plan) {
    const row = document.createElement("li");
    const skill = studentSkillLabel(item.skill || result.focus);
    row.textContent = `Start with ${item.activity.replaceAll("_", " ")} for ${skill}.`;
    planList.append(row);
  }
}

async function finishSession() {
  const completionRecord = await enqueueEvent("SESSION_COMPLETED", {
    student_id: studentId,
  });
  const synced = await attemptSync();
  const snapshot = await client.pullSnapshot().catch(() => null);
  if (snapshot) {
    seedSkillStates(snapshot);
    strategyMemory = snapshot.strategy_memory || strategyMemory;
    renderMastery();
  }
  const episodes = strategyMemory.validated_episodes || [];
  const summaryText = sessionSummary.querySelector(".summary-text");
  await refreshPendingCount();
  const pending = pendingTotal;
  // H8: the server may return a verified, fact-grounded session summary
  // (plan Section 15). Use it only when present and non-empty; every failure
  // path (offline, provider unavailable, rejected proposal) falls back to
  // the deterministic summary prose below.
  const personalizedSummaries = synced?.body?.personalized_summaries;
  const currentSummary = Array.isArray(personalizedSummaries)
    ? personalizedSummaries.find(
        (entry) =>
          entry.source_event_id === completionRecord?.id &&
          entry.session_id === sessionId
      )?.summary_text
    : null;
  const legacySummary =
    (!Array.isArray(personalizedSummaries) || personalizedSummaries.length === 0) &&
    typeof synced?.body?.personalized_summary === "string"
      ? synced.body.personalized_summary
      : "";
  const verifiedSummary = String(currentSummary || legacySummary).trim();
  const syncStatusText = failedEventTotal > 0
    ? "Some progress could not be saved yet. Keep learning; recent work remains on this device."
    : pending > 0
      ? "Your progress is saved on this device and will save when the connection returns."
      : "Your progress is saved.";
  if (verifiedSummary) {
    summaryText.textContent = `${verifiedSummary} ${syncStatusText}`;
  } else {
    const memorySummary = episodes.length > 0
      ? "BridgeSAT will remember the approaches that helped you, so it can use them sooner next time."
      : "BridgeSAT will keep track of the approaches that help you learn.";
    summaryText.textContent = `Session complete. ${memorySummary} ${syncStatusText}`;
  }
  sessionSummary.classList.remove("hidden");
  questionArea.classList.add("hidden");
  await store.delete("active_session", "state");
}

async function upgradeInstalledPackWhenIdle(activeSession) {
  if (activeSession || !pack || !navigator.onLine) return false;
  try {
    const listing = await request("/v1/content-packs");
    const newest = latestPackVersion([pack.pack_version, ...(listing.packs || [])]);
    if (!newest || newest === pack.pack_version) return false;
    const upgraded = await request(`/v1/content-packs/${newest}`);
    await store.put("content_packs", { id: upgraded.pack_version, ...upgraded });
    const state = (await new SyncStateAccess(store).load()) || {};
    await new SyncStateAccess(store).save({
      ...state,
      content_pack_version: upgraded.pack_version,
    });
    pack = upgraded;
    renderWelcomeCatalog(pack);
    return true;
  } catch (_error) {
    return false;
  }
}

async function init() {
  store = await OfflineStore.open();
  await refreshPendingCount();
  await hydrateWelcomeCatalog();
  const state = await new SyncStateAccess(store).load();
  if (state?.device_id && state?.student_id) {
    deviceId = state.device_id;
    studentId = state.student_id;
    authToken = state.token || null;
    sessionId = state.session_id || "sess_" + uuidHex().slice(0, 16);
    branchId = state.session_branch_id || sessionId;
    deviceSeq = state.last_device_sequence || 0;
    dailyMinutes = state.daily_minutes || 20;
    const cachedPacks = await store.all("content_packs");
    const cachedVersion = state.content_pack_version || latestPackVersion(
      cachedPacks.map((entry) => entry.pack_version || entry.id)
    );
    pack = cachedVersion ? await store.get("content_packs", cachedVersion) : null;
    client = new OfflineSyncClient({ store, transport, deviceId, studentId });
    await client.queue.restore();
    const cachedMemory = await store.get("memory_snapshot", "state");
    if (cachedMemory?.snapshot?.strategy_memory) {
      strategyMemory = cachedMemory.snapshot.strategy_memory;
    }
    const activeRecord = await store.get("active_session", "state");
    const active = restoreActiveSessionSnapshot(activeRecord);
    await upgradeInstalledPackWhenIdle(active);
    const snapshot = await client.pullSnapshot().catch(() => null);
    if (snapshot) {
      seedSkillStates(snapshot);
      strategyMemory = snapshot.strategy_memory || strategyMemory;
      dailyMinutes = snapshot.student?.daily_minutes || dailyMinutes;
    }
    if (pack) {
      welcomeCard.classList.add("hidden");
      profileCard.classList.add("hidden");
      sessionCard.classList.remove("hidden");
      if (active) {
        sessionId = active.sessionId;
        branchId = active.branchId;
        answered = active.answeredIds;
        hintLevel = active.hintLevel;
        if (Object.keys(active.skillStates).length) skillStates = active.skillStates;
        misconceptionCounts = active.misconceptionCounts;
        sessionPhase = active.phase;
        diagnosticAnswers = active.diagnosticAnswers;
        feedbackState = active.feedbackState;
        nextActionConstraint = active.nextActionConstraint;
        sessionStartedAtMs = active.sessionStartedAtMs;
        currentAnswerEventId = feedbackState?.sourceEventId || null;
        sessionStarted = true;
        currentQuestion = pack.items.find(
          (item) => item.id === active.currentQuestionId
        );
        renderPhase();
        renderMastery();
        document.querySelector("#end-session-button").classList.toggle(
          "hidden",
          sessionPhase === "diagnostic"
        );
        if (active.stage === "diagnostic_complete" && feedbackState?.diagnosticResult) {
          renderDiagnosticResult(feedbackState.diagnosticResult);
        } else if (currentQuestion) {
          renderQuestion({ preserveState: true });
          if (active.stage === "feedback" && feedbackState) {
            renderAnswerFeedback(feedbackState);
          }
        } else {
          await presentQuestion();
        }
      } else {
        resetSession("diagnostic");
        renderMastery();
        await presentQuestion();
      }
    } else {
      await installPack();
    }
    attemptSync();
  }
  updateNetworkStatus();
}

async function createProfile(name, minutes) {
  dailyMinutes = minutes;
  const created = await request("/v1/students", {
    method: "POST",
    body: JSON.stringify({
      name,
      daily_minutes: minutes,
      target_score: 1200,
    }),
  });
  studentId = created.id;
  authToken = created.token;
  const device = await request("/v1/sync/devices", {
    method: "POST",
    body: JSON.stringify({ device_name: "browser" }),
  });
  deviceId = device.device_id;
  await store.put("sync_state", {
    id: "state",
    device_id: deviceId,
    student_id: studentId,
    token: authToken,
    last_device_sequence: 0,
    daily_minutes: dailyMinutes,
  });
  client = new OfflineSyncClient({ store, transport, deviceId, studentId });
  await installPack();
}

async function installPack() {
  const listing = await request("/v1/content-packs");
  const version = latestPackVersion(listing.packs);
  if (!version) throw new Error("No published content pack is available");
  pack = await request(`/v1/content-packs/${version}`);
  await store.put("content_packs", { id: pack.pack_version, ...pack });
  const state = (await new SyncStateAccess(store).load()) || {};
  await new SyncStateAccess(store).save({
    ...state,
    content_pack_version: pack.pack_version,
  });
  renderWelcomeCatalog(pack);
  welcomeCard.classList.add("hidden");
  profileCard.classList.add("hidden");
  sessionCard.classList.remove("hidden");
  resetSession("diagnostic");
  renderMastery();
  await presentQuestion();
}

document.querySelector("#start-diagnostic-button").addEventListener("click", () => {
  welcomeCard.classList.add("hidden");
  profileCard.classList.remove("hidden");
  document.querySelector("#name").focus();
});

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
document.querySelector(".next-button").addEventListener("click", async () => {
  if (sessionPhase === "diagnostic" && diagnosticAnswers.length >= DIAGNOSTIC_ITEM_COUNT) {
    await completeDiagnostic();
  } else if (
    sessionPhase === "practice" &&
    isSessionEndingAction(feedbackState?.serverAgentEvent || feedbackState?.localAgentEvent)
  ) {
    await finishSession();
  } else {
    await presentQuestion();
  }
});
document.querySelector("#start-plan-button").addEventListener("click", async () => {
  resetSession("practice");
  await presentQuestion();
});
document.querySelector("#end-session-button").addEventListener("click", finishSession);
document.querySelector("#new-session-button").addEventListener("click", async () => {
  resetSession("practice");
  await presentQuestion();
});

init();
