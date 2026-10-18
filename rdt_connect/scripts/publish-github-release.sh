#!/usr/bin/env bash
set -euo pipefail

REPO="RolanDorisTech/rdt-voice"
ROOT="$(git rev-parse --show-toplevel)"
GH_BIN="$(command -v gh || true)"
GIT_BIN="$(command -v git || true)"

[[ -n "$GH_BIN" && -x "$GH_BIN" && -n "$GIT_BIN" && -x "$GIT_BIN" ]] || {
  echo "STOP: Git and GitHub CLI are required."
  exit 1
}
[[ "$("$GIT_BIN" -C "$ROOT" remote get-url origin)" == *"RolanDorisTech/rdt-voice"* ]] || {
  echo "STOP: unexpected Git remote."
  exit 1
}
[[ "$("$GIT_BIN" -C "$ROOT" branch --show-current)" == "main" ]] || {
  echo "STOP: run this from the main branch."
  exit 1
}
[[ "$#" == 1 && -f "$1" ]] || {
  echo "Usage: $0 /absolute/path/to/RDT_Connect_VERSION_aarch64.dmg"
  exit 2
}
"$GH_BIN" auth status >/dev/null

if [[ -n "$("$GIT_BIN" -C "$ROOT" status --porcelain)" ]]; then
  echo "STOP: working tree is not clean. No release was created."
  exit 1
fi

"$GIT_BIN" -C "$ROOT" fetch origin main
HEAD_SHA="$("$GIT_BIN" -C "$ROOT" rev-parse HEAD)"
REMOTE_SHA="$("$GIT_BIN" -C "$ROOT" rev-parse origin/main)"
[[ "$HEAD_SHA" == "$REMOTE_SHA" ]] || {
  echo "STOP: local main is not exactly origin/main. Update and recheck first."
  exit 1
}

VERSION="$(python3 -c 'import json; print(json.load(open("rdt_connect/package.json"))["version"])')"
LOCK_VERSION="$(python3 -c 'import json; print(json.load(open("rdt_connect/package-lock.json"))["version"])')"
[[ "$VERSION" == "$LOCK_VERSION" ]] || {
  echo "STOP: package.json and package-lock.json versions differ."
  exit 1
}
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$ ]] || {
  echo "STOP: version is not valid SemVer."
  exit 1
}

DMG="$(cd "$(dirname "$1")" && pwd -P)/$(basename "$1")"
EXPECTED_NAME="RDT_Connect_${VERSION}_aarch64.dmg"
[[ "$(basename "$DMG")" == "$EXPECTED_NAME" ]] || {
  echo "STOP: expected asset filename: $EXPECTED_NAME"
  exit 1
}

TAG="rdt-connect-v$VERSION"
if "$GIT_BIN" -C "$ROOT" ls-remote --tags origin "refs/tags/$TAG" | grep -q .; then
  echo "STOP: remote tag already exists: $TAG"
  exit 1
fi
if "$GH_BIN" api "repos/$REPO/releases/tags/$TAG" >/dev/null 2>&1; then
  echo "STOP: a release already exists for $TAG"
  exit 1
fi

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "STOP: run DMG integrity verification on macOS."
  exit 1
fi
hdiutil verify "$DMG"
LOCAL_HASH="$(shasum -a 256 "$DMG" | awk '{print $1}')"
LOCAL_SIZE="$(wc -c < "$DMG" | tr -d '[:space:]')"
BUILD_ID="$(tr -d '[:space:]' < rdt_connect/scripts/build-id-sequence.txt)"
TITLE="RDT Connect v$VERSION (Build $BUILD_ID)"

NOTES="$(cat <<NOTES
# $TITLE

**Platform:** macOS, Apple Silicon (arm64).

This is a development prerelease. Install manually from the attached DMG
and follow the installation guide included in the image.

**Signing:** This build is ad-hoc signed and not notarized by Apple, so macOS
may display a first-launch security warning.

**Updates:** Automatic updating is not advertised as verified. Use the DMG
for manual installation.

**SHA-256:** \`$LOCAL_HASH\`
NOTES
)"

echo "Creating a draft prerelease:"
echo "  Tag:    $TAG"
echo "  Target: $HEAD_SHA"
echo "  Asset:  $EXPECTED_NAME ($LOCAL_SIZE bytes)"
echo "  SHA256: $LOCAL_HASH"

"$GH_BIN" release create "$TAG" "$DMG" \
  --repo "$REPO" \
  --target "$HEAD_SHA" \
  --title "$TITLE" \
  --draft \
  --prerelease \
  --notes "$NOTES"

ASSET_JSON="$("$GH_BIN" api "repos/$REPO/releases/tags/$TAG" --jq \
  ".assets[] | select(.name == \"$EXPECTED_NAME\") | [.name,.size,.digest,.browser_download_url] | @tsv")"
[[ -n "$ASSET_JSON" ]] || {
  echo "STOP: release draft exists but expected uploaded asset could not be found."
  echo "Inspect the draft; do not recreate or retag it."
  exit 1
}
IFS=$'\t' read -r REMOTE_NAME REMOTE_SIZE REMOTE_DIGEST DOWNLOAD_URL <<< "$ASSET_JSON"
EXPECTED_DOWNLOAD_URL='https:'
EXPECTED_DOWNLOAD_URL+='/'
EXPECTED_DOWNLOAD_URL+='/github.com/'
EXPECTED_DOWNLOAD_URL+="$REPO"
EXPECTED_DOWNLOAD_URL+='/releases/download/'
EXPECTED_DOWNLOAD_URL+="$TAG"
EXPECTED_DOWNLOAD_URL+='/'
EXPECTED_DOWNLOAD_URL+="$EXPECTED_NAME"
[[ "$REMOTE_NAME" == "$EXPECTED_NAME" &&
   "$REMOTE_SIZE" == "$LOCAL_SIZE" &&
   "$REMOTE_DIGEST" == "sha256:$LOCAL_HASH" &&
   "$DOWNLOAD_URL" == "$EXPECTED_DOWNLOAD_URL" ]] || {
  echo "STOP: uploaded asset verification failed."
  echo "Draft retained for inspection. Local SHA256: $LOCAL_HASH"
  echo "GitHub asset: $ASSET_JSON"
  exit 1
}

echo
echo "UPLOAD VERIFIED:"
echo "$ASSET_JSON"
printf 'Publish this verified prerelease now? [y/N] '
read -r REPLY
case "$REPLY" in
  y|Y|yes|YES|Yes) ;;
  *)
    echo "Draft retained; it has not been published."
    exit 0
    ;;
esac

"$GH_BIN" release edit "$TAG" --repo "$REPO" --draft=false --prerelease
echo "Published prerelease: $DOWNLOAD_URL"
