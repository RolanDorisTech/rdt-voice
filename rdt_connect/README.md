# RDT Connect

Optional macOS Apple Silicon companion for [RDT Voice](https://rolandoristech.github.io/rdt-voice/). Choose a Whisper model, manage your local server, and open RDT Voice in your browser. Connect is not required for browser-only use.

## Current release

**Build 0D52** · [Release notes and assets](https://github.com/RolanDorisTech/rdt-voice/releases/tag/connect-v0.2-dev.10-0d52)

[**Download the custom macOS DMG**](https://github.com/RolanDorisTech/rdt-voice/releases/download/connect-v0.2-dev.10-0d52/RDT_Connect_0.2.0-dev.10_0D52_Custom_aarch64.dmg)

**SHA-256:** `73c3fbb3c67a47017baa93f5a31911bc86dc3b4004778839f93f408634550d7f`

The custom installer includes the approved Finder background, Applications shortcut, and installation guide.

**Platform:** macOS on Apple Silicon. This is a development prerelease, ad-hoc signed and not notarized; macOS may display a first-launch security warning. A Windows installer is not yet available.

## In-app updates

In Connect, select **Check for Updates**. Updating from Build 0D50 to 0D52 was successfully tested on Apple Silicon. The development feed supports signed macOS Apple Silicon updates.

## Developer notes

- `src/` — desktop UI.
- `src-tauri/` — native app, permissions, and updater configuration.
- `scripts/build-macos-with-marker.sh` — macOS app and updater build.
- `scripts/build-custom-dmg.py` — custom installer packaging and verification.
- `scripts/test-build-versioning.cjs` — build-version checks.
- `updates/dev/latest.json` — development updater manifest.

Builds require macOS build tools, Node.js/npm, Rust/Cargo, and a compatible Python environment for the bundled backend. Signed release builds require the existing updater signing key through the protected local release setup.

Custom installer assets are local-only inputs at `~/rdt_connect/DMG_Build_Assets/`. Verify their checksums before packaging. Never commit or upload that asset folder, signing keys, tokens, or private configuration.
