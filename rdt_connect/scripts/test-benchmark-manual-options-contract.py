import json
from pathlib import Path

main = Path("src/main.ts").read_text(encoding="utf-8")
rust = Path("src-tauri/src/main.rs").read_text(encoding="utf-8")
runner = Path("scripts/run-release-contracts.sh").read_text(encoding="utf-8")

assert 'id="benchmark-audio-fixture"' in main
assert 'value="12s"' in main and 'value="21s"' in main
assert "type ManualBenchmarkFixture" in main
assert "timeLimitEnabled: manualHardwareTimingLimitCheckbox.checked" in main
assert "manualHardwareSelection?.timeLimitEnabled" in main
assert 'manualHardwareSelection?.fixture === "21s" ? 120_000 : 60_000' in main
assert 'manualFixture: source === "manual" ? manualHardwareSelection?.fixture ?? null : null' in main
assert "selectedDeviceIds.length !== 2" not in main

assert "native_benchmark_candidate_timeout_ms" in rust
assert 'Some("12s") => 60_000' in rust
assert 'Some("21s") => 120_000' in rust
assert "None => Ok(None)" in rust
assert "local_tls_client_with_optional_timeout" in rust
assert "apply_benchmark_deadline(benchmark_deadline.as_ref())?" in rust
assert "benchmark_candidate_timeout_policy_separates_mandatory_and_manual_modes" in rust

required = {
    "msg.benchmark_timing_limit.1d22",
    "msg.benchmark_audio_label.1d23",
    "msg.benchmark_audio_12s.1d23",
    "msg.benchmark_audio_21s.1d23",
}
for path in Path("src/locales").glob("*.json"):
    data = json.loads(path.read_text(encoding="utf-8"))
    assert required <= set(data["messages"]), f"Missing benchmark audio strings: {path}"
assert "test-benchmark-manual-options-contract.py" in runner
print(f"PASS: paired audio/timing UI and {len(list(Path('src/locales').glob('*.json')))} locale catalogs.")
print("PASS: checked 12s/60s, checked 20.7s/120s, unchecked unlimited.")
