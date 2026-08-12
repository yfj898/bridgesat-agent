"use strict";

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const WEB = path.join(__dirname, "..");
const app = fs.readFileSync(path.join(WEB, "app.js"), "utf8");
const core = fs.readFileSync(path.join(WEB, "offline-core.js"), "utf8");
const html = fs.readFileSync(path.join(WEB, "index.html"), "utf8");

test("student promise leads the initial PWA markup before technical evidence", () => {
  const studentPromise = html.indexOf("Every student deserves a tutor");
  const welcomeStart = html.indexOf('id="welcome-card"');
  const technicalEvidenceStart = html.indexOf(
    '<details class="technical-evidence">'
  );

  assert.ok(studentPromise >= 0, "header promise exists in initial markup");
  assert.ok(welcomeStart >= 0, "welcome card exists in initial markup");
  assert.ok(technicalEvidenceStart >= 0, "technical evidence exists in initial markup");
  assert.ok(
    studentPromise < welcomeStart,
    "header promise precedes the welcome card in initial markup"
  );
  assert.ok(
    welcomeStart < technicalEvidenceStart,
    "welcome card precedes technical evidence in initial markup"
  );
});

test("lightweight student home introduces content without bypassing diagnostic", () => {
  assert.ok(html.includes('id="welcome-card"'));
  assert.match(html, /id="catalog-question-count">103<\/strong> practice items/);
  assert.match(html, /id="catalog-lesson-count">24<\/strong> adaptive lessons/);
  assert.match(html, /id="catalog-skill-count">8<\/strong> skill areas/);
  for (const skill of [
    "Linear equations",
    "Systems of equations",
    "Ratios &amp; percentages",
    "Functions &amp; models",
    "Inequalities",
    "Quadratic equations",
    "Exponents &amp; radicals",
    "Coordinate geometry",
  ]) {
    assert.ok(html.includes(skill));
  }
  assert.match(html, /id="start-diagnostic-button"/);
  assert.match(html, /id="profile-card" class="card hidden"/);
  assert.match(app, /welcomeCard\.classList\.add\("hidden"\)/);
  assert.match(app, /profileCard\.classList\.remove\("hidden"\)/);
  assert.match(app, /resetSession\("diagnostic"\)/);
  assert.match(app, /summarizePackCatalog\(contentPack\)/);
  assert.match(app, /latestPackVersion\(listing\.packs\)/);
});

