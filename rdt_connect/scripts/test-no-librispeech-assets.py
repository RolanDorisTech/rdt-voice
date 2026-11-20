#!/usr/bin/env python3
import argparse
import hashlib
import json
from pathlib import Path

BANNED_SHA256 = "cdaf3f95cefc86b28226332da1b776f46fdb8c0f8d7fa882e7ec830c47784e88"
BANNED_NAME = "rdt_librispeech_test_clean_sample.wav"
TOKEN = b"librispeech"

# These three recordings are owner-provided benchmark assets and must be retained.
OWNED_AUDIO = {
    "benchmark/user-voice-21s.wav": "060ce7a72e16521bb90a646015b27bc714c662a155f3bd78371ed66c264d1938",
    "first-run-benchmark/audio.wav": "55c7b7d3b127a50ce6943a08e0078ff3aa67f42ca926814201a819654914450a",
    "user-voice-benchmark/benchmark-21s.wav": "59f1fd5caa3b3a73daf7f45eb6e9fe7ac658b59c283a474f99d18c4c6969a21e",
}

parser = argparse.ArgumentParser()
parser.add_argument("--app", type=Path)
args = parser.parse_args()

root = Path(__file__).resolve().parents[1]
resources = root / "src-tauri" / "resources"
config = json.loads((root / "src-tauri" / "tauri.conf.json").read_text())
mapping = config["bundle"]["resources"]

if isinstance(mapping, dict):
    if "resources/benchmark" in mapping:
        raise SystemExit("FAIL: Tauri packages the entire benchmark directory.")
elif isinstance(mapping, list):
    if any(str(item).rstrip("/") == "resources/benchmark" for item in mapping):
        raise SystemExit("FAIL: Tauri packages the entire benchmark directory.")
else:
    raise SystemExit("FAIL: unrecognized Tauri resource mapping format.")

roots = [("source resources", resources, Path("."))]
if args.app:
    app = args.app.resolve()
    if not app.is_dir():
        raise SystemExit(f"FAIL: app bundle missing: {app}")
    roots.append(("entire app bundle", app, Path("Contents/Resources")))

def scan_file(path):
    digest = hashlib.sha256()
    overlap = b""
    found_token = False
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            sample = (overlap + block).lower()
            found_token = found_token or TOKEN in sample
            overlap = sample[-(len(TOKEN) - 1):]
    return digest.hexdigest(), found_token

for label, scan_root, owned_prefix in roots:
    if not scan_root.is_dir():
        raise SystemExit(f"FAIL: {label} directory missing: {scan_root}")

    for path in scan_root.rglob("*"):
        relative = path.relative_to(scan_root).as_posix()
        if TOKEN.decode() in path.name.casefold():
            raise SystemExit(f"FAIL: LibriSpeech-named path in {label}: {relative}")
        if not path.is_file():
            continue

        digest, token_found = scan_file(path)
        if path.name.casefold() == BANNED_NAME.casefold() or digest == BANNED_SHA256:
            raise SystemExit(f"FAIL: prohibited LibriSpeech sample in {label}: {relative}")
        if token_found:
            raise SystemExit(f"FAIL: LibriSpeech data marker in {label}: {relative}")

    for relative, expected in OWNED_AUDIO.items():
        path = scan_root / owned_prefix / relative
        if not path.is_file():
            raise SystemExit(f"FAIL: owner recording missing from {label}: {relative}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise SystemExit(f"FAIL: owner recording changed in {label}: {relative}")

print("PASS: no LibriSpeech-named paths, known sample hash, or embedded data markers.")
print("PASS: all three owner-recorded benchmark WAVs retained byte-for-byte.")
print("PASS: Tauri does not package the entire benchmark directory.")
