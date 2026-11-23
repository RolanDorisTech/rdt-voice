#!/usr/bin/env python3
import hashlib
import json
import sys
from pathlib import Path

def fail(message):
    raise SystemExit("R82 staging verification FAILED: " + message)

if len(sys.argv) != 6:
    fail("expected SOURCE STAGED ASSETS VERSION MANIFEST")

source, staged, assets = map(Path, sys.argv[1:4])
version, manifest_path = sys.argv[4], Path(sys.argv[5])

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

if not source.is_dir() or not staged.is_dir():
    fail("source or staged frontend directory is missing")
if not manifest_path.is_file():
    fail("manifest is missing")

manifest = json.loads(manifest_path.read_text())
if manifest.get("bundle_version") != version:
    fail("bundle version mismatch")

alpha = manifest.get("alpha_source_files")
transformed = manifest.get("transformed_files")

if not isinstance(alpha, dict) or not alpha:
    fail("manifest has no Alpha source fingerprints")
if not isinstance(transformed, dict):
    fail("manifest transformed-file map is invalid")

if transformed.get("worker.js", {}).get("transform") != "rdt-offline-runtime-worker-v1":
    fail("worker.js transform version is not the qualified offline-runtime transform")
if transformed.get("index.html", {}).get("transform") != "rdt-connect-offline-easywhisper-v3-native-oom-bridge-v1":
    fail("index.html is missing the qualified Connect native-OOM bridge transform")

def locate(root, name):
    direct = root / name
    if direct.is_file():
        return direct
    matches = list(root.rglob(Path(name).name))
    return matches[0] if len(matches) == 1 else None

checked = 0
for name, expected in alpha.items():
    src = locate(source, name)
    if src is None:
        fail(f"Alpha source file missing: {name}")
    expected_hash = expected if isinstance(expected, str) else (
        expected.get("sha256") or expected.get("source_sha256")
        if isinstance(expected, dict) else None
    )
    if not expected_hash:
        fail(f"Alpha source fingerprint missing: {name}")
    if expected_hash and sha(src) != expected_hash:
        fail(f"Alpha source fingerprint mismatch: {name}")
    checked += 1

for name, record in transformed.items():
    src = locate(source, name)
    dst = locate(staged, name)
    if src is None or dst is None or not isinstance(record, dict):
        fail(f"transformed file missing or invalid: {name}")
    if record.get("source_sha256") and sha(src) != record["source_sha256"]:
        fail(f"transformation source mismatch: {name}")
    if record.get("staged_sha256") and sha(dst) != record["staged_sha256"]:
        fail(f"transformed output mismatch: {name}")

for required in ("index.html", "worker.js"):
    if not (staged / required).is_file():
        fail(f"required offline frontend file missing: {required}")

if not assets.is_dir():
    fail("staged assets directory missing")

print(f"R82 source fingerprints checked: {checked}")
print(f"Transformed files checked: {len(transformed)}")
print("R82 staging verification: PASS")
