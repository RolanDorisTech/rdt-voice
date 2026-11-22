"use strict";

const assert = require("assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { syncBuildVersion } = require("./sync-build-version.cjs");

const EXPECTED = "0.2.0-dev.12.1D44";
const temporaryRoot = fs.mkdtempSync(path.join(os.tmpdir(), "rdt-build-version-sync-"));

function write(file, value) {
  const destination = path.join(temporaryRoot, file);
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  fs.writeFileSync(destination, value);
}

function fixture(lockName = "rdt-connect") {
  write("scripts/build-id-sequence.txt", "1D44\n");
  write("package.json", JSON.stringify({
    name: "rdt-connect",
    version: "0.2.0-dev.12.1D43",
    scripts: {}
  }, null, 2) + "\n");
  write("package-lock.json", JSON.stringify({
    name: "rdt-connect",
    version: "0.2.0-dev.11.1A.10",
    lockfileVersion: 3,
    requires: true,
    packages: {
      "": { name: lockName, version: "0.2.0-dev.11.1A.10" },
      "node_modules/example": { version: "1.2.3" }
    }
  }, null, 2) + "\n");
  write("src-tauri/tauri.conf.json", JSON.stringify({
    productName: "RDT Connect",
    version: "0.2.0-dev.12.1D43"
  }, null, 2) + "\n");
  write("src-tauri/Cargo.toml",
    '[package]\nname = "rdt-connect-desktop"\nversion = "0.2.0-dev.12.1D43"\nedition = "2021"\n\n[dependencies]\nserde = "1"\n'
  );
  write("src-tauri/Cargo.lock",
    'version = 4\n\n[[package]]\nname = "rdt-connect-desktop"\nversion = "0.2.0-dev.12.1D43"\n\n[[package]]\nname = "example"\nversion = "1.2.3"\n'
  );
}

function json(file) {
  return JSON.parse(fs.readFileSync(path.join(temporaryRoot, file), "utf8"));
}

try {
  fixture();
  const first = syncBuildVersion(temporaryRoot);
  assert.equal(first.version, EXPECTED);
  assert.equal(first.marker, "1D44");
  assert.equal(first.changedFiles.length, 5);
  assert.equal(json("package.json").version, EXPECTED);
  assert.equal(json("package-lock.json").version, EXPECTED);
  assert.equal(json("package-lock.json").packages[""].version, EXPECTED);
  assert.equal(json("package-lock.json").packages["node_modules/example"].version, "1.2.3");
  assert.equal(json("src-tauri/tauri.conf.json").version, EXPECTED);
  assert.match(fs.readFileSync(path.join(temporaryRoot, "src-tauri/Cargo.toml"), "utf8"),
    new RegExp(`^version = "${EXPECTED.replaceAll(".", "\\.")}"$`, "m"));
  assert.match(fs.readFileSync(path.join(temporaryRoot, "src-tauri/Cargo.lock"), "utf8"),
    new RegExp(`^version = "${EXPECTED.replaceAll(".", "\\.")}"$`, "m"));

  const backupsBefore = fs.readdirSync(temporaryRoot)
    .concat(fs.readdirSync(path.join(temporaryRoot, "src-tauri")))
    .filter(name => name.includes(".bak-build-version-sync-")).length;
  assert.equal(backupsBefore, 5);

  const second = syncBuildVersion(temporaryRoot);
  assert.equal(second.changedFiles.length, 0);
  const backupsAfter = fs.readdirSync(temporaryRoot)
    .concat(fs.readdirSync(path.join(temporaryRoot, "src-tauri")))
    .filter(name => name.includes(".bak-build-version-sync-")).length;
  assert.equal(backupsAfter, backupsBefore);

  // A mismatched npm package identity must fail before modifying files.
  const badRoot = fs.mkdtempSync(path.join(os.tmpdir(), "rdt-build-version-bad-"));
  try {
    const originalRoot = temporaryRoot;
    fixture();
    const packageBefore = fs.readFileSync(path.join(originalRoot, "package.json"), "utf8");
    const badLock = json("package-lock.json");
    badLock.packages[""].name = "wrong-package";
    write("package-lock.json", JSON.stringify(badLock, null, 2) + "\n");
    assert.throws(() => syncBuildVersion(originalRoot), /root package name/);
    assert.equal(fs.readFileSync(path.join(originalRoot, "package.json"), "utf8"), packageBefore);
  } finally {
    fs.rmSync(badRoot, { recursive: true, force: true });
  }

  const buildScript = fs.readFileSync("scripts/build-macos-with-marker.sh", "utf8");
  const syncIndex = buildScript.indexOf('node "$ROOT/scripts/sync-build-version.cjs" "$ROOT"');
  const gateIndex = buildScript.indexOf('bash "$ROOT/scripts/run-release-contracts.sh"');
  assert(syncIndex >= 0 && gateIndex > syncIndex,
    "Build must synchronize metadata before the release gate.");

  const gateScript = fs.readFileSync("scripts/run-release-contracts.sh", "utf8");
  assert(gateScript.includes('node "$ROOT/scripts/test-build-version-sync.cjs"'),
    "Release gate must run version-sync regression tests.");

  const packageJson = JSON.parse(fs.readFileSync("package.json", "utf8"));
  assert.equal(packageJson.scripts["sync:build-version"],
    "node scripts/sync-build-version.cjs .");

  console.log("PASS: all five version sources synchronize to the marker.");
  console.log("PASS: stale npm lock metadata and Cargo lock entry are repaired.");
  console.log("PASS: dependency lock data remains unchanged.");
  console.log("PASS: backups are retained and repeat runs are idempotent.");
  console.log("PASS: mismatched package identity fails closed without edits.");
  console.log("PASS: build preflight and release-gate regression wiring.");
} finally {
  fs.rmSync(temporaryRoot, { recursive: true, force: true });
}
