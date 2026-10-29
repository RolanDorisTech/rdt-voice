#!/usr/bin/env bash
# Build one Linux worker variant from a locally supplied whisper.cpp source tree.
# This script does not download source, install packages, fetch models, or run inference.
set -euo pipefail
PINNED_WHISPER_CPP_COMMIT="d1be6fde11ac6e0407606b4e42fe72d34add8037"

usage() {
  cat >&2 <<'USAGE'
Usage: build-linux-worker.sh [cpu|cuda|vulkan|dotprod|avx2] [WHISPER_CPP_SOURCE]

The source tree must be a clean Git checkout at the pinned whisper.cpp v1.9.5 commit.
Defaults: variant=cpu; source=$WHISPER_CPP_SOURCE.
Optional environment: RDT_WORKER_BUILD_ROOT, RDT_BUILD_JOBS.
Builds into backend/whisper_cpp/bin with a deterministic engine candidate name.
USAGE
}

variant="${1:-cpu}"
source_arg="${2:-${WHISPER_CPP_SOURCE:-}}"
case "$variant" in
  cpu|cuda|vulkan|dotprod|avx2) ;;
  -h|--help) usage; exit 0 ;;
  *) usage; exit 2 ;;
esac

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "This worker build script supports Linux only." >&2
  exit 2
fi
if [[ -z "$source_arg" || ! -f "$source_arg/CMakeLists.txt" ]]; then
  echo "Provide WHISPER_CPP_SOURCE pointing to the local whisper.cpp source tree." >&2
  exit 2
fi
source_root="$(cd -- "$source_arg" && pwd -P)"
if ! command -v git >/dev/null 2>&1; then
  echo "git is required to verify the pinned whisper.cpp source revision." >&2
  exit 2
fi
source_top="$(git -C "$source_root" rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$source_top" || "$(cd -- "$source_top" && pwd -P)" != "$source_root" ]]; then
  echo "WHISPER_CPP_SOURCE must be the root of a Git checkout; source archives cannot prove provenance." >&2
  exit 2
fi
source_commit="$(git -C "$source_root" rev-parse --verify HEAD 2>/dev/null || true)"
if [[ "$source_commit" != "$PINNED_WHISPER_CPP_COMMIT" ]]; then
  echo "Unsupported whisper.cpp source commit: ${source_commit:-unknown}" >&2
  echo "Required pinned commit: $PINNED_WHISPER_CPP_COMMIT (v1.9.5)." >&2
  exit 2
fi
if [[ -n "$(git -C "$source_root" status --porcelain --untracked-files=all)" ]]; then
  echo "WHISPER_CPP_SOURCE must be clean; modified or untracked source files invalidate the pin." >&2
  exit 2
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
whisper_cpp_dir="$(cd -- "$script_dir/.." && pwd)"
worker_source="$whisper_cpp_dir/worker"
bin_dir="$whisper_cpp_dir/bin"
cuda=OFF
vulkan=OFF
dotprod=OFF
avx2=OFF
case "$variant" in
  cpu) target_name="rdt_whisper_worker" ;;
  avx2)
    machine="$(uname -m)"
    case "$machine" in x86_64|amd64) ;; *) echo "avx2 variant requires Linux x86_64; got $machine." >&2; exit 2 ;; esac
    target_name="rdt_whisper_worker_avx2"; avx2=ON
    ;;
  cuda) target_name="rdt_whisper_worker_cuda"; cuda=ON ;;
  vulkan) target_name="rdt_whisper_worker_vulkan"; vulkan=ON ;;
  dotprod)
    machine="$(uname -m)"
    case "$machine" in aarch64|arm64) ;; *) echo "dotprod variant requires Linux ARM64; got $machine." >&2; exit 2 ;; esac
    target_name="rdt_whisper_worker_dotprod"; dotprod=ON
    ;;
esac
if [[ -n "${RDT_WORKER_BUILD_ROOT:-}" ]]; then
  build_root="${RDT_WORKER_BUILD_ROOT%/}/$variant"
else
  build_root="$whisper_cpp_dir/.build-linux-$variant"
fi
jobs="${RDT_BUILD_JOBS:-2}"
if ! [[ "$jobs" =~ ^[1-9][0-9]*$ ]]; then
  echo "RDT_BUILD_JOBS must be a positive integer." >&2
  exit 2
fi

cmake -S "$worker_source" -B "$build_root" \
  -DWHISPER_CPP_SOURCE="$source_root" \
  -DRDT_WHISPER_CPP_COMMIT="$source_commit" \
  -DRDT_ENABLE_CUDA="$cuda" \
  -DRDT_ENABLE_VULKAN="$vulkan" \
  -DRDT_ENABLE_ARM_DOTPROD="$dotprod" \
  -DRDT_ENABLE_X64_AVX2="$avx2" \
  -DCMAKE_EXPORT_COMPILE_COMMANDS=ON \
  -DCMAKE_BUILD_TYPE=Release
cmake --build "$build_root" --config Release --target rdt_whisper_worker --parallel "$jobs"

built="$build_root/bin/rdt_whisper_worker"
if [[ ! -f "$built" ]]; then
  echo "CMake completed without the expected worker binary: $built" >&2
  exit 1
fi
mkdir -p "$bin_dir"
install -m 0755 "$built" "$bin_dir/$target_name"
if command -v sha256sum >/dev/null 2>&1; then
  digest="$(sha256sum "$bin_dir/$target_name" | awk '{print $1}')"
else
  digest="unavailable"
fi
printf 'Installed %s\nSHA-256: %s\n' "$bin_dir/$target_name" "$digest"
printf 'Next step: perform a real model-backed inference qualification before advertising this worker as usable.\n'
