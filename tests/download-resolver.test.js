"use strict";
const { selectLatestEligibleConnectDownload: select, formatFileSize } = require("../download.js");
const REPO = "https://github.com/RolanDorisTech/rdt-voice/releases/download";
const GOOD_DIGEST = "sha256:" + "a".repeat(64);
function release(tag, overrides = {}) {
  const version = tag.replace("rdt-connect-v", "");
  const name = `RDT_Connect_${version}_aarch64.dmg`;
  return Object.assign({ tag_name: tag, name: "display title is ignored", draft: false,
    published_at: "2026-10-16T00:00:00Z", assets: [{ name, state: "uploaded", size: 371715568,
      digest: GOOD_DIGEST, browser_download_url: `${REPO}/${tag}/${name}` }] }, overrides);
}
const clone = x => JSON.parse(JSON.stringify(x));
const NEWER = "rdt-connect-v0.2.0-dev.11";
const FALLBACK = "rdt-connect-v0.2.0-dev.10";
const fallback = () => release(FALLBACK, { published_at: "2026-10-15T00:00:00Z" });
let total = 0, failed = 0;
function check(label, actual, expected) {
  total++; const ok = actual === expected; if (!ok) failed++;
  console.log(`${ok ? "PASS" : "FAIL"}  ${label}${ok ? "" : ` (got ${String(actual)}, want ${String(expected)})`}`);
}
const tagOf = r => { const x = select(r); return x && x.tag; };
function rejects(label, mutate) {
  const bad = release(NEWER); mutate(bad);
  check(`rejects: ${label}`, tagOf([bad, fallback()]), FALLBACK);
}
const asset = fn => b => fn(b.assets[0]);

// Happy paths and selection policy.
check("selects a valid release", tagOf([release(FALLBACK)]), FALLBACK);
check("returns numeric size, digest and canonical HTTPS URL", (() => {
  const x = select([release(FALLBACK)]);
  return x.size === 371715568 && x.digest === GOOD_DIGEST && x.url === `${REPO}/${FALLBACK}/RDT_Connect_0.2.0-dev.10_aarch64.dmg`;
})(), true);
check("returns the restricted streaming Worker URL", (() => {
  const x = select([release(FALLBACK)]);
  return x.proxyUrl === `https://rdt-connect-download-proxy.rolandoristech.workers.dev/download/${FALLBACK}`;
})(), true);
check("policy: newest published_at wins over higher version number",
  tagOf([release("rdt-connect-v0.2.0-dev.9", { published_at: "2026-10-17T00:00:00Z" }),
         release(FALLBACK, { published_at: "2026-10-16T00:00:00Z" })]), "rdt-connect-v0.2.0-dev.9");
check("ignores display title", tagOf([release(FALLBACK, { name: "RDT Voice 9.9" })]), FALLBACK);
check("accepts prerelease: true (development stream)", tagOf([release(FALLBACK, { prerelease: true })]), FALLBACK);
check("accepts uppercase hex digest", (() => {
  const r = release(FALLBACK); r.assets[0].digest = "sha256:" + "A".repeat(64); return !!select([r]);
})(), true);
check("formats file size without losing exact bytes", formatFileSize(371715568), "354.5 MiB (371,715,568 bytes)");

// Release-level metadata.
rejects("draft: true", b => b.draft = true);
rejects("draft field missing", b => delete b.draft);
rejects("draft: null", b => b.draft = null);
rejects('draft: "false" string', b => b.draft = "false");
rejects("published_at invalid", b => b.published_at = "nope");
rejects("published_at null", b => b.published_at = null);
rejects("published_at missing", b => delete b.published_at);
rejects("published_at is a number", b => b.published_at = 1792000000000);
check("ignores other product's tag (rdt-voice-v…)", tagOf([release("rdt-voice-v0.2.0-dev.11"), fallback()]), FALLBACK);
check("ignores legacy tag format", tagOf([release("v0.2-dev.9")]), null);
check("ignores uppercase tag prefix", tagOf([release("RDT-Connect-v0.2.0-dev.11"), fallback()]), FALLBACK);
check("garbage release skipped", tagOf([[null], release(FALLBACK)]), FALLBACK);

