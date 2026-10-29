# RDT Connect

RDT Connect is an optional desktop companion for [RDT Voice](https://rolandoristech.github.io/rdt-voice/). Choose a Whisper model, manage your local server, and open RDT Voice in your browser. Connect is not required for browser-only use.

## RDT Connect Downloads

| Platform | Installer | Version | Build | File size |
| --- | --- | --- | --- | --- |
| macOS (Apple Silicon) | [**Download (.dmg)**](https://github.com/RolanDorisTech/rdt-voice/releases/download/rdt-connect-v0.2.0-dev.11-1d04/RDT_Connect_0.2.0-dev.11.1D04_aarch64.dmg) | `0.2.0-dev.11.1D04` | `1D04` | 235,712,825 bytes |
| Windows (ARM64) | [**Download (.exe)**](https://github.com/RolanDorisTech/rdt-voice/releases/download/rdt-connect-v0.2.0-dev.11-2a05/RDT.Connect_0.2.0-dev.11.2A05_arm64-setup.exe) | `0.2.0-dev.11.2A05` | `2A05` | 101,982,643 bytes |
| Windows (x64) | [**Download (.exe)**](https://github.com/RolanDorisTech/rdt-voice/releases/download/rdt-connect-v0.2.0-dev.11-3a01/RDT.Connect_0.2.0-dev.11.3A01_x64-setup.exe) | `0.2.0-dev.11.3A01` | `3A01` | 137,375,653 bytes |

**SHA-256 (Build 1D04):** `93b8442447e083e1e328904f06ec3ff3e8dd1f13df9fe28de1b6ae8e775ac39f`

**Platform notes**

- **macOS Apple Silicon:** The published DMG is ad-hoc signed and not notarized by Apple; macOS may show a first-launch security prompt.
- **Windows ARM64:** The installer workflow passed CI. This does not claim an independent physical-device transcription test.
- **Windows x64:** CI verified installation, installed-app architecture and build metadata, packaged resources, and a short startup smoke test. A full microphone/transcription test is not claimed.
- **Linux:** Qualification and release integration are handled separately. Linux downloads will be listed when the corresponding release assets are published and verified.

**Updater note:** Platform-specific development updater feeds and signatures are being reconciled separately. Use direct downloads rather than assuming cross-platform automatic updates are fully qualified.

## Developer notes

- `src/` — desktop UI.
- `src-tauri/` — native app, permissions, and updater configuration.
- `scripts/build-macos-with-marker.sh` — macOS app and updater build.
- `scripts/build-custom-dmg.py` — custom installer packaging and verification.
- `scripts/test-build-versioning.cjs` — build-version checks.
- `updates/dev/` — development updater manifests. Platform-specific feed and signature compatibility is being verified separately; do not assume automatic updates are qualified on every platform.

Builds require macOS build tools, Node.js/npm, Rust/Cargo, and a compatible Python environment for the bundled backend. Signed release builds require the existing updater signing key through the protected local release setup.

Custom installer assets are local-only inputs at `~/rdt_connect/DMG_Build_Assets/`. Verify their checksums before packaging. Never commit or upload that asset folder, signing keys, tokens, or private configuration.
