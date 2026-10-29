"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const script = path.join(__dirname, "stamp-platform-build.cjs");
const originalVersion = "0.2.0-dev.11.1D01";
const stampedVersion = "0.2.0-dev.11.2A01";

function fixtureRoot() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "rdt-build-stamp-test-"));
  for (const relative of ["scripts", "src-tauri"]) fs.mkdirSync(path.join(root, relative), { recursive: true });
  fs.writeFileSync(path.join(root, "scripts/build-id-sequence-windows.txt"), "2A01\n");
  fs.writeFileSync(path.join(root, "scripts/build-id-sequence-linux.txt"), "4L001\n");
  fs.writeFileSync(path.join(root, "scripts/build-id-sequence.txt"), "1D01\n");
  const packageJson = { name: "rdt-connect-desktop", version: originalVersion, dependencies: {}, devDependencies: {} };
  fs.writeFileSync(path.join(root, "package.json"), `${JSON.stringify(packageJson, null, 2)}\n`);
  fs.writeFileSync(path.join(root, "package-lock.json"), `${JSON.stringify({ ...packageJson, lockfileVersion: 3, requires: true, packages: { "": { name: packageJson.name, version: originalVersion } } }, null, 2)}\n`);
  fs.writeFileSync(path.join(root, "src-tauri/tauri.conf.json"), `${JSON.stringify({ productName: "RDT Connect", version: originalVersion }, null, 2)}\n`);
  fs.writeFileSync(path.join(root, "src-tauri/Cargo.toml"), `[package]\nname = "rdt-connect-desktop"\nversion = "${originalVersion}"\nedition = "2021"\n\n[dependencies]\nserde = "1"\n`);
  fs.writeFileSync(path.join(root, "src-tauri/Cargo.lock"), `version = 4\n\n[[package]]\nname = "rdt-connect-desktop"\nversion = "${originalVersion}"\ndependencies = []\n`);
  return root;
}

function run(root, platform, marker = null) {
  const args = [script, platform];
  if (marker !== null) args.push(marker);
  return spawnSync(process.execPath, args, {
    encoding: "utf8",
    env: { ...process.env, GITHUB_ENV: "", RDT_STAMP_ROOT: root },
  });
}
function readVersions(root) {
  const pkg = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8"));
  const lock = JSON.parse(fs.readFileSync(path.join(root, "package-lock.json"), "utf8"));
  const tauri = JSON.parse(fs.readFileSync(path.join(root, "src-tauri/tauri.conf.json"), "utf8"));
  const cargo = fs.readFileSync(path.join(root, "src-tauri/Cargo.toml"), "utf8").match(/^version = "([^"]+)"/m)?.[1];
  const cargoLock = fs.readFileSync(path.join(root, "src-tauri/Cargo.lock"), "utf8").match(/^name = "rdt-connect-desktop"\s*\nversion = "([^"]+)"/m)?.[1];
  return [pkg.version, lock.version, lock.packages[""].version, tauri.version, cargo, cargoLock];
}

const root = fixtureRoot();
try {
  const good = run(root, "windows");
  assert.equal(good.status, 0, `Windows stamping should pass: ${good.stderr}\n${good.stdout}`);
  assert.ok(good.stdout.includes("marker=2A01 version=0.2.0-dev.11.2A01"));
  assert.deepEqual(readVersions(root), Array(6).fill(stampedVersion), "all version manifests must stamp consistently");

  const beforeInvalidMarker = readVersions(root);
  fs.writeFileSync(path.join(root, "scripts/build-id-sequence-windows.txt"), "0D55\n");
  const badMarker = run(root, "windows");
  assert.notEqual(badMarker.status, 0, "Windows stamping must reject macOS marker families");
  assert.deepEqual(readVersions(root), beforeInvalidMarker, "invalid markers must not change manifests");

  fs.writeFileSync(path.join(root, "scripts/build-id-sequence-windows.txt"), "2A01\n");
  const lockPath = path.join(root, "src-tauri/Cargo.lock");
  fs.writeFileSync(lockPath, fs.readFileSync(lockPath, "utf8").replace(stampedVersion, originalVersion));
  const mismatched = run(root, "windows");
  assert.notEqual(mismatched.status, 0, "inconsistent manifest versions must be rejected");
  assert.equal(JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8")).version, stampedVersion, "failed preflight must not partially stamp files");

  console.log("PASS: Windows 2A01 stamps package, package lock, Tauri, Cargo and Cargo.lock consistently");
  console.log("PASS: Windows stamping rejects out-of-namespace markers without changing versions");
  console.log("PASS: Windows stamping rejects inconsistent source manifests before mutation");
} finally {
  fs.rmSync(root, { recursive: true, force: true });
}



// Linux ARM64 and x64 use five-character L markers; retired distro markers fail.
for (const linuxMarker of ["4L001", "5L001"]) {
  const linuxRoot = fixtureRoot();
  try {
    const result = run(linuxRoot, "linux", linuxMarker);
    const expectedVersion = `0.2.0-dev.11.${linuxMarker}`;
    assert.equal(result.status, 0,
      `Linux ${linuxMarker} stamping should pass: ${result.stderr}\n${result.stdout}`);
    assert.ok(result.stdout.includes(`marker=${linuxMarker} version=${expectedVersion}`));
    assert.deepEqual(readVersions(linuxRoot), Array(6).fill(expectedVersion));

    const beforeInvalid = readVersions(linuxRoot);
    const invalid = run(linuxRoot, "linux", "4U01");
    assert.notEqual(invalid.status, 0, "Retired Linux distro markers must be rejected");
    assert.deepEqual(readVersions(linuxRoot), beforeInvalid,
      "Invalid Linux markers must not mutate manifests");
    console.log(`PASS: Linux ${linuxMarker} stamps all manifests and rejects retired markers`);
  } finally {
    fs.rmSync(linuxRoot, { recursive: true, force: true });
  }
}

// Verify the actual Dev 11 Windows ARM64 marker remains intact.
const markerRoot = fixtureRoot();
try {
  fs.writeFileSync(
    path.join(markerRoot, "scripts/build-id-sequence-windows.txt"),
    "2A06\n"
  );

  const result = run(markerRoot, "windows");
  const expectedVersion = "0.2.0-dev.11.2A06";

  assert.equal(
    result.status,
    0,
    `Windows 2A06 stamping should pass: ${result.stderr}\n${result.stdout}`
  );
  assert.ok(
    result.stdout.includes(`marker=2A06 version=${expectedVersion}`),
    "stamping output must preserve the whole 2A06 marker"
  );
  assert.deepEqual(
    readVersions(markerRoot),
    Array(6).fill(expectedVersion),
    "all manifests must use the intact 2A06 suffix"
  );

  console.log("PASS: Windows 2A06 remains intact across all version manifests");
} finally {
  fs.rmSync(markerRoot, { recursive: true, force: true });
}
