#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

PY312="${PYTHON312:-}"
if [[ -z "$PY312" ]]; then
  if command -v python3.12 >/dev/null 2>&1; then
    PY312="$(command -v python3.12)"
  elif [[ -x "$HOME/.local/bin/python3.12" ]]; then
    PY312="$HOME/.local/bin/python3.12"
  else
    echo "STOP: Python 3.12 required; set PYTHON312=/path/to/python3.12"
    exit 2
  fi
fi

PY312_VERSION="$("$PY312" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
[[ "$PY312_VERSION" == 3.12.* ]] || {
  echo "STOP: expected Python 3.12, found $PY312_VERSION"
  exit 2
}

if command -v sha256sum >/dev/null 2>&1; then
  REQ_HASH="$(sha256sum backend/linux/requirements-linux.txt | awk '{print substr($1,1,16)}')"
else
  REQ_HASH="$(shasum -a 256 backend/linux/requirements-linux.txt | awk '{print substr($1,1,16)}')"
fi

CACHE="${RDT_TEST_CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/rdt-connect/test-envs}"
VENV="$CACHE/linux-py${PY312_VERSION}-${REQ_HASH}"
mkdir -p "$CACHE"

if [[ ! -x "$VENV/bin/python" ]]; then
  echo "Creating cached Linux test environment: Python $PY312_VERSION"
  "$PY312" -m venv "$VENV"
fi

if [[ ! -f "$VENV/.requirements-${REQ_HASH}" ]]; then
  echo "Installing pinned Linux test requirements ($REQ_HASH)"
  "$VENV/bin/python" -m pip install --disable-pip-version-check -q \
    -r backend/linux/requirements-linux.txt
  : > "$VENV/.requirements-${REQ_HASH}"
else
  echo "Reusing cached Linux test environment ($REQ_HASH)"
fi

failures=0
run_suite() {
  local label="$1"
  shift
  echo
  echo "===== $label ====="
  if "$@"; then
    echo "PASS: $label"
  else
    local rc=$?
    echo "FAIL: $label (exit $rc)"
    failures=$((failures + 1))
  fi
}

run_suite "Whisper / hardware / release contracts" \
  "$VENV/bin/python" -m unittest discover -s backend/whisper_cpp/tests -p 'test_*.py' -q

run_suite "Linux backend / packaging contracts" \
  "$VENV/bin/python" -m unittest discover -s backend/linux/tests -p 'test_*.py' -q

run_suite "Windows backend / installer contracts" \
  "$VENV/bin/python" -m unittest discover -s backend/windows/tests -p 'test_*.py' -q

run_suite "Connect / localization / desktop contracts" \
  bash -c 'cd rdt_connect && exec "$1" -m unittest discover -s scripts -p "test_*.py" -q' _ "$VENV/bin/python"

run_suite "Platform build-stamping contract" \
  node rdt_connect/scripts/test-platform-build-stamping.cjs

if command -v actionlint >/dev/null 2>&1; then
  run_suite "GitHub workflow lint" \
    actionlint -ignore 'unexpected key "queue" for "concurrency" section' .github/workflows/*.yml
elif [[ "${RDT_REQUIRE_ACTIONLINT:-0}" == "1" ]]; then
  echo "FAIL: CI requires actionlint, but it is not installed" >&2
  failures=$((failures + 1))
else
  echo "SKIP: actionlint not installed (local optional check)"
fi

if command -v gh >/dev/null 2>&1 && gh actions-lock --help >/dev/null 2>&1; then
  run_suite "Pinned GitHub Actions lock" gh actions-lock --verify --fail-on-warning
elif [[ "${RDT_REQUIRE_ACTIONS_LOCK:-0}" == "1" ]]; then
  echo "FAIL: CI requires gh-actions-lock, but it is not installed" >&2
  failures=$((failures + 1))
else
  echo "SKIP: gh-actions-lock not installed (local optional check)"
fi

run_suite "Staged and unstaged patch hygiene" bash -c 'git diff --check && git diff --cached --check'

echo
if (( failures )); then
  echo "FAILED: $failures validation group(s)."
  exit 1
fi
echo "PASS: all available platform contract checks."
