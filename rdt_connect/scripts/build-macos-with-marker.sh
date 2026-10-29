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
if [[ ! "$MARKER" =~ ^1[A-Z][0-9]{2}$ ]]; then
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

# Make the existing updater key available to Tauri's build-time signer.
KEY_PATH="$HOME/.tauri/rdt-connect-updater.key"
if [[ "${RDT_SKIP_UPDATER_ARTIFACTS:-0}" == "0" ]]; then
  if [[ ! -s "$KEY_PATH" ]]; then
    echo "ERROR: existing updater signing key is missing." >&2
    exit 4
  fi
  export TAURI_SIGNING_PRIVATE_KEY_PATH="$KEY_PATH"
  TAURI_SIGNING_PRIVATE_KEY="$(cat "$KEY_PATH")"
  if [[ -z "$TAURI_SIGNING_PRIVATE_KEY" ]]; then
    echo "ERROR: updater signing key is empty." >&2
    exit 4
  fi
  export TAURI_SIGNING_PRIVATE_KEY
fi

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
CREDITS_SOURCE="$ROOT/src-tauri/resources/Credits.html"
CREDITS_BACKUP=""

restore_credits() {
  if [[ -n "${CREDITS_BACKUP:-}" && -f "$CREDITS_BACKUP" ]]; then
    if cp -p "$CREDITS_BACKUP" "$CREDITS_SOURCE" &&
       cmp -s "$CREDITS_BACKUP" "$CREDITS_SOURCE"; then
      echo "PASS: original Credits.html restored exactly."
      CREDITS_BACKUP=""
    else
      echo "ERROR: Credits.html restoration failed; backup retained: $CREDITS_BACKUP" >&2
      return 1
    fi
  fi
}

restore_all() {
  local status=0
  restore_config || status=$?
  restore_credits || status=$?
  return "$status"
}
trap restore_all EXIT INT TERM

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
# Stamp native macOS About with the same marker as the app.
if [[ ! -s "$CREDITS_SOURCE" ]]; then
  echo "ERROR: native About Credits.html is missing or empty." >&2
  exit 4
fi

CREDITS_BACKUP_CANDIDATE="$ROOT/build/Credits.restore-${MARKER}-$$.html"
if [[ -e "$CREDITS_BACKUP_CANDIDATE" || -L "$CREDITS_BACKUP_CANDIDATE" ]]; then
  echo "ERROR: Credits backup already exists: $CREDITS_BACKUP_CANDIDATE" >&2
  exit 4
fi
if ! cp -p "$CREDITS_SOURCE" "$CREDITS_BACKUP_CANDIDATE"; then
  echo "ERROR: could not back up native About Credits.html." >&2
  exit 4
fi
CREDITS_BACKUP="$CREDITS_BACKUP_CANDIDATE"

if ! python3 - "$CREDITS_SOURCE" "$MARKER" <<'PY_CREDITS_MARKER'
import re, sys
from pathlib import Path

p = Path(sys.argv[1])
marker = sys.argv[2]
text = p.read_text()
pattern = re.compile(
    r'(<p\s+class=["\']build["\']>\s*)Build\s+[0-9][A-Z][0-9]{2}(\s*</p>)'
)
updated, count = pattern.subn(
    lambda m: f"{m.group(1)}Build {marker}{m.group(2)}", text
)
if count != 1:
    raise SystemExit(f"ERROR: expected one native About build marker; found {count}.")
p.write_text(updated)
print(f"PASS: native macOS About stamped with Build {marker}.")
PY_CREDITS_MARKER
then
  exit 4
fi

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
    # Deduplicate identical MLX shaders only in the macOS app-only release path.
    MLX_INTERNAL="$APP_BUNDLE/Contents/Resources/rdt_voice_backend/_internal"
    MLX_CANONICAL="$MLX_INTERNAL/mlx/mlx.metallib"
    MLX_TOP="$MLX_INTERNAL/mlx.metallib"
    MLX_NESTED="$MLX_INTERNAL/mlx/lib/mlx.metallib"

    for shader in "$MLX_CANONICAL" "$MLX_TOP" "$MLX_NESTED"; do
      if [[ ! -f "$shader" || -L "$shader" ]]; then
        echo "ERROR: expected regular MLX shader before deduplication: $shader" >&2
        exit 4
      fi
    done

    MLX_HASH="$(shasum -a 256 "$MLX_CANONICAL" | awk '{print $1}')"
    for shader in "$MLX_TOP" "$MLX_NESTED"; do
      shader_hash="$(shasum -a 256 "$shader" | awk '{print $1}')"
      if [[ "$shader_hash" != "$MLX_HASH" ]]; then
        echo "ERROR: MLX shader differs from canonical copy; refusing deduplication: $shader" >&2
        exit 4
      fi
    done

    rm "$MLX_TOP" "$MLX_NESTED"
    ln -s "mlx/mlx.metallib" "$MLX_TOP"
    ln -s "../mlx.metallib" "$MLX_NESTED"

    if ! codesign --force --deep --sign - "$APP_BUNDLE" ||
       ! codesign --verify --deep --strict "$APP_BUNDLE"; then
      echo "ERROR: optimized app failed ad-hoc signing/verification." >&2
      exit 4
    fi

    TMP_ARCHIVE="${UPDATER_ARCHIVE}.shader-dedupe.tmp.$$"
    rm -f "$TMP_ARCHIVE"
    if ! tar -czf "$TMP_ARCHIVE" -C "$UPDATER_DIR" "${PRODUCT}.app"; then
      rm -f "$TMP_ARCHIVE"
      echo "ERROR: failed to recreate optimized updater archive." >&2
      exit 4
    fi
    if ! python3 - "$TMP_ARCHIVE" "$PRODUCT.app" <<'PYVALIDATE'
