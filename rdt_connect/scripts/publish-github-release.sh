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

EXISTING_DRAFT_IDS="$("$GH_BIN" api --paginate "repos/$REPO/releases?per_page=100" --jq \
  ".[] | select(.draft == true and .tag_name == \"$TAG\") | .id")"
if [[ -n "$EXISTING_DRAFT_IDS" ]]; then
  echo "STOP: a draft already exists for $TAG (release IDs: $EXISTING_DRAFT_IDS)."
  echo "Inspect that draft instead of creating another."
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

RELEASE_CREATE_OUTPUT="$("$GH_BIN" release create "$TAG" "$DMG" \
  --repo "$REPO" \
  --target "$HEAD_SHA" \
  --title "$TITLE" \
  --draft \
  --prerelease \
  --notes "$NOTES")"
printf '%s\n' "$RELEASE_CREATE_OUTPUT"

# Draft release lookups by tag may return 404. Resolve the draft by its ID instead.
DRAFT_IDS="$("$GH_BIN" api --paginate "repos/$REPO/releases?per_page=100" --jq \
  ".[] | select(.draft == true and .tag_name == \"$TAG\") | .id")"
[[ -n "$DRAFT_IDS" && "$DRAFT_IDS" != *$'\n'* ]] || {
  echo "STOP: expected exactly one draft for $TAG; found: $DRAFT_IDS"
  echo "Inspect GitHub drafts before retrying."
  exit 1
}
RELEASE_ID="$DRAFT_IDS"

DRAFT_META="$("$GH_BIN" api "repos/$REPO/releases/$RELEASE_ID" --jq \
  '[.tag_name, (.draft|tostring), (.prerelease|tostring), .target_commitish] | @tsv')"
IFS=$'\t' read -r DRAFT_TAG DRAFT_FLAG DRAFT_PRERELEASE DRAFT_TARGET <<< "$DRAFT_META"
[[ "$DRAFT_TAG" == "$TAG" &&
   "$DRAFT_FLAG" == "true" &&
   "$DRAFT_PRERELEASE" == "true" &&
   "$DRAFT_TARGET" == "$HEAD_SHA" ]] || {
  echo "STOP: draft metadata does not match the intended tag and commit."
  echo "$DRAFT_META"
  exit 1
}

ASSET_JSON="$("$GH_BIN" api "repos/$REPO/releases/$RELEASE_ID" --jq \
  ".assets[] | select(.name == \"$EXPECTED_NAME\") | [.name,.size,.digest,.browser_download_url] | @tsv")"
[[ -n "$ASSET_JSON" && "$ASSET_JSON" != *$'\n'* ]] || {
  echo "STOP: expected exactly one matching uploaded asset in draft $RELEASE_ID."
  exit 1
}
IFS=$'\t' read -r REMOTE_NAME REMOTE_SIZE REMOTE_DIGEST DOWNLOAD_URL <<< "$ASSET_JSON"

case "$DOWNLOAD_URL" in
  "https://github.com/$REPO/releases/download/"*"/$EXPECTED_NAME") ;;
  *)
    echo "STOP: draft asset URL has unexpected origin or filename."
    echo "$DOWNLOAD_URL"
    exit 1
    ;;
esac

[[ "$REMOTE_NAME" == "$EXPECTED_NAME" &&
   "$REMOTE_SIZE" == "$LOCAL_SIZE" &&
   "$REMOTE_DIGEST" == "sha256:$LOCAL_HASH" ]] || {
  echo "STOP: uploaded asset verification failed."
  echo "Draft retained for inspection. Local SHA256: $LOCAL_HASH"
  echo "GitHub asset: $ASSET_JSON"
  exit 1
}

echo
echo "UPLOAD VERIFIED:"
echo "Release ID: $RELEASE_ID"
echo "Tag: $TAG"
echo "Asset: $REMOTE_NAME ($REMOTE_SIZE bytes)"
echo "SHA256: $REMOTE_DIGEST"
echo "Draft URL: $DOWNLOAD_URL"
printf 'Publish this verified prerelease? [y/N] '
read -r REPLY
case "$REPLY" in
  y|Y|yes|YES|Yes) ;;
  *)
    echo "Draft retained; it has not been published."
    exit 0
    ;;
esac

"$GH_BIN" api --method PATCH "repos/$REPO/releases/$RELEASE_ID" \
  -F draft=false -F prerelease=true >/dev/null

FINAL_META="$("$GH_BIN" api "repos/$REPO/releases/$RELEASE_ID" --jq \
  '[.tag_name, (.draft|tostring), (.prerelease|tostring)] | @tsv')"
IFS=$'\t' read -r FINAL_TAG FINAL_DRAFT FINAL_PRERELEASE <<< "$FINAL_META"
[[ "$FINAL_TAG" == "$TAG" &&
   "$FINAL_DRAFT" == "false" &&
   "$FINAL_PRERELEASE" == "true" ]] || {
  echo "STOP: published release metadata verification failed."
  echo "$FINAL_META"
  exit 1
}

FINAL_ASSET_JSON="$("$GH_BIN" api "repos/$REPO/releases/$RELEASE_ID" --jq \
  ".assets[] | select(.name == \"$EXPECTED_NAME\") | [.name,.size,.digest,.browser_download_url] | @tsv")"
[[ -n "$FINAL_ASSET_JSON" && "$FINAL_ASSET_JSON" != *$'\n'* ]] || {
  echo "STOP: published asset missing or ambiguous."
  exit 1
}
IFS=$'\t' read -r FINAL_NAME FINAL_SIZE FINAL_DIGEST FINAL_URL <<< "$FINAL_ASSET_JSON"

EXPECTED_DOWNLOAD_URL='https:'
EXPECTED_DOWNLOAD_URL+='/'
EXPECTED_DOWNLOAD_URL+='/github.com/'
EXPECTED_DOWNLOAD_URL+="$REPO"
EXPECTED_DOWNLOAD_URL+='/releases/download/'
EXPECTED_DOWNLOAD_URL+="$TAG"
EXPECTED_DOWNLOAD_URL+='/'
EXPECTED_DOWNLOAD_URL+="$EXPECTED_NAME"

[[ "$FINAL_NAME" == "$EXPECTED_NAME" &&
   "$FINAL_SIZE" == "$LOCAL_SIZE" &&
   "$FINAL_DIGEST" == "sha256:$LOCAL_HASH" &&
   "$FINAL_URL" == "$EXPECTED_DOWNLOAD_URL" ]] || {
  echo "STOP: published asset verification failed."
  echo "$FINAL_ASSET_JSON"
  exit 1
}

echo "PASS: published release and canonical download URL verified."
echo "Published prerelease: $FINAL_URL"
