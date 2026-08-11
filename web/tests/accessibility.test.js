"use strict";

// Accessibility core-path checks for the BridgeSAT PWA (EVALUATION_SPEC
// section 9, API_AND_OPERATIONS section 11). Static inspection of the
// shipped HTML/CSS: accessible names, announced status, visible focus,
// touch target size, no color-only status, no motion-critical animation.
// Contrast, 200% zoom, and screen-reader behavior remain manual checks.

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const WEB = path.join(__dirname, "..");
const html = fs.readFileSync(path.join(WEB, "index.html"), "utf8");
const css = fs.readFileSync(path.join(WEB, "styles.css"), "utf8");

test("network and sync state are announced with role=status", () => {
  const statuses = [...html.matchAll(/role="status"/g)];
  assert.ok(statuses.length >= 2, "expected at least 2 role=status elements");
  for (const text of ["network-status", "sync-status"]) {
    assert.ok(html.includes(`id="${text}"`), `${text} element exists`);
  }
});

test("cards have accessible names via aria-labelledby", () => {
  const labelled = [...html.matchAll(/aria-labelledby="([^"]+)"/g)].map(
    (m) => m[1]
  );
  assert.ok(labelled.length >= 3, "at least three aria-labelledby cards");
  for (const id of labelled) {
    assert.ok(html.includes(`id="${id}"`), `label target ${id} exists`);
  }
});

test("visible focus indicator is defined", () => {
  assert.ok(
    /:focus-visible\s*\{/.test(css),
    "styles.css defines :focus-visible rules"
  );
});

test("touch targets are at least 44px tall", () => {
  const matches = [...css.matchAll(/min-height:\s*([\d.]+)px/g)];
  assert.ok(matches.length > 0, "a min-height rule exists");
  assert.ok(
    matches.every((m) => parseFloat(m[1]) >= 44),
    `all min-heights >= 44px (got ${matches.map((m) => m[1]).join(", ")})`
  );
});

test("no required information is conveyed by color alone", () => {
  const statusDivs = [...html.matchAll(/id="([^"]*(?:status|state)[^"]*)"[^>]*class="([^"]*)"/g)];
  for (const [, id, cls] of statusDivs) {
    const open = html.indexOf(`id="${id}"`);
    const close = html.indexOf("</div>", open);
    const content = html.slice(open, close);
    assert.ok(
      content.length > `id="${id}"`.length + 10,
      `${id} carries text content`
    );
    assert.ok(
      !/aria-hidden/.test(content),
      `${id} is not hidden from assistive technology`
    );
  }
});

test("no motion-critical animation without a reduced-motion escape", () => {
  const hasKeyframes = /@keyframes/.test(css);
  const hasReducedMotion = /prefers-reduced-motion/.test(css);
  assert.ok(
    !hasKeyframes || hasReducedMotion,
    "any animation must respect prefers-reduced-motion"
  );
});

test("progress and error text is learner-facing and neutral", () => {
  assert.ok(html.includes("Checking connection"));
  assert.ok(/Checking connection…/.test(html));
});
