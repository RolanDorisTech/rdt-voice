"use strict";

const fs = require("node:fs");

function fail(message) {
  console.error(`FAIL: ${message}`);
  process.exit(1);
}

const main = fs.readFileSync("src/main.ts", "utf8");
const html = fs.readFileSync("index.html", "utf8");

if (main.includes("HANDOFF Item 16")) {
  fail("handoff text was injected into src/main.ts");
}

if (html.includes("0D53") || html.includes("0.2.0-dev.10")) {
  fail("obsolete 0D53/dev.10 About fallback remains in index.html");
}

const aboutFallback = '<span id="about-version-label" class="eyebrow">RDT Connect</span>';
if (!html.includes(aboutFallback)) {
  fail("About version label must use the neutral RDT Connect fallback");
}

const htmlIds = new Set(
  [...html.matchAll(/\bid\s*=\s*["']([^"']+)["']/g)].map(match => match[1])
);

const required = [
  ["header-check-updates-button", '$("header-check-updates-button")'],
  ["about-check-updates-button", '$("about-check-updates-button")'],
  ["header-update-message", null],
  ["application-update-panel", null],
];

for (const [id, source] of required) {
  if (!htmlIds.has(id)) fail(`required HTML ID is missing: ${id}`);
  if (source && !main.includes(source)) {
    fail(`runtime lookup is missing or changed: ${id}`);
  }
}

const lookups = [
  ...main.matchAll(/\$\(\s*["']([^"']+)["']\s*\)/g)
].map(match => match[1]);

const missing = [...new Set(lookups.filter(id => !htmlIds.has(id)))];
if (missing.length) {
  fail(`static DOM lookups missing from index.html: ${missing.join(", ")}`);
}

console.log("PASS: no injected handoff text");
console.log("PASS: obsolete 0D53/dev.10 fallback absent");
console.log("PASS: About uses neutral runtime fallback");
console.log("PASS: current update controls and associated panels exist");
console.log(`PASS: all ${lookups.length} static DOM lookups match index.html`);
