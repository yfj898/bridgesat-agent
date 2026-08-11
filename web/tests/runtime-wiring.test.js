"use strict";

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const WEB = path.join(__dirname, "..");
const app = fs.readFileSync(path.join(WEB, "app.js"), "utf8");
const core = fs.readFileSync(path.join(WEB, "offline-core.js"), "utf8");
const html = fs.readFileSync(path.join(WEB, "index.html"), "utf8");

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
  assert.match(
    app,
    /nextActionConstraint = \{[\s\S]*\.\.\.\(nextActionConstraint \|\| \{\}\)[\s\S]*\.\.\.\(relevant\.action_payload \|\| \{\}\)/
  );
  assert.match(app, /pickNextQuestion\([\s\S]*nextActionConstraint/);
});

test("memory-aware intervention is visible and traceable", () => {
  assert.ok(html.includes('id="agent-intervention"'));
  assert.match(core, /Based on what helped you before/);
  assert.match(html, /<summary>Why this recommendation\?<\/summary>/);
  assert.match(html, /<details class="technical-evidence">/);
  assert.doesNotMatch(html, /<details class="technical-evidence" open>/);
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
  assert.match(app, /source_answer_event_id: sourceEventId/);
});

test("verified personalized explanation is rendered without replacing the deterministic copy", () => {
  assert.match(core, /event\.personalized_explanation \|\| ""/);
  assert.match(core, /event\.personalized_emphasis \|\| ""/);
  assert.match(app, /view\.personalized[\s\S]{0,40}event\.validated_episode_id/);
  assert.match(app, /personalized: \$\{view\.personalizedEmphasis/);
  assert.match(app, /view\.personalized/);
});

test("offline and reconnect states remain student-visible", () => {
  assert.match(app, /Offline — keep practicing with saved questions/);
  assert.match(app, /Back online — reconnecting/);
  assert.match(app, /Syncing saved progress…/);
  assert.match(app, /Synced — .*no progress lost/);
  assert.match(app, /progress is safe on this device and will retry/);
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

test("time-budget closure is executed by the student UI", () => {
  assert.match(app, /minutesRemaining: remainingMinutes/);
  assert.match(app, /isSessionEndingAction\(relevant\)/);
  assert.match(app, /isSessionEndingAction\(feedbackState\?\.serverAgentEvent/);
  assert.match(app, /await finishSession\(\)/);
  assert.match(app, /"End with review"/);
});
