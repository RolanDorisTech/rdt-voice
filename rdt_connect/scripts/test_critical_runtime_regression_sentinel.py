from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def read(relative):
    path = ROOT / relative
    if not path.is_file():
        raise SystemExit(f"FAIL: required regression file is missing: {relative}")
    return path.read_text(encoding="utf-8")

gate = read("scripts/run-release-contracts.sh")

import re

inventory = re.search(
    r"(?ms)^PYTHON_TESTS=\(\s*\n(.*?)^\)",
    gate,
)
if not inventory:
    raise SystemExit("FAIL: could not identify PYTHON_TESTS inventory.")

sentinel_call = 'python3 "$ROOT/scripts/test_critical_runtime_regression_sentinel.py"'
if "test_critical_runtime_regression_sentinel.py" in inventory.group(1):
    raise SystemExit("FAIL: sentinel invocation must not be inside PYTHON_TESTS.")

invocations = [
    line.strip() for line in gate.splitlines()
    if line.strip() == sentinel_call
]
if len(invocations) != 1:
    raise SystemExit(
        f"FAIL: expected one executable sentinel invocation; found {len(invocations)}"
    )

loop_start = gate.find('for test_file in "${PYTHON_TESTS[@]}"; do')
if loop_start < 0:
    raise SystemExit("FAIL: Python release-contract execution loop is missing.")

loop_done = re.search(r"(?m)^done[ \t]*$", gate[loop_start:])
if not loop_done:
    raise SystemExit("FAIL: Python release-contract loop terminator is missing.")

if gate.find(sentinel_call) < loop_start + loop_done.end():
    raise SystemExit("FAIL: sentinel must execute after the Python test loop.")

build = read("scripts/build-macos-with-marker.sh")
patcher_test = read("scripts/test_connect_single_process_runtime_contract.py")
packager_test = read("scripts/test_preserve_packager_resource_tracker_contract.py")
rust = read("src-tauri/src/main.rs")

# The actual behavior suites must remain mandatory in every release gate.
required_gate_tests = (
    "test_connect_single_process_runtime_contract.py",
    "test_preserve_packager_resource_tracker_contract.py",
    "test-connect-benchmark-response-contract.py",
    "test-connect-benchmark-api.py",
)
for test in required_gate_tests:
    count = gate.count(test)
    if count != 1:
        raise SystemExit(f"FAIL: release gate must invoke {test} exactly once; found {count}")

# Benchmark backend guard: ordinary sys plus the private helper alias.
if "test_injected_guard_binds_standard_sys_and_private_alias" not in patcher_test:
    raise SystemExit("FAIL: standard-sys injection regression test was removed.")

# Startup must not execute synthetic inference, but normal inference remains.
if "test_synthetic_startup_warmup_is_removed_but_real_inference_remains" not in patcher_test:
    raise SystemExit("FAIL: synthetic-startup-inference regression test was removed.")

# Both production package paths must carry exactly one resource-tracker hook.
standard = read("scripts/prepare-macos-bundle.sh")
preserve = read("scripts/prepare-macos-bundle-preserve.sh")
hook = "pyi_rth_rdt_resource_tracker.py"
for name, source in (("standard packager", standard), ("preserve packager", preserve)):
    if source.count("--runtime-hook") != 1 or source.count(hook) != 1:
        raise SystemExit(f"FAIL: {name} must include exactly one resource-tracker runtime hook.")

if "test_both_packagers_include_exactly_one_hook" not in packager_test:
    raise SystemExit("FAIL: packaging-hook regression test was removed.")

# Rust single-candidate result handling must retain its behavior tests.
required_rust_cases = (
    "native_benchmark_selected_devices_valid",
    "native_benchmark_selection_contract_tests",
    "accepts_single_gpu",
    "accepts_single_cpu",
    "accepts_gpu_cpu_pair_in_either_order",
    "rejects_empty_duplicate_unknown_and_more_than_two_devices",
)
for item in required_rust_cases:
    if item not in rust:
        raise SystemExit(f"FAIL: Rust single-candidate contract missing: {item}")

# Every canonical macOS build must run the full release gate before compilation.
gate_call = 'bash "$ROOT/scripts/run-release-contracts.sh"'
compile_call = "npm run tauri -- build"
if build.count(gate_call) != 1:
    raise SystemExit("FAIL: canonical build must invoke the release gate exactly once.")
if build.index(gate_call) > build.index(compile_call):
    raise SystemExit("FAIL: release gate occurs after app compilation.")

print("PASS: benchmark sys-import regression remains release-gated.")
print("PASS: single-GPU and single-CPU result tests remain in Rust suite.")
print("PASS: synthetic startup inference remains regression-tested.")
print("PASS: standard and preserve packagers both retain the runtime hook.")
print("PASS: response/API benchmark behavior suites remain release-gated.")
print("PASS: app compilation cannot precede the mandatory release gate.")
