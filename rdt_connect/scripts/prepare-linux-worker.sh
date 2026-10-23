#!/usr/bin/env bash
# Build the shared CPU worker natively on Ubuntu 24.04 LTS. This is not yet a
# Linux backend-resource/installer script: Linux service/TLS packaging is a later slice.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PINS="$REPO_ROOT/backend/whisper_cpp/engine-pins.json"
PYTHON="${RDT_BUILD_PYTHON:-python3}"
WHISPER_CPP_SOURCE="${WHISPER_CPP_SOURCE:-}"
BUILD_DIR="${RDT_LINUX_WORKER_BUILD_DIR:-$REPO_ROOT/rdt_connect/build/linux-worker}"
"$PYTHON" - <<'PY'
import platform, sys
if sys.version_info[:3] < (3, 11, 0):
    raise SystemExit("Python >=3.11 is required for source pin verification; backend packaging still requires the qualified native 3.13 runtime.")
if platform.system() != "Linux":
    raise SystemExit("Run this worker-preparation script on Linux, not for cross-compilation.")
PY
if [[ -z "$WHISPER_CPP_SOURCE" ]]; then
  WHISPER_CPP_SOURCE="$BUILD_DIR/whisper.cpp"
  mkdir -p "$BUILD_DIR"
  source_repo="$("$PYTHON" -c 'import json,sys;print(json.load(open(sys.argv[1]))["source_repository"])' "$PINS")"
  source_commit="$("$PYTHON" -c 'import json,sys;print(json.load(open(sys.argv[1]))["source_commit"])' "$PINS")"
  if [[ ! -d "$WHISPER_CPP_SOURCE/.git" ]]; then git clone --no-checkout "$source_repo" "$WHISPER_CPP_SOURCE"; fi
  git -C "$WHISPER_CPP_SOURCE" fetch --depth 1 origin "$source_commit"
  git -C "$WHISPER_CPP_SOURCE" checkout --detach "$source_commit"
  git -C "$WHISPER_CPP_SOURCE" submodule update --init --recursive
fi
"$PYTHON" "$REPO_ROOT/backend/whisper_cpp/scripts/verify_engine_checkout.py" --source "$WHISPER_CPP_SOURCE" --pins "$PINS" --output "$BUILD_DIR/engine-pin-report.json"
cmake -S "$REPO_ROOT/backend/whisper_cpp/worker" -B "$BUILD_DIR/cmake" -DWHISPER_CPP_SOURCE="$WHISPER_CPP_SOURCE" -DCMAKE_BUILD_TYPE=Release -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
cmake --build "$BUILD_DIR/cmake" --parallel 2
worker="$BUILD_DIR/cmake/bin/rdt_whisper_worker"
test -x "$worker" || { echo "Native worker output missing: $worker" >&2; exit 4; }
printf 'Native worker built: %s\nPin evidence: %s\n' "$worker" "$BUILD_DIR/engine-pin-report.json"
printf 'This validates only a native CPU worker build, not a packaged Linux GUI/backend, HTTPS trust, or GPU inference.\n'
