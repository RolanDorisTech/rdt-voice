"use strict";

const assert = require("node:assert/strict");
const semver = require("semver");
const {
  withBuildMarker,
  decodeBuildVersion,
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
const v1C = build("1C00");
const v0E = build("0E00");
const v1D = build("1D00");
const winA01 = build("2A01");
const winA99 = build("2A99");
const winB00 = build("2B00");
const linuxA01 = build("4A01");

for (const version of [v51, v52, v99, v100, v1C, v0E, v1D]) {
  assert.ok(semver.valid(version), `Valid SemVer: ${version}`);
  assert.ok(semver.gt(version, base), `${version} > ${base}`);
}
for (const version of [winA01, winA99, winB00, linuxA01]) {
  assert.ok(semver.valid(version), `Valid platform SemVer: ${version}`);
  assert.ok(semver.gt(version, base), `${version} > ${base}`);
}

assert.equal(v51, "0.2.0-dev.10.0D.51");
assert.equal(build("0D00"), "0.2.0-dev.10.0D.0");
assert.ok(semver.gt(v52, v51), "0D52 > 0D51");
assert.ok(semver.gt(v53, v52), "0D53 > 0D52");
assert.ok(semver.gt(v100, v99), "0D100 > 0D99");
assert.ok(semver.gt(v100, v52), "0D100 > 0D52");
assert.ok(semver.gt(v1D, v1C), "1D00 > 1C00");
assert.ok(semver.gt(v1D, v0E), "1D00 > 0E00");

assert.equal(winA01, "0.2.0-dev.10.2A.1");
assert.equal(winA99, "0.2.0-dev.10.2A.99");
assert.equal(winB00, "0.2.0-dev.10.2B.0");
assert.equal(linuxA01, "0.2.0-dev.10.4A.1");
assert.equal(decodeBuildVersion(winA01).marker, "2A01");
assert.equal(decodeBuildVersion(linuxA01).marker, "4A01");
assert.equal(formatProductVersionLabel(v51), "v0.2.0-dev.10");
assert.equal(formatFriendlyUpdateVersion(v51), "v0.2.0-dev.10 Build 0D51");
assert.equal(formatFriendlyUpdateVersion(v100), "v0.2.0-dev.10 Build 0D100");
assert.equal(formatFriendlyUpdateVersion(v1D), "v0.2.0-dev.10 Build 1D00");

console.log("PASS: valid SemVer, marker encoding, numeric counters and family ordering");
console.log("PASS: 1D00 > 1C00 and 1D00 > 0E00");
console.log("PASS: platform markers 2A01/2A99/2B00 and 4A01 encode to valid SemVer");
console.log("PASS: friendly labels for 0D51, 0D100 and 1D00");
