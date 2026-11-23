#!/usr/bin/env bash
# Launch RDT Connect's Linux transcription server from a source checkout or bundle.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd -- "${BACKEND_DIR}/.." && pwd)"
WHISPER_CPP_DIR="${BACKEND_DIR}/whisper_cpp"

if [[ ! -f "${SCRIPT_DIR}/server.py" ]]; then
  echo "RDT Linux server source is missing: ${SCRIPT_DIR}/server.py" >&2
  exit 2
fi
if [[ ! -f "${WHISPER_CPP_DIR}/engine.py" || ! -f "${WHISPER_CPP_DIR}/model_registry.py" ]]; then
  echo "Shared whisper.cpp engine/model registry are missing under ${WHISPER_CPP_DIR}" >&2
  exit 2
fi

PYTHON_BIN="${RDT_PYTHON:-${PYTHON:-python3}}"
if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "Python executable not found: ${PYTHON_BIN}" >&2
  exit 2
fi

# Preserve an operator-supplied worker override. Otherwise the shared engine uses
# its deterministic Linux default candidates in backend/whisper_cpp/bin and can
# fall back from GPU/ARM variants to the CPU worker after a failed startup handshake.
if [[ -n "${RDT_WHISPER_WORKER_EXE:-}" ]]; then
  export RDT_WHISPER_WORKER_EXE="$(realpath -m -- "${RDT_WHISPER_WORKER_EXE}")"
fi
export PYTHONPATH="${WHISPER_CPP_DIR}${PYTHONPATH:+:${PYTHONPATH}}"
cd "${REPO_ROOT}"
exec "${PYTHON_BIN}" "${SCRIPT_DIR}/server.py" "$@"
