#!/usr/bin/env python3
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESOURCES = ROOT / "src-tauri" / "resources"
EXPECTED_AUDIO_SHA = "060ce7a72e16521bb90a646015b27bc714c662a155f3bd78371ed66c264d1938"
EXPECTED_FIXTURE_ID = "rdt-user-voice-manual-21s-v1"

parser = argparse.ArgumentParser()
parser.add_argument("--app", type=Path)
args = parser.parse_args()

def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

def validate_pair(base, label):
    fixture_path = base / "benchmark" / "fixture.json"
    audio_path = base / "benchmark" / "user-voice-21s.wav"
    assert fixture_path.is_file(), f"{label}: benchmark/fixture.json missing"
    assert audio_path.is_file(), f"{label}: benchmark/user-voice-21s.wav missing"

    fixture = json.loads(fixture_path.read_text())
    owner = json.loads((RESOURCES / "user-voice-benchmark" / "fixture.json").read_text())
    actual_sha = sha(audio_path)

    assert actual_sha == EXPECTED_AUDIO_SHA, f"{label}: manual audio hash mismatch"
    assert fixture.get("schema") == "rdt-connect-benchmark-fixture-v1"
    assert fixture.get("fixture_id") == EXPECTED_FIXTURE_ID
    assert fixture.get("source") == "User-provided voice recording"
    assert fixture.get("audio_filename") == "user-voice-21s.wav"
    assert fixture.get("sha256") == EXPECTED_AUDIO_SHA
    assert fixture.get("sample_rate_hz") == 16000
    assert 20.0 <= fixture.get("duration_seconds", 0) <= 21.5

    reference = fixture.get("reference")
    assert isinstance(reference, str) and len(reference.split()) == 50
    assert owner.get("source_sha256") == actual_sha
    assert owner.get("transcript") == reference
    assert owner.get("transcript_origin") == "Original script written for this benchmark"
    return len(reference.split())

config = json.loads((ROOT / "src-tauri" / "tauri.conf.json").read_text())
mapping = config["bundle"]["resources"]
assert mapping.get("resources/benchmark/fixture.json") == "benchmark/fixture.json"
assert mapping.get("resources/benchmark/user-voice-21s.wav") == "benchmark/user-voice-21s.wav"
assert "resources/benchmark" not in mapping

count = validate_pair(RESOURCES, "source resources")
print(f"PASS: manual source fixture and owner-authored {count}-word reference.")
print("PASS: manual WAV SHA-256 and explicit Tauri resource mappings.")

if args.app:
    app = args.app.resolve()
    assert app.is_dir(), f"app bundle missing: {app}"
    app_resources = app / "Contents" / "Resources"
    count = validate_pair(app_resources, "app bundle")
    print(f"PASS: app bundle contains verified manual fixture and {count}-word reference.")
