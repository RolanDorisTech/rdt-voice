"use strict";

const assert = require("node:assert/strict");
const semver = require("semver");

const {
  decodeBuildVersion,
  withBuildMarker,
  formatProductVersionLabel,
  formatFriendlyUpdateVersion,
} = require("./build-versioning.cjs");

const base = "0.2.0-dev.10";
const build = marker => withBuildMarker(base, marker);

const v51 = build("0D51");
const v52 = build("0D52");
const v53 = build("0D53");
const v99 = build("0D99");
const v100 = build("0D100");
const v0E = build("0E00");
const v1C = build("1C00");
const v1D = build("1D00");
const v1D42 = build("1D42");

for (const version of [v51, v52, v53, v99, v100, v0E, v1C, v1D, v1D42]) {
  assert.ok(semver.valid(version), `Valid SemVer: ${version}`);
  assert.ok(semver.gt(version, base), `${version} > ${base}`);
}

assert.equal(v51, "0.2.0-dev.10.0D51");
assert.equal(build("0D00"), "0.2.0-dev.10.0D00");
assert.equal(v1D42, "0.2.0-dev.10.1D42");
assert.ok(semver.gt(v52, v51), "0D52 > 0D51");
assert.ok(semver.gt(v53, v52), "0D53 > 0D52");
assert.ok(semver.gt(v99, v53), "0D99 > 0D53");
assert.ok(semver.gt(v0E, v99), "0E00 > 0D99");
assert.ok(semver.gt(v1C, v0E), "1C00 > 0E00");
assert.ok(semver.gt(v1D, v1C), "1D00 > 1C00");

assert.equal(decodeBuildVersion(v1D42).marker, "1D42");
assert.equal(decodeBuildVersion(v100).marker, "0D100");
assert.equal(formatProductVersionLabel(v51), "v0.2.0-dev.10");
assert.equal(formatFriendlyUpdateVersion(v51), "v0.2.0-dev.10 Build 0D51");
assert.equal(formatFriendlyUpdateVersion(v100), "v0.2.0-dev.10 Build 0D100");

const legacy = decodeBuildVersion("0.2.0-dev.12.1D.41");
assert.equal(legacy.baseVersion, "0.2.0-dev.12");
assert.equal(legacy.marker, "1D41");
assert.equal(
  withBuildMarker("0.2.0-dev.12.1D.41", "1D42"),
  "0.2.0-dev.12.1D42"
);
assert.equal(
  formatFriendlyUpdateVersion("0.2.0-dev.12.1D.41"),
  "v0.2.0-dev.12 Build 1D41"
);

assert.throws(() => withBuildMarker(base, "1D.42"), /Invalid build marker/);
console.log("PASS: contiguous build markers, legacy-version compatibility and labels.");
console.log("PASS: family ordering, SemVer validity and 1D42 regression.");
