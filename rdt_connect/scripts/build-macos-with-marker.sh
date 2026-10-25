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
if [[ ! "$MARKER" =~ ^[01][A-Z][0-9]{2}$ ]]; then
  echo "ERROR: invalid build marker: $MARKER" >&2
  exit 2
fi
export RDT_BUILD_ID="$MARKER"
printf 'RDT Connect source-patch marker: %s\n' "$RDT_BUILD_ID"

case "${RDT_SKIP_UPDATER_ARTIFACTS:-0}" in
  0|1) ;;
  *) echo "ERROR: RDT_SKIP_UPDATER_ARTIFACTS must be 0 or 1." >&2; exit 2 ;;
esac
case "${RDT_BUILD_APP_ONLY:-0}" in
  0|1) ;;
  *) echo "ERROR: RDT_BUILD_APP_ONLY must be 0 or 1." >&2; exit 2 ;;
esac
APP_ONLY="${RDT_BUILD_APP_ONLY:-0}"

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
node "$ROOT/scripts/build-versioning.cjs" validate "$VERSION" "$MARKER"

UPDATER_DIR="$ROOT/src-tauri/target/release/bundle/macos"
UPDATER_ARCHIVE="$UPDATER_DIR/${PRODUCT}.app.tar.gz"
UPDATER_SIGNATURE="$UPDATER_ARCHIVE.sig"
artifact_sha256() {
  if [[ -f "$1" ]]; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    printf 'MISSING'
  fi
}
BEFORE_ARCHIVE_SHA256="$(artifact_sha256 "$UPDATER_ARCHIVE")"
BEFORE_SIGNATURE_SHA256="$(artifact_sha256 "$UPDATER_SIGNATURE")"

DMG_DIR="$ROOT/src-tauri/target/release/bundle/dmg"
RAW="$DMG_DIR/${PRODUCT}_${VERSION}_aarch64.dmg"
FINAL="$DMG_DIR/${PRODUCT// /_}_${VERSION}_${MARKER}_Tauri_Standard_aarch64.dmg"
APP_BUNDLE="$ROOT/src-tauri/target/release/bundle/macos/${PRODUCT}.app"

# A custom-layout DMG only needs the .app bundle. Avoid creating the standard
# Tauri DMG at all in this mode, and never collide with an existing release DMG.
if [[ "$APP_ONLY" == "0" ]]; then
  mkdir -p "$DMG_DIR"
  for candidate in "$RAW" "$FINAL"; do
    if [[ -e "$candidate" || -L "$candidate" ]]; then
      echo "ERROR: refusing to overwrite existing DMG: $candidate" >&2
      exit 2
    fi
  done
  echo "Standard DMG destination: $(basename "$FINAL")"
else
  echo "Build mode: .app only; custom DMG assembly follows in a separate step."
fi

echo "Canonical SemVer: $VERSION"
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

# RDT_ABOUT_BUNDLE_VERSION_PATCH
# Keep macOS's CFBundleVersion numeric while showing the full RDT marker
# in Credits.html. Changes to tauri.conf.json are temporary and restored.
mkdir -p "$ROOT/build"
CONF_BACKUP_CANDIDATE="$ROOT/build/tauri.conf.restore-${MARKER}-$$.json"
if [[ -e "$CONF_BACKUP_CANDIDATE" || -L "$CONF_BACKUP_CANDIDATE" ]]; then
  echo "ERROR: config backup path already exists: $CONF_BACKUP_CANDIDATE" >&2
  exit 2
fi
cp -p "$CONF" "$CONF_BACKUP_CANDIDATE"
CONF_BACKUP="$CONF_BACKUP_CANDIDATE"

python3 - "$CONF" "$MARKER" "${RDT_SKIP_UPDATER_ARTIFACTS:-0}" <<'PY_TEMP_BUNDLE_METADATA'
import json
import re
import sys
from pathlib import Path

p = Path(sys.argv[1])
marker = sys.argv[2]
skip_updater = sys.argv[3]

match = re.fullmatch(r"([0-9])([A-Z])([0-9]{2})", marker)
if not match:
    raise SystemExit(f"ERROR: cannot derive numeric macOS build version from {marker!r}")
family = int(match.group(1))
letter = match.group(2)
sequence = int(match.group(3))
bundle_version = ((family * 26 + ord(letter) - ord("D")) * 100) + sequence
if bundle_version < 1:
    raise SystemExit(f"ERROR: marker is outside the supported numeric build range: {marker!r}")
if skip_updater not in {"0", "1"}:
    raise SystemExit(f"ERROR: invalid updater artifact mode: {skip_updater!r}")

config = json.loads(p.read_text())
bundle = config.setdefault("bundle", {})
macos = bundle.setdefault("macOS", {})
macos["bundleVersion"] = str(bundle_version)

if skip_updater == "1":
    if bundle.get("createUpdaterArtifacts") is not True:
        raise SystemExit(
            "ERROR: expected bundle.createUpdaterArtifacts=true before "
            "temporary override."
        )
    bundle["createUpdaterArtifacts"] = False

p.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n")
print(
    f"Temporary macOS CFBundleVersion: {bundle_version} "
    f"(source marker {marker})."
)
if skip_updater == "1":
    print("Updater artifact generation temporarily disabled for this build.")
PY_TEMP_BUNDLE_METADATA
if [[ "$APP_ONLY" == "1" ]]; then
  if npm run tauri -- build --bundles app; then
    BUILD_STATUS=0
  else
    BUILD_STATUS=$?
  fi
else
  if npm run tauri -- build; then
    BUILD_STATUS=0
  else
    BUILD_STATUS=$?
  fi
fi
restore_config
trap - EXIT INT TERM

if [[ "$BUILD_STATUS" -ne 0 ]]; then
  echo "ERROR: Tauri build failed (exit $BUILD_STATUS)." >&2
  exit "$BUILD_STATUS"
fi

if [[ "$APP_ONLY" == "1" ]]; then
  if [[ ! -d "$APP_BUNDLE" || ! -f "$APP_BUNDLE/Contents/MacOS/rdt-connect-desktop" ]]; then
    echo "ERROR: Tauri did not produce the expected .app bundle: $APP_BUNDLE" >&2
    exit 2
  fi
  echo "PASS: app bundle built for marker $RDT_BUILD_ID: $APP_BUNDLE"

  if [[ "${RDT_SKIP_UPDATER_ARTIFACTS:-0}" == "0" ]]; then
    for artifact in "$UPDATER_ARCHIVE" "$UPDATER_SIGNATURE"; do
      if [[ ! -s "$artifact" ]]; then
        echo "ERROR: required updater artifact missing or empty: $artifact" >&2
        exit 2
      fi
    done

    AFTER_ARCHIVE_SHA256="$(artifact_sha256 "$UPDATER_ARCHIVE")"
    AFTER_SIGNATURE_SHA256="$(artifact_sha256 "$UPDATER_SIGNATURE")"
    if [[ "$AFTER_ARCHIVE_SHA256" == "$BEFORE_ARCHIVE_SHA256" ||
          "$AFTER_SIGNATURE_SHA256" == "$BEFORE_SIGNATURE_SHA256" ]]; then
      echo "ERROR: updater archive/signature were not both refreshed by this build." >&2
      echo "Archive: $UPDATER_ARCHIVE" >&2
      echo "Signature: $UPDATER_SIGNATURE" >&2
      exit 2
    fi
    echo "PASS: updater archive and signature refreshed by this build."
  fi

  echo "The next step is python3 scripts/build-custom-dmg.py; it will reject a stale app marker."
  exit 0
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