// Asset identity and state.
rejects("no assets", b => b.assets = []);
rejects("assets not an array", b => b.assets = null);
rejects("wrong asset filename", asset(a => a.name = "RDT_Connect_0.2.0-dev.11_x86_64.dmg"));
rejects("asset not uploaded (state: open)", asset(a => a.state = "open"));
rejects("asset state missing", asset(a => delete a.state));
rejects("duplicate matching assets", b => b.assets.push(clone(b.assets[0])));
rejects("duplicate filename with one malformed duplicate", b => b.assets.push({ ...b.assets[0], state: "new" }));

// Asset size must be a positive safe JS integer, not a coercible value.
rejects("size: true", asset(a => a.size = true));
rejects('size: "371715568" string', asset(a => a.size = "371715568"));
rejects("size: 0", asset(a => a.size = 0));
rejects("size: -1", asset(a => a.size = -1));
rejects("size: 1.5 fractional", asset(a => a.size = 1.5));
rejects("size: unsafe integer", asset(a => a.size = Number.MAX_SAFE_INTEGER + 2));
rejects("size: NaN", asset(a => a.size = NaN));
rejects("size: Infinity", asset(a => a.size = Infinity));
rejects("size: null", asset(a => a.size = null));
rejects("size: []", asset(a => a.size = []));
rejects("size missing", asset(a => delete a.size));

// Digest must be a string containing exactly a SHA-256 digest.
rejects("digest missing", asset(a => delete a.digest));
rejects("digest null", asset(a => a.digest = null));
rejects("digest not string (number)", asset(a => a.digest = 12345));
rejects("digest not string (object)", asset(a => a.digest = { toString: () => GOOD_DIGEST }));
rejects("digest wrong algorithm", asset(a => a.digest = "sha1:" + "a".repeat(40)));
rejects("digest too short", asset(a => a.digest = "sha256:" + "a".repeat(63)));
rejects("digest non-hex", asset(a => a.digest = "sha256:" + "g".repeat(64)));
rejects("digest without prefix", asset(a => a.digest = "a".repeat(64)));

// URL must be the exact public asset URL on the canonical host/origin/path.
rejects("url missing", asset(a => delete a.browser_download_url));
rejects("url not a string", asset(a => a.browser_download_url = 42));
rejects("url malformed", asset(a => a.browser_download_url = "::"));
rejects("url empty", asset(a => a.browser_download_url = ""));
rejects("url wrong host", asset(a => a.browser_download_url = a.browser_download_url.replace("github.com", "evil.example")));
rejects("url non-HTTPS", asset(a => a.browser_download_url = a.browser_download_url.replace("https:", "http:")));
rejects("url wrong repository path", asset(a => a.browser_download_url = a.browser_download_url.replace("RolanDorisTech", "Someone")));
rejects("url wrong tag in path", asset(a => a.browser_download_url = a.browser_download_url.replace(NEWER, FALLBACK)));
rejects("url query string", asset(a => a.browser_download_url += "?x=1"));
rejects("url fragment", asset(a => a.browser_download_url += "#x"));
rejects("url nonstandard port", asset(a => a.browser_download_url = a.browser_download_url.replace("github.com/", "github.com:444/")));
rejects("url embedded credentials", asset(a => a.browser_download_url = a.browser_download_url.replace("https://", "https://user@")));

// Input resilience.
check("non-array inputs return null", select({}) === null && select(null) === null && select("x") === null, true);
check("array of non-objects returns null", select([null, 1, "x", undefined]) === null, true);
check("empty array returns null", select([]), null);
check("returns null when no release is eligible", tagOf([release(NEWER, { draft: true })]), null);

console.log(`\n${total} assertions, ${total - failed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
