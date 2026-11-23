#!/usr/bin/env bash
set -euo pipefail

PREFIX=""
NOTICE_ROOT=""

while (($#)); do
  case "$1" in
    --prefix)
      PREFIX="${2:?missing --prefix value}"
      shift 2
      ;;
    --notices)
      NOTICE_ROOT="${2:?missing --notices value}"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$PREFIX" || -z "$NOTICE_ROOT" ]]; then
  echo "Usage: build-linux-ffmpeg.sh --prefix DIR --notices DIR" >&2
  exit 2
fi

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "STOP: FFmpeg Linux bundle must be built on native Linux." >&2
  exit 2
fi

for tool in curl gpg xz tar make cc nasm sha256sum ldd readelf readlink python3; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "STOP: required Linux FFmpeg build tool is missing: $tool" >&2
    exit 2
  }
done

PREFIX="$(mkdir -p "$PREFIX" && cd "$PREFIX" && pwd -P)"
NOTICE_ROOT="$(mkdir -p "$NOTICE_ROOT" && cd "$NOTICE_ROOT" && pwd -P)"

if find "$PREFIX" -mindepth 1 -print -quit | grep -q .; then
  echo "STOP: refusing to overwrite nonempty FFmpeg prefix: $PREFIX" >&2
  exit 2
fi

if find "$NOTICE_ROOT" -mindepth 1 -print -quit | grep -q .; then
  echo "STOP: refusing to overwrite nonempty FFmpeg notice directory: $NOTICE_ROOT" >&2
  exit 2
fi

VERSION="9.1"
BASE_URL="https://ffmpeg.org/releases"
KEY_URL="https://ffmpeg.org/ffmpeg-devel.asc"
EXPECTED_PRIMARY_FINGERPRINT="FCF986EA15E6E293A5644F10B4322F04D67658D8"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/rdt-ffmpeg-${VERSION}.XXXXXX")"
trap 'rm -rf -- "$WORK"' EXIT

mkdir -p "$WORK/gnupg" "$NOTICE_ROOT/source"
chmod 0700 "$WORK/gnupg"
export GNUPGHOME="$WORK/gnupg"

TARBALL="$WORK/ffmpeg-${VERSION}.tar.xz"
SIGNATURE="$WORK/ffmpeg-${VERSION}.tar.xz.asc"
KEY_FILE="$WORK/ffmpeg-devel.asc"

echo "===== DOWNLOAD OFFICIAL FFMPEG SOURCE AND SIGNATURE ====="
curl --fail --location --retry 3 \
  --output "$TARBALL" \
  "${BASE_URL}/ffmpeg-${VERSION}.tar.xz"

curl --fail --location --retry 3 \
  --output "$SIGNATURE" \
  "${BASE_URL}/ffmpeg-${VERSION}.tar.xz.asc"

curl --fail --location --retry 3 \
  --output "$KEY_FILE" \
  "$KEY_URL"

echo
echo "===== VERIFY OFFICIAL FFMPEG SIGNING KEY ====="
gpg --batch --import "$KEY_FILE"

if [[ ! "$EXPECTED_PRIMARY_FINGERPRINT" =~ ^[A-Fa-f0-9]{40}$ ]]; then
  echo "STOP: configured FFmpeg primary fingerprint is not a 40-character fingerprint." >&2
  exit 1
fi

echo "===== VERIFY EXACT OFFICIAL FFMPEG PRIMARY KEY ====="
KEY_RECORDS="$(
  gpg --batch --with-colons --fingerprint --list-keys \
    "$EXPECTED_PRIMARY_FINGERPRINT" 2>/dev/null
)"

ACTUAL_PRIMARY_FINGERPRINT="$(
  awk -F: '
    $1 == "pub" { in_primary = 1; next }
    in_primary && $1 == "fpr" { print toupper($10); exit }
  ' <<<"$KEY_RECORDS"
)"

if [[ "$ACTUAL_PRIMARY_FINGERPRINT" != "$EXPECTED_PRIMARY_FINGERPRINT" ]]; then
  echo "STOP: the exact expected FFmpeg primary key is not present in the imported keyring." >&2
  echo "Expected: $EXPECTED_PRIMARY_FINGERPRINT" >&2
  echo "Actual:   ${ACTUAL_PRIMARY_FINGERPRINT:-missing}" >&2
  exit 1
