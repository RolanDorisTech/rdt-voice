"use strict";

const semver = require("semver");

function parseVersion(version) {
  const parsed = semver.parse(version);
  if (!parsed || parsed.build.length) {
    throw new Error(`Invalid version or unexpected build metadata: ${version}`);
  }
  return parsed;
}

function baseVersionFromIds(parsed, ids) {
  return `${parsed.major}.${parsed.minor}.${parsed.patch}-${ids.join(".")}`;
}

function decodeBuildVersion(version) {
  const parsed = parseVersion(version);
  const ids = parsed.prerelease.map(String);

  if (ids.length < 2) {
    return { baseVersion: parsed.version, marker: null };
  }

  // Current format: the complete marker is one SemVer prerelease identifier.
  // Example: 0.2.0-dev.12.1D42 (never 0.2.0-dev.12.1D.42).
  const contiguous = /^([0-9][A-Z])([0-9]{2,})$/.exec(ids.at(-1));
  if (contiguous) {
    const baseIds = ids.slice(0, -1);
    if (baseIds.length) {
      return {
        baseVersion: baseVersionFromIds(parsed, baseIds),
        marker: contiguous[1] + contiguous[2],
      };
    }
  }

  // Read-only compatibility for already-installed legacy versions such as
  // 0.2.0-dev.12.1D.41. New versions are never emitted in this format.
  const family = ids.at(-2);
  const counter = ids.at(-1);
  if (/^[0-9][A-Z]$/.test(family) && /^(0|[1-9][0-9]*)$/.test(counter)) {
    const baseIds = ids.slice(0, -2);
    if (baseIds.length) {
      return {
        baseVersion: baseVersionFromIds(parsed, baseIds),
        marker: family + counter.padStart(2, "0"),
      };
    }
  }

  return { baseVersion: parsed.version, marker: null };
}

function withBuildMarker(baseVersion, marker) {
  if (!/^([0-9][A-Z])([0-9]{2,})$/.test(marker)) {
    throw new Error(`Invalid build marker: ${marker}`);
  }

  const parsedInput = parseVersion(baseVersion);
  const decoded = decodeBuildVersion(parsedInput.version);
  const base = semver.parse(decoded.marker ? decoded.baseVersion : parsedInput.version);
  const ids = [...base.prerelease.map(String), marker];
  const version = `${base.major}.${base.minor}.${base.patch}-${ids.join(".")}`;

  if (!semver.valid(version)) {
    throw new Error(`Generated invalid SemVer: ${version}`);
  }
  return version;
}

function formatProductVersionLabel(version) {
  const decoded = decodeBuildVersion(version);
  return `v${decoded.baseVersion}`;
}

function formatFriendlyUpdateVersion(version) {
  const decoded = decodeBuildVersion(version);
  return decoded.marker
    ? `v${decoded.baseVersion} Build ${decoded.marker}`
    : `v${decoded.baseVersion}`;
}

module.exports = {
  decodeBuildVersion,
  withBuildMarker,
  formatProductVersionLabel,
  formatFriendlyUpdateVersion,
};

if (require.main === module) {
  const [, , action, version, marker] = process.argv;
  if (action === "validate") {
    const decoded = decodeBuildVersion(version);
    if (decoded.marker !== marker ||
        withBuildMarker(decoded.baseVersion, marker) !== version) {
      throw new Error(`Build marker ${marker} does not match internal version ${version}`);
    }
    console.log(`PASS: ${version} correctly encodes build ${marker}`);
  } else {
    throw new Error("Usage: node build-versioning.cjs validate <version> <marker>");
  }
}
