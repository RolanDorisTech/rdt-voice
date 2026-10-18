# Product Versions, Tags, Downloads, and Releases

RDT Voice and RDT Connect are related products in one repository, but they
are independent release streams. A release of one product does not require
a version bump or release of the other.

## Canonical tag names

Use lowercase product-prefixed tags with a full Semantic Version:

- RDT Voice: `rdt-voice-vMAJOR.MINOR.PATCH`
- RDT Connect: `rdt-connect-vMAJOR.MINOR.PATCH`

Prereleases append a SemVer prerelease identifier, for example:

- `rdt-voice-v0.2.0-dev.10`
- `rdt-connect-v0.2.0-dev.10`
- `rdt-connect-v0.2.0-rc.1`

Advance each product's version independently. Internal build identifiers
such as `0D42` belong in the release title and notes, not in the tag.

Never reuse or move an already-published version tag. Preserve historical
legacy tags and asset URLs; use the canonical convention for new releases.

## RDT Connect macOS asset convention

For Apple Silicon, use:

`RDT_Connect_VERSION_aarch64.dmg`

Replace `VERSION` with the exact version from `rdt_connect/package.json`.
Keep the same version in the tag, release title, app metadata, and DMG name.
Additional platforms must use distinct, explicit asset names.

## Product-aware downloads

Do not use GitHub's repository-wide `releases/latest/download/...` shortcut
for product-specific downloads. The repository contains two release streams.

The official RDT Voice site must resolve Connect downloads by selecting a
published release with the `rdt-connect-v` tag prefix and a valid, uploaded
asset matching the expected Connect filename. Validate the asset's name,
size, and SHA-256 digest. Skip empty releases and never redirect to release
notes as though they were a file download.

The permanent Connect download entry point belongs to the official website.
Its resolver—not a versioned asset URL hard-coded into the README—must
discover the eligible release. Development and stable download policies
must be explicit rather than determined accidentally by another product's
release date.

## Release checklist

1. Build and test the app from the source revision to be released.
2. Confirm `package.json`, `package-lock.json`, app metadata, and the DMG
   agree on the version.
3. Confirm the product-prefixed tag does not already exist.
4. Run the guarded publishing script from a clean, current `main` checkout.
5. Verify the uploaded asset name, byte size, SHA-256 digest, and direct URL.
6. Publish only after verification; retain the release's product-specific tag.
7. Select the previous release for the same product when generating notes.
8. Do not advertise automatic updating unless signed updater artifacts and
   the product-specific feed have passed end-to-end testing.

The release script creates a draft first, verifies the uploaded asset against
the local file, and asks before publishing the prerelease.