fi

echo "FFMPEG_PRIMARY_KEY_FINGERPRINT=$ACTUAL_PRIMARY_FINGERPRINT"

echo
echo "===== VERIFY DETACHED SIGNATURE AND SIGNER IDENTITY ====="
if ! gpg --batch --status-fd 1 --verify "$SIGNATURE" "$TARBALL" \
    >"$WORK/gpg-verify.status" 2>"$WORK/gpg-verify.log"; then
  cat "$WORK/gpg-verify.log" >&2
  cat "$WORK/gpg-verify.status" >&2
  echo "STOP: FFmpeg source signature verification failed." >&2
  exit 1
fi

cat "$WORK/gpg-verify.log"
cat "$WORK/gpg-verify.status"

if ! awk -v expected="$EXPECTED_PRIMARY_FINGERPRINT" '
  $1 == "[GNUPG:]" && $2 == "VALIDSIG" {
    signer = toupper($3)
    primary = (NF >= 12 ? toupper($12) : signer)
    if (signer == expected || primary == expected) matched = 1
  }
  END { exit !matched }
' "$WORK/gpg-verify.status"; then
  echo "STOP: signature is valid but was not made by the pinned FFmpeg release key or its subkey." >&2
  exit 1
fi

ACTUAL_SIGNING_FINGERPRINT="$(
  awk '$1 == "[GNUPG:]" && $2 == "VALIDSIG" { print toupper($3); exit }' \
    "$WORK/gpg-verify.status"
)"
echo "FFMPEG_SIGNATURE_SIGNER_FINGERPRINT=$ACTUAL_SIGNING_FINGERPRINT"
echo "FFMPEG_SIGNATURE_PRIMARY_KEY_MATCH=PASS"

SOURCE_SHA256="$(sha256sum "$TARBALL" | awk '{print $1}')"
SIGNATURE_SHA256="$(sha256sum "$SIGNATURE" | awk '{print $1}')"

echo
echo "FFMPEG_VERSION=$VERSION"
echo "FFMPEG_SOURCE_SHA256=$SOURCE_SHA256"
echo "FFMPEG_SIGNATURE_SHA256=$SIGNATURE_SHA256"
echo "FFMPEG_SIGNING_KEY=$ACTUAL_FINGERPRINT"
echo "FFMPEG_BUILD_ARCH=$(uname -m)"

echo
echo "===== EXTRACT VERIFIED SOURCE ====="
tar -xJf "$TARBALL" -C "$WORK"
SOURCE_DIR="$WORK/ffmpeg-${VERSION}"

if [[ ! -s "$SOURCE_DIR/COPYING.LGPLv2.1" ]]; then
  echo "STOP: FFmpeg LGPL license text missing from verified source archive." >&2
  exit 1
fi

echo
echo "===== CONFIGURE LGPL-ONLY SHARED RUNTIME ====="
CONFIGURE_ARGS=(
  "--prefix=$PREFIX"
  "--enable-shared"
  "--disable-static"
  "--disable-gpl"
  "--disable-nonfree"
  "--disable-autodetect"
  "--disable-network"
  "--disable-ffplay"
  "--disable-doc"
  "--disable-debug"
  "--extra-ldflags=-Wl,--disable-new-dtags -Wl,-rpath,\$ORIGIN/../lib"
)

printf '%s\n' "${CONFIGURE_ARGS[@]}" \
  | tee "$NOTICE_ROOT/FFMPEG-CONFIGURE-ARGS.txt"

cd "$SOURCE_DIR"
./configure "${CONFIGURE_ARGS[@]}"

if grep -Eq -- '--enable-gpl|--enable-nonfree' config.mak; then
  echo "STOP: generated FFmpeg configuration unexpectedly enables GPL/nonfree." >&2
  exit 1
fi

echo
echo "===== BUILD FFMPEG AND FFPROBE ====="
JOBS="$(getconf _NPROCESSORS_ONLN)"
make -j"$JOBS"
make install

