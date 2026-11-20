import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";

const base = process.cwd();
const root = path.resolve(process.env.RDT_EMBEDDED_WEB_ROOT || path.join(base, "src-tauri/resources/rdt_voice_web"));
const manifestPath = path.resolve(process.env.RDT_EMBEDDED_WEB_MANIFEST || path.join(base, "src-tauri/resources/rdt_voice_web_manifest.json"));
const config = JSON.parse(fs.readFileSync(path.join(base, "src-tauri/tauri.conf.json"), "utf8"));
const resources = config.bundle?.resources;

if (!resources || resources["resources/rdt_voice_web"] !== "rdt_voice_web") {
  throw new Error("Tauri bundle mapping for the offline RDT Voice app is missing.");
}
if (!fs.existsSync(root) || !fs.existsSync(manifestPath)) {
  throw new Error("Embedded RDT Voice resource directory or manifest is missing.");
}

const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
const actual = {};

function hash(file) {
  return crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex");
}

function walk(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.name === ".DS_Store" || entry.name.endsWith(".bak") || entry.name.includes(".bak.")) continue;
    if (entry.isDirectory()) walk(full);
    else if (entry.isFile()) {
      const rel = path.relative(root, full).split(path.sep).join("/");
      actual[rel] = hash(full);
    }
  }
}

for (const required of ["index.html", "worker.js", "rdt-easywhisper-benchmark-core.js"]) {
  if (!fs.existsSync(path.join(root, required))) {
    throw new Error(`Offline RDT Voice bundle is incomplete: ${required}`);
  }
}

walk(root);
const expected = manifest.files || {};
const missing = Object.keys(expected).filter(name => {
  const record = expected[name];
  const digest = typeof record === "string" ? record : record?.sha256;
  return !digest || actual[name] !== digest;
});
const extra = Object.keys(actual).filter(name => expected[name] === undefined);

const alphaSourceRoot = process.env.RDT_VOICE_WEB_SOURCE ||
  path.join(process.env.HOME || process.cwd(), "dev", "rdt-voice", "alpha_testing");

