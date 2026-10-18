import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";

const readme = readFileSync("README.md", "utf8");
const index = readFileSync("index.html", "utf8");

const rows = readme.split(/\r?\n/)
  .filter(line => line.startsWith("| macOS (Apple Silicon) |"));
assert.equal(rows.length, 1, "README must have exactly one macOS download row");

const columns = rows[0].split("|");
const version = columns[3].trim().replaceAll(String.fromCharCode(96), "");
const match = rows[0].match(
  /\[\*\*Download \(\.dmg\)\*\*\]\((https:\/\/github\.com\/RolanDorisTech\/rdt-voice\/releases\/download\/([^/]+)\/([^)]+))\)/
);

assert.ok(match, "README button must directly link to a GitHub Release DMG");
assert.equal(match[2], `rdt-connect-v${version}`, "release tag must match the README version");
assert.equal(match[3], `RDT_Connect_${version}_aarch64.dmg`,
  "DMG filename must match the README version");
assert.equal(readme.includes("download.html"), false,
  "README must not route through the retired download page");
assert.match(index, /window\.location\.assign\(downloadUrl\)/,
  "main website CTA must preserve direct navigation to the resolved release asset");

for (const file of [
  "download.html",
  "download.js",
  "cloudflare/rdt-connect-download-proxy.mjs",
  "cloudflare/README-rdt-connect-download-proxy.md",
  "tests/download-page.browser.test.py",
  "tests/download-resolver.test.js",
  "tests/test_download_proxy_worker.mjs",
]) {
  assert.equal(existsSync(file), false, `${file} should be removed`);
}

console.log("PASS: README links straight to the published DMG.");
console.log("PASS: main website CTA keeps its direct-download behavior.");
console.log("PASS: dedicated page, proxy source, and associated tests are removed.");
