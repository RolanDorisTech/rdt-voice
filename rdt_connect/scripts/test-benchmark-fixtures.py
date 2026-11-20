#!/usr/bin/env python3
import argparse
import hashlib
import json
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "src-tauri/resources"
parser = argparse.ArgumentParser()
parser.add_argument("--app", type=Path)
args = parser.parse_args()

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def audio(path, low, high):
    with wave.open(str(path), "rb") as wav:
        rate = wav.getframerate()
        channels = wav.getnchannels()
        duration = wav.getnframes() / rate
    assert rate == 16000 and channels == 1
    assert low <= duration <= high

def validate(base):
    first = base / "first-run-benchmark"
    ff = json.loads((first / "fixture.json").read_text())
    fa = first / "audio.wav"
    ref = first / "reference-28-words.txt"
    assert ff["fixture_id"] == "rdt-user-voice-first-run-12s-v1"
    assert ff["source"] == "User-provided voice recording"
    assert ff["sha256"] == sha(fa) == "55c7b7d3b127a50ce6943a08e0078ff3aa67f42ca926814201a819654914450a"
    assert ff["reference_sha256"] == sha(ref) == "c2a2654a48f82a06d23928bd46b85a5801fb40935bcaa3fbfb356ccbd75aca56"
    assert ff["transcript"].strip() == ref.read_text().strip()
    assert len(ff["transcript"].split()) == 28
    audio(fa, 11.5, 12.5)

    manual = base / "benchmark"
    mf = json.loads((manual / "fixture.json").read_text())
    ma = manual / "user-voice-21s.wav"
    owner = json.loads((RES / "user-voice-benchmark/fixture.json").read_text())
    assert mf["fixture_id"] == "rdt-user-voice-manual-21s-v1"
    assert mf["schema"] == "rdt-connect-benchmark-fixture-v1"
    assert mf["source"] == "User-provided voice recording"
    assert mf["sha256"] == sha(ma) == "060ce7a72e16521bb90a646015b27bc714c662a155f3bd78371ed66c264d1938"
    assert owner["source_sha256"] == sha(ma)
    assert mf["transcript"] == owner["transcript"]
    assert len(mf["transcript"].split()) == 50
    audio(ma, 20.0, 21.5)

validate(RES)
config = json.loads((ROOT / "src-tauri/tauri.conf.json").read_text())
mapping = config["bundle"]["resources"]
assert mapping["resources/first-run-benchmark"] == "first-run-benchmark"
assert mapping["resources/benchmark/fixture.json"] == "benchmark/fixture.json"
assert mapping["resources/benchmark/user-voice-21s.wav"] == "benchmark/user-voice-21s.wav"

if args.app:
    app = args.app.resolve()
    assert app.is_dir()
    validate(app / "Contents/Resources")
    print("PASS: both owner-recorded fixtures verified in app.")

print("PASS: 12-second first-run fixture and 20.7-second manual fixture.")
print("PASS: transcript provenance, WAV hashes, duration, and Tauri mappings.")