if (fs.existsSync(alphaSourceRoot)) {
  for (const required of ["index.html", "worker.js", "pcm-worklet.js", "models.manifest.json", "locales"]) {
    if (!fs.existsSync(path.join(alphaSourceRoot, required))) {
      throw new Error(`RDT Voice Alpha R82 source is incomplete: ${required}`);
    }
  }

  const alphaIndex = fs.readFileSync(path.join(alphaSourceRoot, "index.html"), "utf8");
  const offlineMarker = '<meta name="rdt-connect-offline-easywhisper" content="disabled">';
  if (alphaIndex.includes(offlineMarker)) {
    throw new Error("Canonical Alpha R82 source must not contain the offline-only marker.");
  }

  const stagedIndex = fs.readFileSync(path.join(root, "index.html"), "utf8");
  const cspTags = [...stagedIndex.matchAll(/<meta\b(?=[^>]*\bhttp-equiv\s*=\s*["']Content-Security-Policy["'])[^>]*>/gi)];
  if (cspTags.length !== 1) {
    throw new Error(`Expected one embedded CSP meta tag; found ${cspTags.length}.`);
  }

  const cspAttr = cspTags[0][0].match(/\bcontent\s*=\s*(["'])([\s\S]*?)\1/i);
  if (!cspAttr) throw new Error("Embedded CSP meta tag has no content attribute.");

  const policy = cspAttr[2].replace(/&(#x[0-9a-f]+|#\d+|amp|quot|apos);/gi, (entity, code) => {
    if (code.toLowerCase() === "amp") return "&";
    if (code.toLowerCase() === "quot") return '"';
    if (code.toLowerCase() === "apos") return "'";
    const value = code[1].toLowerCase() === "x"
      ? parseInt(code.slice(2), 16) : parseInt(code.slice(1), 10);
    return Number.isFinite(value) && value <= 0x10ffff ? String.fromCodePoint(value) : entity;
  });

  const directives = new Map(policy.split(";").map(value => value.trim()).filter(Boolean).map(value => {
    const [name, ...values] = value.split(/\s+/);
    return [name.toLowerCase(), values];
  }));
  const scriptPolicy = directives.get("script-src-elem")
    ?? directives.get("script-src")
    ?? directives.get("default-src")
    ?? [];

  if (scriptPolicy.includes("'unsafe-inline'")) {
    throw new Error("Embedded CSP script policy must not use unsafe-inline.");
  }

  for (const match of stagedIndex.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script\s*>/gi)) {
    if (/\bsrc\s*=/i.test(match[1])) continue;
    const type = match[1].match(/\btype\s*=\s*["']([^"']+)["']/i)?.[1]?.toLowerCase();
    if (type && ["application/json", "application/ld+json", "text/template"].includes(type)) continue;
    const digest = crypto.createHash("sha256").update(match[2], "utf8").digest("base64");
    if (!scriptPolicy.includes(`'sha256-${digest}'`)) {
      const line = stagedIndex.slice(0, match.index).split("\n").length;
      throw new Error(`Embedded CSP missing hash for inline script at line ${line}: sha256-${digest}.`);
    }
  }
  console.log("Embedded CSP inline-script hashes: PASS; unsafe-inline absent.");

  if (stagedIndex.split(offlineMarker).length - 1 !== 1) {
    throw new Error("Embedded offline index must contain exactly one EasyWhisper disable marker.");
  }

  for (const guard of [
    "RDT_CONNECT_EASYWHISPER_DISABLED",
    'setEasySetupChoice("server")',
    'easySetupPath === "easy"',
    "rolandoristech.github.io/rdt-voice/"
  ]) {
    if (!stagedIndex.includes(guard)) {
      throw new Error(`Embedded offline EasyWhisper protection is missing: ${guard}`);
    }
  }

  const alpha = {};
  function walkAlpha(dir) {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      if (
        entry.name === ".DS_Store" ||
        entry.name.endsWith(".bak") ||
        entry.name.includes(".bak.") ||
        (entry.isDirectory() && /^\.private-browsing-i18n-backup-\d{8}-\d{6}$/.test(entry.name))
      ) continue;

      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walkAlpha(full);
      else if (entry.isFile()) {
        const rel = path.relative(alphaSourceRoot, full).split(path.sep).join("/");
        alpha[rel] = hash(full);
      }
    }
  }
  walkAlpha(alphaSourceRoot);

  const alphaSourceFiles = manifest.alpha_source_files || {};
  const transformed = manifest.transformed_files || {};
  const allowedTransforms = new Set(["worker.js", "index.html"]);
  const missingTransforms = ["worker.js", "index.html"].filter(
    name => !Object.prototype.hasOwnProperty.call(transformed, name)
  );
  const unsupportedTransforms = Object.keys(transformed).filter(
    name => !allowedTransforms.has(name)
  );

  const alphaMismatch = Object.keys(alpha).filter(name => {
    const sourceRecord = alphaSourceFiles[name];
    const sourceDigest = typeof sourceRecord === "string"
      ? sourceRecord
      : sourceRecord?.sha256 || sourceRecord?.source_sha256;
    if (sourceDigest !== alpha[name]) return true;

    const transform = transformed[name];
    if (transform) {
      const expectedTransform = name === "worker.js"
        ? "rdt-offline-runtime-worker-v1"
        : "rdt-connect-offline-easywhisper-v2";
      return transform.transform !== expectedTransform ||
        transform.source_sha256 !== alpha[name] ||
        transform.staged_sha256 !== actual[name] ||
        expected[name] !== actual[name];
    }
    return actual[name] !== alpha[name] || expected[name] !== alpha[name];
  });

  if (
    missingTransforms.length ||
    unsupportedTransforms.length ||
    Object.keys(alphaSourceFiles).length !== Object.keys(alpha).length ||
    alphaMismatch.length
  ) {
    throw new Error(
      `RDT Voice Alpha source/embedded hash mismatch: ${alphaMismatch.slice(0, 8).join(", ")}; ` +
      `unsupported transforms: ${unsupportedTransforms.join(", ")}`
    );
  }

  console.log(`RDT Voice Alpha R82 source: PASS — ${Object.keys(alpha).length} source files match embedded hashes.`);
} else if (process.env.RDT_VOICE_WEB_SOURCE) {
  throw new Error(`Configured RDT_VOICE_WEB_SOURCE does not exist: ${alphaSourceRoot}`);
} else {
  console.log("RDT Voice Alpha source cross-check: SKIP — source checkout not present; embedded manifest remains enforced.");
}

const connectSupport = manifest.connect_support_files || {};
const supportMismatch = Object.entries(connectSupport).filter(
  ([name, digest]) => actual[name] !== digest || expected[name] !== digest
);
if (
  Object.keys(connectSupport).length !== manifest.connect_support_file_count ||
  supportMismatch.length
) {
  throw new Error(
    `Connect support asset manifest mismatch: ${supportMismatch.map(([name]) => name).join(", ")}`
  );
}

if (
  missing.length ||
  extra.length ||
  Object.keys(actual).length !== manifest.file_count
) {
  throw new Error(
    `Embedded RDT Voice hash mismatch. Missing/changed: ${missing.slice(0, 8)}; extra: ${extra.slice(0, 8)}`
  );
}

console.log(`RDT Voice ${manifest.bundle_version}: PASS — ${Object.keys(actual).length} bundled files verified.`);
console.log("Tauri resource mapping: PASS — included in desktop bundle.");
