"use strict";

const fs = require("fs");
const path = require("path");
const {
  decodeBuildVersion,
  withBuildMarker,
} = require("./build-versioning.cjs");

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, "utf8"));
}

function jsonText(value, previousText) {
  const eol = previousText.includes("\r\n") ? "\r\n" : "\n";
  return JSON.stringify(value, null, 2).replace(/\n/g, eol) + eol;
}

function packageSection(text) {
  const headers = [...text.matchAll(/^\[package\][ \t]*$/gm)];
  if (headers.length !== 1) {
    throw new Error(`Expected one Cargo [package] section; found ${headers.length}`);
  }

  const start = headers[0].index + headers[0][0].length;
  const nextHeader = /^\[[^\r\n]+\][ \t]*$/gm;
  nextHeader.lastIndex = start;
  const next = nextHeader.exec(text);
  const end = next ? next.index : text.length;
  return { start, end, text: text.slice(start, end) };
}

function replaceTomlVersion(section, version, description) {
  const pattern = /^([ \t]*version[ \t]*=[ \t]*)"[^"]+"([ \t]*)$/m;
  if (!pattern.test(section)) {
    throw new Error(`Missing literal version field in ${description}`);
  }
  return section.replace(pattern, `$1"${version}"$2`);
}

function cargoLockPackage(text, name) {
  const headers = [...text.matchAll(/^\[\[package\]\][ \t]*$/gm)];
  const matches = [];

  for (let i = 0; i < headers.length; i += 1) {
    const start = headers[i].index;
    const end = i + 1 < headers.length ? headers[i + 1].index : text.length;
    const block = text.slice(start, end);
    const nameMatch = block.match(/^name = "([^"]+)"[ \t]*$/m);
    if (nameMatch && nameMatch[1] === name) {
      matches.push({ start, end, block });
    }
  }

  if (matches.length !== 1) {
    throw new Error(`Expected one Cargo.lock entry for ${name}; found ${matches.length}`);
  }

  return matches[0];
}

function syncBuildVersion(projectRoot) {
  const root = path.resolve(projectRoot);
  const markerFile = path.join(root, "scripts/build-id-sequence.txt");
  const marker = fs.readFileSync(markerFile, "utf8").trim();

  // Validate the marker using the same SemVer rules used by the build.
  withBuildMarker("0.0.0", marker);

  const packagePath = path.join(root, "package.json");
  const npmLockPath = path.join(root, "package-lock.json");
  const tauriPath = path.join(root, "src-tauri/tauri.conf.json");
  const cargoPath = path.join(root, "src-tauri/Cargo.toml");
  const cargoLockPath = path.join(root, "src-tauri/Cargo.lock");

  const packageJson = readJson(packagePath);
  const npmLock = readJson(npmLockPath);
  const tauri = readJson(tauriPath);

  if (typeof packageJson.name !== "string" || !packageJson.name) {
    throw new Error("package.json is missing its package name");
  }
  if (typeof packageJson.version !== "string") {
    throw new Error("package.json is missing its version");
  }
  if (npmLock.name !== packageJson.name) {
    throw new Error("package-lock.json name does not match package.json");
  }
  if (!npmLock.packages || !npmLock.packages[""]) {
    throw new Error("package-lock.json is missing its root package entry");
  }
  if (npmLock.packages[""].name !== packageJson.name) {
    throw new Error("package-lock.json root package name does not match package.json");
  }
  if (typeof tauri.version !== "string") {
    throw new Error("tauri.conf.json is missing its version");
  }

  // package.json is the canonical base-version source.
  // The marker file is the canonical build-identity source.
  const decoded = decodeBuildVersion(packageJson.version);
  const targetVersion = withBuildMarker(decoded.baseVersion, marker);
  if (decodeBuildVersion(targetVersion).marker !== marker) {
    throw new Error(`Generated version does not encode marker ${marker}`);
  }

  const cargoText = fs.readFileSync(cargoPath, "utf8");
  const cargoPackage = packageSection(cargoText);
  const cargoName = cargoPackage.text.match(/^name[ \t]*=[ \t]*"([^"]+)"[ \t]*$/m);
  if (!cargoName) {
    throw new Error("Cargo.toml [package] name is missing");
  }

  const cargoLockText = fs.readFileSync(cargoLockPath, "utf8");
  const lockEntry = cargoLockPackage(cargoLockText, cargoName[1]);

  const changes = new Map();

  packageJson.version = targetVersion;
  npmLock.version = targetVersion;
  npmLock.packages[""].version = targetVersion;
  tauri.version = targetVersion;

  changes.set(packagePath, jsonText(packageJson, fs.readFileSync(packagePath, "utf8")));
  changes.set(npmLockPath, jsonText(npmLock, fs.readFileSync(npmLockPath, "utf8")));
  changes.set(tauriPath, jsonText(tauri, fs.readFileSync(tauriPath, "utf8")));

  const updatedCargoSection = replaceTomlVersion(
    cargoPackage.text,
    targetVersion,
    "Cargo.toml [package]"
  );
  changes.set(
    cargoPath,
    cargoText.slice(0, cargoPackage.start) +
      updatedCargoSection +
      cargoText.slice(cargoPackage.end)
  );

  const updatedLockBlock = replaceTomlVersion(
    lockEntry.block,
    targetVersion,
    `Cargo.lock package ${cargoName[1]}`
  );
  changes.set(
    cargoLockPath,
    cargoLockText.slice(0, lockEntry.start) +
      updatedLockBlock +
      cargoLockText.slice(lockEntry.end)
  );

  const changed = [...changes.entries()].filter(
    ([file, content]) => fs.readFileSync(file, "utf8") !== content
  );

  if (changed.length === 0) {
    return { marker, version: targetVersion, changedFiles: [], backups: [] };
  }

  const stamp = new Date().toISOString().replace(/[-:.]/g, "") + `-${process.pid}`;
  const entries = changed.map(([file, content]) => ({
    file,
    content,
    backup: `${file}.bak-build-version-sync-${stamp}`,
    temporary: `${file}.tmp-build-version-sync-${stamp}`,
  }));

  // Preflight every backup and temporary path before writing anything.
  for (const entry of entries) {
    if (fs.existsSync(entry.backup) || fs.existsSync(entry.temporary)) {
      throw new Error(`Backup or temporary path already exists for ${entry.file}`);
    }
  }

  // Prepare all replacement files first; retain exact originals before promotion.
  for (const entry of entries) {
    fs.writeFileSync(entry.temporary, entry.content, { flag: "wx" });
    fs.chmodSync(entry.temporary, fs.statSync(entry.file).mode & 0o777);
  }
  for (const entry of entries) {
    fs.copyFileSync(entry.file, entry.backup, fs.constants.COPYFILE_EXCL);
  }
  for (const entry of entries) {
    fs.renameSync(entry.temporary, entry.file);
  }

  return {
    marker,
    version: targetVersion,
    changedFiles: entries.map(entry => entry.file),
    backups: entries.map(entry => entry.backup),
  };
}

module.exports = { syncBuildVersion };

if (require.main === module) {
  const root = process.argv[2] || process.cwd();
  const result = syncBuildVersion(root);
  console.log(`PASS: build ${result.marker} uses ${result.version}`);
  if (result.changedFiles.length === 0) {
    console.log("PASS: all version metadata was already synchronized.");
  } else {
    for (const file of result.changedFiles) console.log(`Updated: ${file}`);
    for (const file of result.backups) console.log(`Backup: ${file}`);
  }
}