import sys
import tarfile

archive_path, app_root = sys.argv[1:]
base = app_root + "/Contents/Resources/rdt_voice_backend/_internal/"
expected = {
    base + "mlx/mlx.metallib": ("file", ""),
    base + "mlx.metallib": ("symlink", "mlx/mlx.metallib"),
    base + "mlx/lib/mlx.metallib": ("symlink", "../mlx.metallib"),
}
try:
    with tarfile.open(archive_path, "r:gz") as tar:
        members = {m.name: m for m in tar.getmembers()}
        for name, (kind, target) in expected.items():
            member = members.get(name)
            if member is None:
                raise RuntimeError(f"missing archive member: {name}")
            if kind == "file" and not member.isfile():
                raise RuntimeError(f"canonical shader is not a regular file: {name}")
            if kind == "symlink" and (not member.issym() or member.linkname != target):
                raise RuntimeError(f"wrong symlink: {name} -> {member.linkname!r}")
        shader_members = [
            m for m in members.values()
            if m.name.endswith("/mlx.metallib") and (m.isfile() or m.issym())
        ]
        if len(shader_members) != 3:
            raise RuntimeError(f"expected 3 shader paths, found {len(shader_members)}")
except Exception as exc:
    print(f"ERROR: optimized updater archive validation failed: {exc}", file=sys.stderr)
    sys.exit(1)
print("PASS: updater archive contains the canonical shader and both correct relative symlinks.")
PYVALIDATE
    then
      rm -f "$TMP_ARCHIVE"
      echo "ERROR: refusing to publish an invalid optimized updater archive." >&2
      exit 4
    fi

    mv "$TMP_ARCHIVE" "$UPDATER_ARCHIVE"

    KEY_PATH="$HOME/.tauri/rdt-connect-updater.key"
    if [[ ! -s "$KEY_PATH" ]]; then
      echo "ERROR: existing updater signing key is missing." >&2
      exit 4
    fi
    : "${TAURI_SIGNING_PRIVATE_KEY_PASSWORD:?Set the existing updater-key password before building.}"

    # Sign a same-named temporary archive; preserve the current signature on failure.
    SIGN_WORK_DIR="$(mktemp -d "$UPDATER_DIR/.updater-sign.XXXXXX")"
    SIGN_TEMP_ARCHIVE="$SIGN_WORK_DIR/$(basename "$UPDATER_ARCHIVE")"

    if ! cp -p "$UPDATER_ARCHIVE" "$SIGN_TEMP_ARCHIVE"; then
      rm -rf "$SIGN_WORK_DIR"
      echo "ERROR: could not stage updater archive for signing." >&2
      exit 4
    fi

    if ! env -u TAURI_SIGNING_PRIVATE_KEY "$ROOT/node_modules/.bin/tauri" signer sign \
      --private-key-path "$KEY_PATH" \
      --app-version "$VERSION" \
      "$SIGN_TEMP_ARCHIVE"; then
      rm -rf "$SIGN_WORK_DIR"
      echo "ERROR: optimized updater archive signing failed." >&2
      exit 4
    fi

    MINISIGN_BIN="$(command -v minisign || true)"
    PUBLIC_KEY_PATH="$HOME/.tauri/rdt-connect-updater.key.pub"

    if [[ -z "$MINISIGN_BIN" || ! -s "$PUBLIC_KEY_PATH" ]]; then
      rm -rf "$SIGN_WORK_DIR"
      echo "ERROR: minisign verifier or updater public key is missing." >&2
      exit 4
    fi

    DECODED_PUBLIC_KEY="$SIGN_WORK_DIR/updater-public-key.minisign.pub"
    DECODED_SIGNATURE="$SIGN_WORK_DIR/updater-signature.minisig"

    if [[ ! -s "$SIGN_TEMP_ARCHIVE" ]]; then
      rm -rf "$SIGN_WORK_DIR"
      echo "ERROR: updater archive is missing or empty before verification." >&2
      exit 4
    fi

    if [[ ! -s "$SIGN_TEMP_ARCHIVE.sig" ]]; then
      rm -rf "$SIGN_WORK_DIR"
      echo "ERROR: updater signature is missing or empty." >&2
      exit 4
    fi

    if ! base64 -D -i "$PUBLIC_KEY_PATH" -o "$DECODED_PUBLIC_KEY" ||
       ! base64 -D -i "$SIGN_TEMP_ARCHIVE.sig" -o "$DECODED_SIGNATURE"; then
      rm -rf "$SIGN_WORK_DIR"
      echo "ERROR: could not decode updater signature or public key." >&2
      exit 4
    fi

    if ! "$MINISIGN_BIN" -V -m "$SIGN_TEMP_ARCHIVE" \
         -x "$DECODED_SIGNATURE" -p "$DECODED_PUBLIC_KEY" -q ||
       ! codesign --verify --deep --strict "$APP_BUNDLE"; then
      rm -rf "$SIGN_WORK_DIR"
      echo "ERROR: optimized app/updater artifact validation failed." >&2
      exit 4
    fi

    # The staged signature is on the same filesystem; replace only after validation.
    if ! mv -f "$SIGN_TEMP_ARCHIVE.sig" "$UPDATER_SIGNATURE"; then
      rm -rf "$SIGN_WORK_DIR"
      echo "ERROR: could not promote validated updater signature." >&2
      exit 4
    fi
    rm -rf "$SIGN_WORK_DIR"

    echo "PASS: duplicate MLX shaders replaced with relative symlinks."
    echo "PASS: app re-signed and updater archive regenerated/signed."
  fi

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
