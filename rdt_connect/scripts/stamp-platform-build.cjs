"use strict";

// Stamp a disposable CI worktree for one platform without changing the shared
// checked-in product version used by other platform source builds.
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.resolve(process.env.RDT_STAMP_ROOT || path.resolve(__dirname, ".."));
const MARKERS = {
  macos: { file: "build-id-sequence.txt", allowed: /^1[A-Z]\d{2}$/ },
  windows: { file: "build-id-sequence-windows.txt", allowed: /^[23][A-Z]\d{2}$/ },
  linux: { file: "build-id-sequence-linux.txt", allowed: /^[45]L\d{3}$/ },
};

function die(message) {
  console.error(`ERROR: ${message}`);
  process.exit(2);
}

function readJson(file) {
  return JSON.parse(fs.readFileSync(path.join(ROOT, file), "utf8"));
}
function writeJson(file, value) {
  const target = path.join(ROOT, file);
  const temporary = `${target}.tmp-${process.pid}`;
  fs.writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, { flag: "wx" });
  fs.renameSync(temporary, target);
}
function atomicWrite(file, value) {
  const target = path.join(ROOT, file);
  const temporary = `${target}.tmp-${process.pid}`;
  fs.writeFileSync(temporary, value, { flag: "wx" });
  fs.renameSync(temporary, target);
}
function cargoTomlPackageSection(text) {
  const heading = /^\[package\]\s*\n/m.exec(text);
  if (!heading) throw new Error("Cargo.toml has no [package] section");
  const contentStart = heading.index + heading[0].length;
  const nextSection = text.indexOf("\n[", contentStart);
  return {
    start: contentStart,
    end: nextSection < 0 ? text.length : nextSection + 1,
    content: text.slice(contentStart, nextSection < 0 ? text.length : nextSection + 1),
  };
}
function cargoTomlVersion(text) {
  const section = cargoTomlPackageSection(text);
  const found = section.content.match(/^version\s*=\s*"([^"]+)"\s*$/m);
  if (!found) throw new Error("Cargo.toml [package] has no version");
  return found[1];
}
function replaceCargoTomlVersion(text, version) {
  const section = cargoTomlPackageSection(text);
  const replaced = section.content.replace(/^(version\s*=\s*)"[^"]+"\s*$/m, `$1"${version}"`);
  return text.slice(0, section.start) + replaced + text.slice(section.end);
}
function cargoLockPackageVersion(text) {
  const match = text.match(/^\[\[package\]\]\s*\nname = "rdt-connect-desktop"\s*\nversion = "([^"]+)"/m);
  if (!match) throw new Error("Cargo.lock has no rdt-connect-desktop package record");
  return match[1];
}
function replaceCargoLockPackageVersion(text, version) {
  const pattern = /(^\[\[package\]\]\s*\nname = "rdt-connect-desktop"\s*\nversion = ")[^"]+("\s*\n)/m;
  if (!pattern.test(text)) throw new Error("Cargo.lock has no rdt-connect-desktop package record");
  return text.replace(pattern, `$1${version}$2`);
}
function baseVersion(version) {
  // Support legacy versions (... .2A.5) and whole-marker versions
  // (... .2A05). The build marker remains one indivisible identifier.
  const legacy = /^(.*)\.([0-9][A-Z])\.(0|[1-9][0-9]*)$/.exec(version);
  const wholeMarker = /^(.*)\.((?:[1-3][A-Z][0-9]{2}|[45]L[0-9]{3}))$/.exec(version);
  const match = legacy || wholeMarker;
  if (!match ||
      !/^\d+\.\d+\.\d+-(?:[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)$/.test(match[1])) {
    throw new Error(`Cannot safely decode the existing build version: ${version}`);
  }
  return match[1];
}

function stampedVersion(base, marker) {
  // Keep the complete marker, including its leading zero, together.
  return `${base}.${marker}`;
}

function stamp(platform, requestedMarker = null) {
  const policy = MARKERS[platform];
  if (!policy) throw new Error(`Unsupported platform '${platform}'; choose macos, windows, or linux`);
  const markerPath = path.join(ROOT, "scripts", policy.file);
  const marker = requestedMarker ?? fs.readFileSync(markerPath, "utf8").trim();
  if (!policy.allowed.test(marker)) {
    throw new Error(`${markerPath} contains '${marker}', which is outside the reserved ${platform} marker namespace`);
  }
  const pkg = readJson("package.json");
  const lock = readJson("package-lock.json");
  const tauri = readJson("src-tauri/tauri.conf.json");
  const cargoPath = path.join(ROOT, "src-tauri/Cargo.toml");
  const cargoLockPath = path.join(ROOT, "src-tauri/Cargo.lock");
  const cargoText = fs.readFileSync(cargoPath, "utf8");
  const cargoLockText = fs.readFileSync(cargoLockPath, "utf8");
  const versions = [
    ["package.json", pkg.version],
    ["package-lock.json", lock.version],
    ["package-lock.json root package", lock.packages?.[""]?.version],
    ["tauri.conf.json", tauri.version],
    ["Cargo.toml", cargoTomlVersion(cargoText)],
    ["Cargo.lock", cargoLockPackageVersion(cargoLockText)],
  ];
  const unique = [...new Set(versions.map(([, value]) => value))];
  if (unique.length !== 1 || versions.some(([, value]) => typeof value !== "string" || !value)) {
    throw new Error(`Version manifests disagree before platform stamping: ${versions.map(([name, value]) => `${name}=${value}`).join(", ")}`);
  }
  const priorVersion = unique[0];
  const nextVersion = stampedVersion(baseVersion(priorVersion), marker);
  pkg.version = nextVersion;
  lock.version = nextVersion;
  if (!lock.packages || !lock.packages[""]) throw new Error("package-lock.json root package record is missing");
  lock.packages[""].version = nextVersion;
  tauri.version = nextVersion;
  const nextCargo = replaceCargoTomlVersion(cargoText, nextVersion);
  const nextCargoLock = replaceCargoLockPackageVersion(cargoLockText, nextVersion);

  // Prepare and validate every output before replacing any source file.
  if (cargoTomlVersion(nextCargo) !== nextVersion || cargoLockPackageVersion(nextCargoLock) !== nextVersion) {
    throw new Error("Version stamping did not produce consistent Rust manifests");
  }
  writeJson("package.json", pkg);
  writeJson("package-lock.json", lock);
  writeJson("src-tauri/tauri.conf.json", tauri);
  atomicWrite("src-tauri/Cargo.toml", nextCargo);
  atomicWrite("src-tauri/Cargo.lock", nextCargoLock);
  if (process.env.GITHUB_ENV) {
    fs.appendFileSync(process.env.GITHUB_ENV, `RDT_BUILD_ID=${marker}\n`, { encoding: "utf8" });
  }
  console.log(`PLATFORM_BUILD_STAMP platform=${platform} marker=${marker} version=${nextVersion}`);
  console.log(`RDT_BUILD_ID=${marker}`);
  return { platform, marker, priorVersion, version: nextVersion };
}

if (require.main === module) {
  try {
    if (process.argv.length < 3 || process.argv.length > 4) throw new Error("Usage: node scripts/stamp-platform-build.cjs <macos|windows|linux> [MARKER]");
    stamp(process.argv[2], process.argv[3] ?? null);
  } catch (error) {
    die(error instanceof Error ? error.message : String(error));
  }
}

module.exports = { stamp, baseVersion, stampedVersion, MARKERS };