test("PWA consumes server agent events returned by sync", () => {
  assert.match(app, /consumeAgentEvents\(result\.body\.server_events \|\| \[\]\)/);
  assert.match(app, /consumeAgentEvents\(strategyMemory\.recent_agent_events \|\| \[\]\)/);
  assert.match(app, /renderAgentIntervention\(relevant\)/);
  assert.match(app, /selectRelevantAgentEvent\(events, expectedSourceEventId\)/);
  assert.match(core, /event\.source_event_id === expectedSourceEventId/);
  assert.match(core, /event\.source_event_id === expectedSourceEventId/);
  assert.match(app, /feedbackState\.serverAgentEvent\.hybrid_ranked/);
  assert.match(
    app,
    /nextActionConstraint = \{[\s\S]*\.\.\.\(nextActionConstraint \|\| \{\}\)[\s\S]*\.\.\.\(relevant\.action_payload \|\| \{\}\)/
  );
  assert.match(app, /pickNextQuestion\([\s\S]*nextActionConstraint/);
});

test("snapshot replay cannot downgrade a live validated-episode event", () => {
  assert.match(
    app,
    /feedbackState\.serverAgentEvent\.validated_episode_id[\s\S]{0,120}!relevant\.validated_episode_id[\s\S]{0,40}return;/
  );
});

test("consumeAgentEvents reconstructs a teaching transfer constraint", () => {
  const start = app.indexOf("function consumeAgentEvents");
  const end = app.indexOf("async function completeDiagnostic", start);
  assert.ok(start >= 0 && end > start, "consumeAgentEvents source exists");
  const consumeAgentEvents = app.slice(start, end);

  assert.ok(consumeAgentEvents.includes("relevant.action_payload"));
  assert.ok(consumeAgentEvents.includes("isTeachingAction(relevant)"));
  assert.ok(consumeAgentEvents.includes("currentQuestion.author_metadata.transfer_group"));
  assert.ok(consumeAgentEvents.includes("transfer_group"));
  assert.ok(consumeAgentEvents.includes('instruction_role: "transfer"'));
});

test("attemptSync reports rejected progress without promising automatic updates", () => {
  const start = app.indexOf("async function attemptSync()");
  const end = app.indexOf("function seedSkillStates", start);
  assert.ok(start >= 0 && end > start, "attemptSync source exists");
  const attemptSync = app.slice(start, end);

  assert.match(
    attemptSync,
    /const result = await client\.sync\(\);[\s\S]*await refreshPendingCount\(\);/
  );
  assert.ok(
    attemptSync.includes(
      "Some progress could not be saved yet. Keep learning; recent work remains on this device."
    )
  );
  assert.doesNotMatch(
    attemptSync,
    /will update automatically when the connection returns\./
  );
});

test("attemptSync chooses truthful status after its final pending refresh", () => {
  const start = app.indexOf("async function attemptSync()");
  const end = app.indexOf("function seedSkillStates", start);
  assert.ok(start >= 0 && end > start, "attemptSync source exists");
  const attemptSync = app.slice(start, end);
  const syncStart = attemptSync.indexOf("const result = await client.sync();");
  const tryEnd = attemptSync.indexOf("  } catch", syncStart);
  const syncAttempt = attemptSync.slice(syncStart, tryEnd);
  const finalRefresh = syncAttempt.lastIndexOf("await refreshPendingCount();");
  assert.ok(syncStart >= 0, "client sync result exists");
  assert.ok(tryEnd > syncStart, "attemptSync try block exists");
  assert.ok(finalRefresh > syncStart, "final pending refresh follows client sync");
  const postSyncStatus = syncAttempt.slice(finalRefresh);

  assert.match(
    postSyncStatus,
    /await refreshPendingCount\(\);\s*if \(failedEventTotal > 0\)/
  );
  assert.ok(
    postSyncStatus.includes(
      "Some progress could not be saved yet. Keep learning; recent work remains on this device."
    )
  );
  assert.match(
    postSyncStatus,
    /else if \(result\.reason === "empty" && pendingTotal > 0\)/
  );
  assert.ok(
    postSyncStatus.includes(
      "Some saved progress needs another try. Keep learning; nothing has been lost."
    )
  );
  assert.ok(
    postSyncStatus.includes("Your recent learning progress is saved.")
  );
});

test("memory-aware intervention is visible and traceable", () => {
  assert.ok(html.includes('id="agent-intervention"'));
  assert.match(core, /This helped you before/);
  assert.match(html, /<summary>Why\?<\/summary>/);
  assert.match(html, /<details class="technical-evidence">/);
  assert.doesNotMatch(html, /<details class="technical-evidence" open>/);
  assert.match(html, /<summary>Learning record details<\/summary>/);
  assert.match(app, /details\.open = false/);
  assert.match(core, /RECALLED_SUCCESSFUL_EPISODE/);
  assert.match(app, /view\.reasonCode, view\.policyVersion/);
  assert.match(app, /Validated Episode/);
  assert.match(app, /content gate: \$\{lesson\.review_status\}/);
  assert.match(app, /simulated reviewers/);
  assert.match(app, /license: \$\{lesson\.license/);
  assert.match(app, /source: \$\{lesson\.source_lineage/);
  assert.match(app, /entry\.id === event\.action_payload\?\.content_id/);
  assert.match(app, /"WORKED_EXAMPLE_PRESENTED"/);
  assert.match(app, /"MICRO_LESSON_PRESENTED"/);
  assert.match(app, /source_answer_event_id: sourceEventId/);
  assert.match(app, /const answeredWhileOnline = navigator\.onLine/);
  assert.match(app, /if \(!answeredWhileOnline\) \{\s*await recordPresentedIntervention/);
});

test("verified personalized explanation is rendered without replacing the deterministic copy", () => {
  assert.match(core, /event\.personalized_explanation \|\| ""/);
  assert.match(core, /event\.personalized_emphasis \|\| ""/);
  assert.match(app, /view\.personalized[\s\S]{0,40}event\.validated_episode_id/);
  assert.match(app, /personalized: \$\{view\.personalizedEmphasis/);
  assert.doesNotMatch(app, /view\.personalized \|\|/);
});

test("correct feedback retains the worked explanation", () => {
  assert.match(app, /state\.correct[\s\S]*item\.worked_explanation/);
});

test("finishSession renders a verified sync summary and keeps deterministic fallback", () => {
  const start = app.indexOf("async function finishSession()");
  const end = app.indexOf("async function upgradeInstalledPackWhenIdle", start);
  assert.ok(start >= 0 && end > start, "finishSession source exists");
  const finishSession = app.slice(start, end);

  assert.match(finishSession, /const synced = await attemptSync\(\);/);
  assert.match(
    finishSession,
    /Array\.isArray\(personalizedSummaries\)/
  );
  assert.match(
    finishSession,
    /typeof synced\?\.body\?\.personalized_summary === "string"/
  );
  assert.match(finishSession, /String\(currentSummary \|\| legacySummary\)\.trim\(\)/);
  assert.match(finishSession, /entry\.source_event_id === completionRecord\?\.id/);
  assert.match(finishSession, /entry\.session_id === sessionId/);
  assert.match(
    finishSession,
    /if \(verifiedSummary\) \{\s*summaryText\.textContent = `\$\{verifiedSummary\} \$\{syncStatusText\}`;\s*\} else \{/s
  );
  assert.match(
    finishSession,
    /const memorySummary = episodes\.length > 0[\s\S]*summaryText\.textContent = `Session complete\./
  );
  assert.match(
    finishSession,
    /failedEventTotal > 0[\s\S]*Some progress could not be saved yet\. Keep learning; recent work remains on this device\./
  );
  assert.match(app, /let failedEventTotal = 0;/);
  assert.match(app, /failedEventTotal = records\.filter\(\(record\) => record\.status === "failed"\)\.length;/);
  assert.match(
    finishSession,
    /pending > 0[\s\S]*Your progress is saved on this device and will save when the connection returns\./
  );
  assert.doesNotMatch(
    finishSession,
    /will update automatically when the connection returns\./
  );
  assert.match(finishSession, /: "Your progress is saved\.";/);
});

test("offline and reconnect states remain student-visible", () => {
  assert.match(app, /you can keep learning/);
  assert.match(app, /save automatically/);
  assert.match(app, /You're connected again\. Saving the progress from this session/);
  assert.match(app, /Saving your recent learning progress…/);
  assert.match(app, /progress is safe on this device/);
  assert.doesNotMatch(app, /Sync paused/);
  assert.doesNotMatch(app, /\$\{pending\} update/);
});

test("student-facing practice rendering uses qualitative learning language", () => {
  assert.ok(html.includes("Your SAT study partner"));
  assert.ok(
    html.includes("Start with two questions. I'll find the best place to begin.")
  );
  assert.ok(
    html.includes(
      "BridgeSAT notices stuck points, chooses the next step, and remembers what worked."
    )
  );
  assert.doesNotMatch(html, /Pending sync:/);
  assert.doesNotMatch(html, /id="pending-count"/);
  assert.match(app, /function studentSkillLabel\(skill\)/);
  assert.match(app, /function studentProgressLabel\(state\)/);
  assert.match(app, /value\.textContent = studentProgressLabel\(state\)/);
  assert.doesNotMatch(app, /value\.textContent = `\$\{mastery\}%`/);
  assert.match(
    app,
    /Try one more similar problem so I can see whether the same step is getting in the way\./
  );
  assert.match(app, /isTeachingAction\(agentEvent\)[\s\S]{0,120}"Try a new problem"/);
  assert.match(app, /"This approach worked on a new problem\."/);
  assert.doesNotMatch(app, /This answer suggests \$\{state\.misconception/);
  assert.match(app, /transfer_group: item\.author_metadata\.transfer_group/);
  assert.match(app, /instruction_role: "transfer"/);
});

test("active session is persisted and restored through IndexedDB", () => {
  assert.match(app, /put\(\s*"active_session"/);
  assert.match(app, /get\("active_session", "state"\)/);
  assert.match(app, /restoreActiveSessionSnapshot\(activeRecord\)/);
});

test("PWA competition path posts only sync; /v1/adapt is never called", () => {
  assert.match(app, /\/v1\/sync\/devices/);
  assert.match(core, /\/v1\/sync\/events/);
  assert.doesNotMatch(app, /\/v1\/adapt/);
  assert.doesNotMatch(core, /\/v1\/adapt/);
});

test("practice sessions do not enqueue an unsupported SESSION_STARTED event", () => {
  const start = app.indexOf("async function presentQuestion()");
  const end = app.indexOf("async function showHint()", start);
  assert.ok(start >= 0 && end > start, "presentQuestion source exists");
  const presentQuestion = app.slice(start, end);

  assert.match(
    presentQuestion,
    /if \(sessionPhase === "diagnostic"\) \{\s*await enqueueEvent\("DIAGNOSTIC_STARTED"/
  );
  assert.doesNotMatch(presentQuestion, /"SESSION_STARTED"/);
  assert.match(presentQuestion, /await enqueueEvent\("CONTENT_PRESENTED"/);
});

test("time-budget closure is executed by the student UI", () => {
  assert.match(app, /minutesRemaining: remainingMinutes/);
  assert.match(app, /isSessionEndingAction\(relevant\)/);
  assert.match(app, /isSessionEndingAction\(feedbackState\?\.serverAgentEvent/);
  assert.match(app, /await finishSession\(\)/);
  assert.match(app, /"Finish with a review"/);
});
