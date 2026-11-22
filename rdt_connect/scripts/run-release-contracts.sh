#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Reuse the marker-specific isolated target when called by the release build.
# Standalone test runs get their own target and never touch src-tauri/target.
if [[ -z "${CARGO_TARGET_DIR:-}" ]]; then
  CARGO_TARGET_DIR="${RDT_RELEASE_CONTRACT_TARGET_DIR:-$ROOT/build/rdt-connect-release-contract-target}"
  export CARGO_TARGET_DIR
fi

if [[ "$CARGO_TARGET_DIR" == "$ROOT/src-tauri/target" ]]; then
  echo "ERROR: release-contract tests may not use protected src-tauri/target." >&2
  exit 2
fi

echo "===== RDT CONNECT PERMANENT RELEASE-CONTRACT GATE ====="
printf 'Cargo test target: %s\n' "$CARGO_TARGET_DIR"

SHELL_SCRIPTS=(
  scripts/build-macos-with-marker.sh
  scripts/prepare-macos-bundle.sh
  scripts/prepare-macos-bundle-preserve.sh
  scripts/pyi_rth_rdt_resource_tracker.py
)

for script in "${SHELL_SCRIPTS[@]}"; do
  if [[ "$script" == *.sh ]]; then
    if [[ ! -f "$ROOT/$script" ]]; then
      echo "ERROR: required release script missing: $script" >&2
      exit 2
    fi
    echo "===== SHELL SYNTAX: $script ====="
    bash -n "$ROOT/$script"
  fi
done

PYTHON_TESTS=(
  test_release_contract_gate.py
  test_connect_socket_first_startup.py
  test_connect_single_process_runtime_contract.py
  test_preserve_packager_resource_tracker_contract.py
  test_model_download_startup_contract.py
  test_model_download_reliability.py
  test-connect-benchmark-response-contract.py
  test-connect-benchmark-api.py
  test-benchmark-fixtures.py
  test-manual-benchmark-fixture.py
  test-offline-launch-contract.py
  test-no-librispeech-assets.py
  test_macos_build_pipeline_contract.py
  test_macos_release_identity.py
  test-dmg-guide-source-contract.py
  test-benchmark-mandatory-budget-contract.py
  test-benchmark-manual-options-contract.py
  test-hardware-offline-preference-contract.py
)

for test_file in "${PYTHON_TESTS[@]}"; do
  if [[ ! -s "$ROOT/scripts/$test_file" ]]; then
    echo "ERROR: required release-contract test missing or empty: scripts/$test_file" >&2
    exit 2
  fi
  echo "===== PYTHON RELEASE CONTRACT: $test_file ====="
  python3 "$ROOT/scripts/$test_file"
done

python3 "$ROOT/scripts/test_critical_runtime_regression_sentinel.py"

echo "===== NODE VERSIONING CONTRACT ====="
node "$ROOT/scripts/test-build-versioning.cjs"
node "$ROOT/scripts/test-build-version-sync.cjs"

echo "===== NODE FRONTEND IDENTIFIER CONTRACT ====="
node "$ROOT/scripts/test-frontend-runtime-identifiers.cjs"

echo "===== RUST FORMAT CHECK ====="
cargo fmt --manifest-path "$ROOT/src-tauri/Cargo.toml" -- --check

echo "===== COMPLETE RUST TEST SUITE ====="
cargo test --manifest-path "$ROOT/src-tauri/Cargo.toml"

echo "===== FINAL DIFF VALIDATION ====="
git -C "$ROOT" diff --check

echo "PASS: all mandatory RDT Connect release contracts passed."
