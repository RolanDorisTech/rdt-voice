#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Use the project's alphanumeric build marker, distinct from the SemVer version.
# The sequence file records the last successful packaged build.
SEQUENCE_FILE="$ROOT/scripts/build-id-sequence.txt"
if [[ ! -f "$SEQUENCE_FILE" ]]; then
  printf 'ERROR: missing build-marker sequence file: %s\n' "$SEQUENCE_FILE" >&2
  exit 2
fi
LAST_MARKER="$(tr -d '[:space:]' < "$SEQUENCE_FILE")"
if [[ ! "$LAST_MARKER" =~ ^(0[A-Z][0-9]{2}R)([0-9]+)$ ]]; then
  printf 'ERROR: unexpected build-marker sequence in %s\n' "$SEQUENCE_FILE" >&2
  exit 2
fi
PREFIX="${BASH_REMATCH[1]}"
REVISION="${BASH_REMATCH[2]}"
NEXT_REVISION=$((10#$REVISION + 1))
NEXT_MARKER="${PREFIX}${NEXT_REVISION}"
export RDT_BUILD_ID="$(date '+%y%m%d').${NEXT_MARKER}"
printf 'RDT Connect build marker: %s\n' "$RDT_BUILD_ID"

# Only advance the stored counter after a successful package build.
npm run prepare:desktop-resources
npm run tauri -- build
printf '%s\n' "$NEXT_MARKER" > "$SEQUENCE_FILE.tmp"
mv "$SEQUENCE_FILE.tmp" "$SEQUENCE_FILE"
printf 'Stored last-successful marker sequence: %s\n' "$NEXT_MARKER"
