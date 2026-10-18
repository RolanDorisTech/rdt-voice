#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# This is the active source-patch marker, NOT a build counter.
MARKER_FILE="$ROOT/scripts/build-id-sequence.txt"
if [[ ! -f "$MARKER_FILE" ]]; then
  echo "ERROR: missing build marker: $MARKER_FILE" >&2
  exit 2
fi
MARKER="$(tr -d '[:space:]' < "$MARKER_FILE")"
if [[ ! "$MARKER" =~ ^0[A-Z][0-9]{2}$ ]]; then
  echo "ERROR: invalid build marker: $MARKER" >&2
  exit 2
fi
export RDT_BUILD_ID="$MARKER"
printf 'RDT Connect source-patch marker: %s\n' "$RDT_BUILD_ID"

case "${RDT_SKIP_UPDATER_ARTIFACTS:-0}" in
  0|1) ;;
  *) echo "ERROR: RDT_SKIP_UPDATER_ARTIFACTS must be 0 or 1." >&2; exit 2 ;;
esac

# Require package.json, Tauri config, Cargo.toml, and Cargo.lock to agree.
METADATA="$(python3 - "$ROOT" <<'PY_VERSION_SOURCE'
import json, re, sys
from pathlib import Path

root = Path(sys.argv[1])
package = json.loads((root / "package.json").read_text())
tauri = json.loads((root / "src-tauri/tauri.conf.json").read_text())
cargo_text = (root / "src-tauri/Cargo.toml").read_text()
lock_text = (root / "src-tauri/Cargo.lock").read_text()

section = re.search(r"(?ms)^\[package\]\s*(.*?)(?=^\[|\Z)", cargo_text)
cv = re.search(r'(?m)^version\s*=\s*"([^"]+)"\s*$', section.group(1)) if section else None
lv = re.search(
    r'(?ms)^\[\[package\]\]\s*\nname = "rdt-connect-desktop"\s*\nversion = "([^"]+)"',
    lock_text
)
versions = {
    "package.json": package.get("version"),
    "tauri.conf.json": tauri.get("version"),
    "Cargo.toml": cv.group(1) if cv else None,
    "Cargo.lock": lv.group(1) if lv else None,
}
if not all(isinstance(v, str) for v in versions.values()):
    raise SystemExit(f"ERROR: could not read all four version sources: {versions}")
if len(set(versions.values())) != 1:
    raise SystemExit(f"ERROR: version mismatch: {versions}")

version = tauri["version"]
semver = r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
if not re.fullmatch(semver, version):
    raise SystemExit(f"ERROR: invalid full SemVer version: {version}")
product = tauri.get("productName")
if not isinstance(product, str) or not product:
    raise SystemExit("ERROR: productName missing.")
print(f"{product}\t{version}", end="")
PY_VERSION_SOURCE
)"
IFS=$'\t' read -r PRODUCT VERSION <<< "$METADATA"

DMG_DIR="$ROOT/src-tauri/target/release/bundle/dmg"
mkdir -p "$DMG_DIR"
RAW="$DMG_DIR/${PRODUCT}_${VERSION}_aarch64.dmg"
FINAL="$DMG_DIR/${PRODUCT// /_}_${VERSION}_Tauri_Standard_aarch64.dmg"

# Refuse collisions before resource preparation or compilation.
for candidate in "$RAW" "$FINAL"; do
  if [[ -e "$candidate" || -L "$candidate" ]]; then
    echo "ERROR: refusing to overwrite existing DMG: $candidate" >&2
    exit 2
  fi
done

echo "Canonical SemVer: $VERSION"
echo "Standard DMG destination: $(basename "$FINAL")"
npm run prepare:desktop-resources

CONF="$ROOT/src-tauri/tauri.conf.json"
CONF_BACKUP=""
restore_config() {
  if [[ -n "$CONF_BACKUP" && -f "$CONF_BACKUP" ]]; then
    cp -p "$CONF_BACKUP" "$CONF"
    if ! cmp -s "$CONF_BACKUP" "$CONF"; then
      echo "ERROR: failed to restore tauri.conf.json exactly." >&2
      return 1
    fi
    rm -f "$CONF_BACKUP"
    CONF_BACKUP=""
    echo "PASS: original tauri.conf.json restored exactly."
  fi
}
trap restore_config EXIT INT TERM

# Optional: skip updater archive/signature generation while the signing
# password is unresolved. The original config is restored even on failure.
if [[ "${RDT_SKIP_UPDATER_ARTIFACTS:-0}" == "1" ]]; then
  mkdir -p "$ROOT/build"
  CONF_BACKUP="$ROOT/build/tauri.conf.restore-${MARKER}-$$.json"
  [[ ! -e "$CONF_BACKUP" ]] || {
    echo "ERROR: config backup path already exists: $CONF_BACKUP" >&2
    exit 2
  }
  cp -p "$CONF" "$CONF_BACKUP"
  python3 - "$CONF" <<'PY_SKIP_UPDATER'
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
config = json.loads(p.read_text())
if config.get("bundle", {}).get("createUpdaterArtifacts") is not True:
    raise SystemExit("ERROR: expected updater-artifact setting to be true before temporary override.")
config["bundle"]["createUpdaterArtifacts"] = False
p.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n")
print("Updater artifact generation temporarily disabled for this build.")
PY_SKIP_UPDATER
fi

if npm run tauri -- build; then
  BUILD_STATUS=0
else
  BUILD_STATUS=$?
fi
restore_config
trap - EXIT INT TERM

if [[ "$BUILD_STATUS" -ne 0 ]]; then
  echo "ERROR: Tauri build failed (exit $BUILD_STATUS)." >&2
  exit "$BUILD_STATUS"
fi

if [[ ! -f "$RAW" ]]; then
  echo "ERROR: Tauri did not produce the expected DMG: $RAW" >&2
  exit 2
fi
if [[ -e "$FINAL" || -L "$FINAL" ]]; then
  echo "ERROR: destination appeared during build; refusing overwrite: $FINAL" >&2
  exit 2
fi

mv "$RAW" "$FINAL"
echo "PASS: full SemVer DMG filename: $(basename "$FINAL")"
shasum -a 256 "$FINAL"
printf 'Packaged with source-patch marker: %s\n' "$RDT_BUILD_ID"
