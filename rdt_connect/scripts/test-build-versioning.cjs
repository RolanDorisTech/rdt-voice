"use strict";

const assert = require("node:assert/strict");
const semver = require("semver");
const {
  withBuildMarker,
  decodeBuildVersion,
  formatProductVersionLabel,
  formatFriendlyUpdateVersion,
} = require("./build-versioning.cjs");

const base = "0.2.0-dev.11";
const build = marker => withBuildMarker(base, marker);

const v00 = build("0D00");
const v51 = build("2A05");
const v52 = build("2A06");
const v53 = build("2A07");
const v98 = build("0D98");
const v99 = build("0D99");
const v0E = build("0E00");
const v1C = build("1C99");
const v1D = build("1D00");

const winA01 = build("2A01");
const winA05 = build("2A05");
const winA99 = build("2A99");
const winB00 = build("2B00");
const winX64 = build("3A01");
const linuxARM64 = build("4U01");
const linuxX64 = build("5U01");

const currentVersions = [
  v00, v51, v52, v53, v98, v99, v0E, v1C, v1D,
  winA01, winA05, winA99, winB00, winX64, linuxARM64, linuxX64,
];

for (const version of currentVersions) {
  assert.ok(semver.valid(version), `Valid SemVer: ${version}`);
  assert.ok(semver.gt(version, base), `${version} > ${base}`);
}

assert.equal(v00, "0.2.0-dev.11.0D00");
assert.equal(v51, "0.2.0-dev.11.2A05");
assert.equal(v99, "0.2.0-dev.11.0D99");
assert.equal(v0E, "0.2.0-dev.11.0E00");
assert.equal(v1D, "0.2.0-dev.11.1D00");
assert.equal(winA05, "0.2.0-dev.11.2A05");
assert.equal(winX64, "0.2.0-dev.11.3A01");
assert.equal(linuxARM64, "0.2.0-dev.11.4U01");
assert.equal(linuxX64, "0.2.0-dev.11.5U01");

assert.ok(semver.gt(v52, v51), "2A06 > 2A05");
assert.ok(semver.gt(v53, v52), "2A07 > 2A06");
assert.ok(semver.gt(v99, v98), "0D99 > 0D98");
assert.ok(semver.gt(v0E, v99), "0E00 > 0D99");
assert.ok(semver.gt(v1D, v1C), "1D00 > 1C99");

for (const [version, marker] of [
  [winA01, "2A01"],
  [winA05, "2A05"],
  [winA99, "2A99"],
  [winB00, "2B00"],
  [winX64, "3A01"],
  [linuxARM64, "4U01"],
  [linuxX64, "5U01"],
]) {
  assert.equal(decodeBuildVersion(version).marker, marker);
  assert.ok(version.endsWith(`.${marker}`), `${marker} must remain contiguous`);
}

assert.throws(() => build("0D100"), /Invalid build marker/);
assert.throws(() => build("2A.05"), /Invalid build marker/);

// Historical versions remain readable, but newly generated versions use
// the whole-marker format.
const legacyWindows = "0.2.0-dev.10.2A.1";
assert.equal(decodeBuildVersion(legacyWindows).marker, "2A01");
assert.equal(
  decodeBuildVersion(legacyWindows).baseVersion,
  "0.2.0-dev.10"
);
assert.equal(
  withBuildMarker(legacyWindows, "3A01"),
  "0.2.0-dev.10.3A01"
);

const legacyCurrent = "0.2.0-dev.11.1D.1";
assert.equal(decodeBuildVersion(legacyCurrent).marker, "1D01");
assert.equal(
  withBuildMarker(legacyCurrent, "1D01"),
  "0.2.0-dev.11.1D01"
);

assert.equal(formatProductVersionLabel(v51), "v0.2.0-dev.11");
assert.equal(
  formatFriendlyUpdateVersion(v51),
  "v0.2.0-dev.11 Build 2A05"
);
assert.equal(
  formatFriendlyUpdateVersion(winA05),
  "v0.2.0-dev.11 Build 2A05"
);
assert.equal(
  formatFriendlyUpdateVersion(v1D),
  "v0.2.0-dev.11 Build 1D00"
);

console.log("PASS: four-character build markers remain contiguous in SemVer");
console.log("PASS: platform markers 1D01, 2A05, 3A01, 4U01 and 5U01");
console.log("PASS: marker ordering and rollover");
console.log("PASS: invalid marker formats are rejected");
console.log("PASS: historical dotted versions remain readable");
console.log("PASS: product labels and friendly update labels");
