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
CUDA_MODE="${RDT_ENABLE_CUDA:-0}"
VULKAN_MODE="${RDT_ENABLE_VULKAN:-0}"

normalize_mode() {
  case "$1" in
    1|ON|on|true|TRUE) printf 'ON' ;;
    0|OFF|off|false|FALSE|"") printf 'OFF' ;;
    *) echo "Expected 0/1 or ON/OFF, got: $1" >&2; return 5 ;;
  esac
}
CUDA_MODE="$(normalize_mode "$CUDA_MODE")"
VULKAN_MODE="$(normalize_mode "$VULKAN_MODE")"

if [[ "$CUDA_MODE" == ON ]] && ! command -v nvcc >/dev/null 2>&1; then
  echo "RDT_ENABLE_CUDA requested, but nvcc is missing. Install a compatible CUDA Toolkit." >&2
  exit 5
fi
if [[ "$VULKAN_MODE" == ON ]] && ! command -v glslc >/dev/null 2>&1; then
  echo "RDT_ENABLE_VULKAN requested, but glslc is missing. Install the Vulkan SDK/toolchain." >&2
  exit 5
fi

VARIANT_DIR="$BUILD_DIR/variants"
mkdir -p "$VARIANT_DIR"

build_variant() {
  local variant="$1" cuda="$2" vulkan="$3" output="$4"
  local cmake_build="$BUILD_DIR/cmake-$variant"
  cmake -S "$REPO_ROOT/backend/whisper_cpp/worker" \
    -B "$cmake_build" \
    -DWHISPER_CPP_SOURCE="$WHISPER_CPP_SOURCE" \
    -DRDT_ENABLE_CUDA="$cuda" \
    -DRDT_ENABLE_VULKAN="$vulkan" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
  cmake --build "$cmake_build" --parallel 2
  local worker="$cmake_build/bin/rdt_whisper_worker"
  test -x "$worker" || {
    echo "Native $variant worker output missing: $worker" >&2
    return 4
  }
  install -m 755 "$worker" "$VARIANT_DIR/$output"
  printf 'Built and staged %s\n' "$VARIANT_DIR/$output"
}

# Always build the CPU fallback first.
build_variant cpu OFF OFF rdt_whisper_worker
if [[ "$CUDA_MODE" == ON ]]; then
  build_variant cuda ON OFF rdt_whisper_worker_cuda
fi
if [[ "$VULKAN_MODE" == ON ]]; then
  build_variant vulkan OFF ON rdt_whisper_worker_vulkan
fi

printf 'Worker variants directory: %s\nPin evidence: %s\n' "$VARIANT_DIR" "$BUILD_DIR/engine-pin-report.json"
printf 'Build completion does not prove GPU inference; runtime qualification is still required.\n'
