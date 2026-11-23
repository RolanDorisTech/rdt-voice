import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const connectRoot = path.resolve(scriptDir, "..");
const resourceRoot = path.join(connectRoot, "src-tauri/resources/rdt_voice_web");
const manifestPath = path.join(connectRoot, "src-tauri/resources/rdt_voice_web_manifest.json");
const sourceRoot = process.env.RDT_VOICE_WEB_SOURCE || path.resolve(path.dirname(process.argv[1]), "..", "vendor", "rdt-voice-r82-alpha");

function fail(message) {
  console.error(`Embedded RDT Voice preflight failed: ${message}`);
  process.exit(1);
}

if (!fs.existsSync(resourceRoot) || !fs.existsSync(manifestPath)) {
  fail("embedded resource directory or manifest is missing");
}

const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
const htmlPath = path.join(resourceRoot, "index.html");
const html = fs.readFileSync(htmlPath, "utf8");

for (const name of [
  "index.html", "worker.js", "pcm-worklet.js",
  "models.manifest.json", "rdt-easywhisper-benchmark-core.js"
]) {
  if (!fs.existsSync(path.join(resourceRoot, name))) {
    fail(`required embedded asset missing: ${name}`);
  }
}

const locales = fs.readdirSync(path.join(resourceRoot, "locales"))
  .filter(name => name.toLowerCase().endsWith(".json"));
if (locales.length !== 52 || manifest.alpha_locale_file_count !== 52) {
  fail(`expected 52 embedded locales and manifest records; found ${locales.length}`);
}

for (const marker of [
  "rdt-connect-offline-easywhisper",
  "rdt_connect_native_oom_message",
  "RDT_CONNECT_NATIVE_OOM_MESSAGE",
  "rdtIsNativeServerOutOfMemory",
  "rdtNativeServerOutOfMemoryMessage",
  "RDT_NATIVE_SERVER_OUT_OF_MEMORY",
  "_httpStatus === 503 && /out of memory/i.test(_serverDetail)",
  "RDT_NATIVE_SERVER_OUT_OF_MEMORY",
  "rdtIsNativeServerOutOfMemory",
  "rdtNativeServerOutOfMemoryMessage"
]) {
  if (!html.includes(marker)) fail(`Connect integration marker missing: ${marker}`);
}

const checker = spawnSync(
  process.execPath,
  [path.join(scriptDir, "check-embedded-rdt-voice.mjs")],
  { cwd: connectRoot, env: { ...process.env, RDT_VOICE_WEB_SOURCE: sourceRoot }, stdio: "inherit" }
);
if (checker.error) fail(`could not run embedded resource checker: ${checker.error.message}`);
if (checker.status !== 0) process.exit(checker.status ?? 1);

if (fs.existsSync(sourceRoot)) {
  const sourceLocales = fs.readdirSync(path.join(sourceRoot, "locales"))
    .filter(name => name.toLowerCase().endsWith(".json"));
  if (sourceLocales.length !== 52) {
    fail(`canonical Alpha locale count is ${sourceLocales.length}, expected 52`);
  }
}

console.log(
  `Embedded RDT Voice preflight passed: ${locales.length} locales; ` +
  `${Object.keys(manifest.files || {}).length} manifest files; Connect OOM bridge retained.`
);
