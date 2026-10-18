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

assert.ok(readme.startsWith("<!-- RDT_CONNECT_PROMO_START -->"),
  "RDT Connect promotion must be the very first README content");
const promoStart = "<!-- RDT_CONNECT_PROMO_START -->";
const promoEnd = "<!-- RDT_CONNECT_PROMO_END -->";
assert.equal(readme.split(promoStart).length - 1, 1,
  "README must contain exactly one top promotion");
assert.equal(readme.split(promoEnd).length - 1, 1,
  "README promotion must have one end marker");
const promo = readme.split(promoStart)[1].split(promoEnd)[0];
assert.equal((promo.match(/<div align="left">/g) || []).length, 1,
  "RDT Connect promotion must be left-aligned");
assert.equal(promo.includes('<div align="center">'), false,
  "RDT Connect promotion must not revert to centered alignment");
assert.ok(promo.includes(match[1]),
  "promotional download badge must point at the same verified DMG");
assert.ok(promo.includes("assets/rdt-connect-server-running.png"),
  "promotion must show the RDT Connect screenshot");
assert.ok(promo.includes("Download-RDT%20Connect%20for%20macOS"),
  "promotion must include its prominent download badge");
assert.ok(promo.includes("#rdt-connect-downloads"),
  "promotion must provide a path to installation details");
assert.ok(readme.indexOf(promoStart) < readme.indexOf("Try_Now"),
  "promotion must appear before the existing RDT Voice Try Now badge");
assert.ok(readme.indexOf(promoEnd) < readme.indexOf("See it in action"),
  "promotion must appear before the demo section");
assert.ok(readme.indexOf(promoEnd) < readme.indexOf("assets/rdt-voice-ui.png"),
  "promotion must appear before the original RDT Voice screenshot");

console.log("PASS: README links straight to the published DMG.");
console.log("PASS: main website CTA keeps its direct-download behavior.");
console.log("PASS: dedicated page, proxy source, and associated tests are removed.");