if [[ ! -x "$PREFIX/bin/ffmpeg" || ! -x "$PREFIX/bin/ffprobe" ]]; then
  echo "STOP: FFmpeg or FFprobe executable missing after install." >&2
  exit 1
fi

rm -rf "$PREFIX/include" "$PREFIX/share" "$PREFIX/lib/pkgconfig"
find "$PREFIX/lib" -type f -name '*.a' -delete

echo
echo "===== VERIFY PRIVATE FFMPEG ELF DEPENDENCIES ====="

if [[ ! -d "$PREFIX/lib" ]]; then
  echo "STOP: private FFmpeg library directory is missing: $PREFIX/lib" >&2
  exit 1
fi

echo "--- Verify private library symlink closure"
while IFS= read -r -d '' link; do
  resolved_link="$(readlink -f -- "$link" || true)"
  case "$resolved_link" in
    "$PREFIX"/lib/*)
      ;;
    *)
      echo "STOP: broken or external FFmpeg library symlink: $link -> $resolved_link" >&2
      exit 1
      ;;
  esac
done < <(find "$PREFIX/lib" -type l -print0)

FFMPEG_DEP_PATTERN='^[[:space:]]*(lib(av|sw|postproc)[^[:space:]]*)[[:space:]]+=>[[:space:]]+(/[^[:space:]]+)'

for binary in "$PREFIX/bin/ffmpeg" "$PREFIX/bin/ffprobe"; do
  echo "--- $binary"

  DYNAMIC="$(readelf -d "$binary")"
  printf '%s\n' "$DYNAMIC" | grep -E '\((RPATH|RUNPATH)\)' || true

  if ! grep -Fq '(RPATH)' <<<"$DYNAMIC" ||
     ! grep -Fq '$ORIGIN/../lib' <<<"$DYNAMIC"; then
    echo "STOP: FFmpeg executable lacks the required private DT_RPATH." >&2
    exit 1
  fi

  if grep -Fq '(RUNPATH)' <<<"$DYNAMIC"; then
    echo "STOP: RUNPATH is not accepted; require transitive DT_RPATH resolution." >&2
    exit 1
  fi

  DEPENDENCIES="$(ldd "$binary")"
  printf '%s\n' "$DEPENDENCIES"

  if grep -q 'not found' <<<"$DEPENDENCIES"; then
    echo "STOP: unresolved FFmpeg shared-library dependency: $binary" >&2
    exit 1
  fi

  PRIVATE_FFMPEG_DEPENDENCY_COUNT=0
  while IFS= read -r dependency_line; do
    if [[ "$dependency_line" =~ $FFMPEG_DEP_PATTERN ]]; then
      dependency_path="${BASH_REMATCH[3]}"

      case "$dependency_path" in
        "$PREFIX"/lib/*)
          ;;
        *)
          echo "STOP: FFmpeg library resolved outside private prefix: $dependency_path" >&2
          exit 1
          ;;
      esac

      resolved_dependency="$(readlink -f -- "$dependency_path" || true)"
      case "$resolved_dependency" in
        "$PREFIX"/lib/*)
          PRIVATE_FFMPEG_DEPENDENCY_COUNT=$((PRIVATE_FFMPEG_DEPENDENCY_COUNT + 1))
          ;;
        *)
          echo "STOP: FFmpeg library target escapes private prefix: $dependency_path -> $resolved_dependency" >&2
          exit 1
          ;;
      esac
    fi
  done <<<"$DEPENDENCIES"

  if [[ "$PRIVATE_FFMPEG_DEPENDENCY_COUNT" -eq 0 ]]; then
    echo "STOP: no private libav/libsw/libpostproc dependencies found for $binary." >&2
    exit 1
  fi

  echo "PRIVATE_FFMPEG_DEPENDENCIES=$PRIVATE_FFMPEG_DEPENDENCY_COUNT"

  echo "--- Restricted-environment executable smoke test"
  env -u LD_LIBRARY_PATH PATH=/usr/bin:/bin "$binary" -version
done

echo "PRIVATE_FFMPEG_ELF_DEPENDENCIES=PASS"
cp "$TARBALL" "$NOTICE_ROOT/source/ffmpeg-${VERSION}.tar.xz"
cp "$SIGNATURE" "$NOTICE_ROOT/source/ffmpeg-${VERSION}.tar.xz.asc"
cp "$KEY_FILE" "$NOTICE_ROOT/source/ffmpeg-devel.asc"
cp "$SOURCE_DIR/COPYING.LGPLv2.1" "$NOTICE_ROOT/COPYING.LGPLv2.1"

echo
echo "===== FFMPEG VERSION AND LICENSE EVIDENCE ====="
"$PREFIX/bin/ffmpeg" -version \
  | tee "$NOTICE_ROOT/FFMPEG-VERSION.txt"

"$PREFIX/bin/ffmpeg" -buildconf \
  | tee "$NOTICE_ROOT/FFMPEG-BUILD-CONFIGURATION.txt"

"$PREFIX/bin/ffmpeg" -L \
  | tee "$NOTICE_ROOT/FFMPEG-LICENSE-INFO.txt"

echo
echo "===== CREATE FFMPEG PACKAGE MANIFEST ====="
python3 - "$PREFIX" "$NOTICE_ROOT" "$VERSION" \
  "$SOURCE_SHA256" "$SIGNATURE_SHA256" "$ACTUAL_FINGERPRINT" <<'PY'
import hashlib
import json
import os
import pathlib
import sys

prefix = pathlib.Path(sys.argv[1])
notices = pathlib.Path(sys.argv[2])
version, source_sha, signature_sha, fingerprint = sys.argv[3:7]


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


runtime_files = []
runtime_symlinks = []
for path in sorted(prefix.rglob("*")):
    relative_path = path.relative_to(prefix).as_posix()

    if path.is_symlink():
        link_target = os.readlink(path)
        if not link_target or pathlib.PurePosixPath(link_target).is_absolute() or "\\" in link_target:
            raise SystemExit(
                f"FFmpeg runtime symlink target is not relocatable: "
                f"{relative_path} -> {link_target}"
            )
        try:
            resolved = path.resolve(strict=True)
            resolved_relative = resolved.relative_to(prefix).as_posix()
        except (OSError, RuntimeError, ValueError) as exc:
            raise SystemExit(
                f"unsafe FFmpeg runtime symlink {relative_path}: {exc}"
            ) from exc

        if not resolved.is_file():
            raise SystemExit(
                f"FFmpeg symlink does not resolve to a regular file: {relative_path}"
            )

        runtime_symlinks.append({
            "path": relative_path,
            "link_target": link_target,
            "resolved_path": resolved_relative,
            "resolved_size_bytes": resolved.stat().st_size,
            "resolved_sha256": digest(resolved),
        })
    elif path.is_file():
        runtime_files.append({
            "path": relative_path,
            "size_bytes": path.stat().st_size,
            "sha256": digest(path),
        })

notice_files = []
for path in sorted(notices.rglob("*")):
    if (
        path.is_file()
        and not path.is_symlink()
        and path.name != "FFMPEG-BUILD-MANIFEST.json"
    ):
        notice_files.append({
            "path": path.relative_to(notices).as_posix(),
            "size_bytes": path.stat().st_size,
            "sha256": digest(path),
        })

manifest = {
    "schema": "rdt-linux-ffmpeg-bundle-v1",
    "version": version,
    "platform": "linux",
    "architecture": os.uname().machine,
    "license": "LGPL-2.1-or-later",
    "gpl_enabled": False,
    "nonfree_enabled": False,
    "dynamic_linking": True,
    "source": {
        "url": f"https://ffmpeg.org/releases/ffmpeg-{version}.tar.xz",
        "signature_url": f"https://ffmpeg.org/releases/ffmpeg-{version}.tar.xz.asc",
        "sha256": source_sha,
        "signature_sha256": signature_sha,
        "signing_key_primary_fingerprint": fingerprint,
        "source_archive": f"source/ffmpeg-{version}.tar.xz",
    },
    "runtime_files": runtime_files,
    "runtime_symlinks": runtime_symlinks,
    "runtime_file_count": len(runtime_files),
    "runtime_symlink_count": len(runtime_symlinks),
    "notice_files": notice_files,
    "runtime_size_bytes": sum(item["size_bytes"] for item in runtime_files),
    "notice_size_bytes": sum(item["size_bytes"] for item in notice_files),
}

target = notices / "FFMPEG-BUILD-MANIFEST.json"
target.write_text(
    json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
print(f"FFMPEG_RUNTIME_FILE_COUNT={len(runtime_files)}")
print(f"FFMPEG_RUNTIME_SIZE_BYTES={manifest['runtime_size_bytes']}")
print(f"FFMPEG_NOTICE_SIZE_BYTES={manifest['notice_size_bytes']}")
print(f"FFMPEG_BUILD_MANIFEST={target}")
PY

echo
echo "===== UPDATE LINUX NOTICE INDEX ====="
python3 - "$NOTICE_ROOT" <<'PY'
import hashlib
import json
import pathlib
import sys

ffmpeg_notices = pathlib.Path(sys.argv[1])
notices_root = ffmpeg_notices.parent
build_manifest = json.loads(
    (ffmpeg_notices / "FFMPEG-BUILD-MANIFEST.json").read_text(encoding="utf-8")
)

build_notices = notices_root / "BUILD-NOTICES.md"
text = build_notices.read_text(encoding="utf-8")
heading = "## FFmpeg — Linux bundled runtime"

if heading not in text:
    text = text.rstrip() + (
        "\n\n## FFmpeg — Linux bundled runtime\n\n"
        "The Linux package carries FFmpeg and FFprobe built from the verified "
        "official FFmpeg source archive. The build uses shared libraries and "
        "disables GPL and nonfree components. The exact source archive, "
        "signature, signing-key fingerprint, LGPL text, build configuration "
        "and SHA-256 manifest are included under `ffmpeg/`. This disclosure "
        "records the build configuration and files; the final packaged binary "
        "must still pass native qualification and release legal review.\n"
    )
    build_notices.write_text(text, encoding="utf-8")

manifest_path = notices_root / "NOTICE-MANIFEST.json"
if not manifest_path.is_file() or manifest_path.is_symlink():
    raise SystemExit("Linux NOTICE-MANIFEST.json missing or unsafe")

data = json.loads(manifest_path.read_text(encoding="utf-8"))
if not isinstance(data, dict):
    raise SystemExit("Linux NOTICE-MANIFEST.json is not an object")

files = []
for path in sorted(notices_root.rglob("*")):
    if (
        not path.is_file()
        or path.is_symlink()
        or path.name in {
            "NOTICE-MANIFEST.json",
            "NOTICE-MANIFEST.json.tmp",
            "BUILD-NOTICES.md",
        }
    ):
        continue
    h = hashlib.sha256(path.read_bytes()).hexdigest()
    files.append({
        "path": path.relative_to(notices_root).as_posix(),
        "size_bytes": path.stat().st_size,
        "sha256": h,
    })

data["files"] = files
data["notice_file_count"] = len(files)
data["ffmpeg"] = {
    "manifest": "ffmpeg/FFMPEG-BUILD-MANIFEST.json",
    "manifest_sha256": hashlib.sha256(
        (ffmpeg_notices / "FFMPEG-BUILD-MANIFEST.json").read_bytes()
    ).hexdigest(),
    "version": build_manifest["version"],
    "architecture": build_manifest["architecture"],
    "license": build_manifest["license"],
    "gpl_enabled": build_manifest["gpl_enabled"],
    "nonfree_enabled": build_manifest["nonfree_enabled"],
    "runtime_size_bytes": build_manifest["runtime_size_bytes"],
    "source_size_bytes": (
        ffmpeg_notices / "source" /
        f"ffmpeg-{build_manifest['version']}.tar.xz"
    ).stat().st_size,
}

temporary = manifest_path.with_name("NOTICE-MANIFEST.json.tmp")
if temporary.exists():
    raise SystemExit("Refusing to overwrite existing NOTICE-MANIFEST.json.tmp")
temporary.write_text(
    json.dumps(data, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
temporary.replace(manifest_path)

print(f"LINUX_NOTICE_FILE_COUNT={len(files)}")
print("LINUX_FFMPEG_NOTICE_INDEX=PASS")
PY

echo
echo "PASS: verified FFmpeg ${VERSION} Linux runtime built and inventoried."
