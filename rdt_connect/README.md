# RDT Connect

RDT Connect is an optional desktop companion for [RDT Voice](https://rolandoristech.github.io/rdt-voice/). Choose a Whisper model, manage your local server, and open RDT Voice in your browser. Connect is not required for browser-only use.

## RDT Connect Downloads

| Platform | Installer | Version | Build | File size |
| --- | --- | --- | --- | --- |
| macOS (Apple Silicon) | [**Download (.dmg)**](https://github.com/RolanDorisTech/rdt-voice/releases/download/connect-v0.2.0-dev.11-1d04/RDT_Connect_0.2.0-dev.11.1D04_aarch64.dmg) | `0.2.0-dev.11` | `1D04` | 235,712,825 bytes |
| Windows (ARM64) | [**Download (.exe)**](https://github.com/RolanDorisTech/rdt-voice/releases/download/connect-v0.2.0-dev.11-2a07/RDT.Connect_0.2.0-dev.11.2A07_arm64-setup.exe) | `0.2.0-dev.11.2A07` | `2A07` | 101979239 bytes |
| Windows (x64) | [**Download (.exe)**](https://github.com/RolanDorisTech/rdt-voice/releases/download/connect-v0.2.0-dev.11-3a03/RDT.Connect_0.2.0-dev.11.3A03_x64-setup.exe) | `0.2.0-dev.11.3A03` | `3A03` | 137365296 bytes |

**SHA-256 (Build 1D04):** `93b8442447e083e1e328904f06ec3ff3e8dd1f13df9fe28de1b6ae8e775ac39f`

**Platform notes**

- **macOS Apple Silicon:** The published DMG is ad-hoc signed and not notarized by Apple; macOS may show a first-launch security prompt.
- **Windows ARM64:** Build 2A07 is published. CI packaging and signing checks passed; independent physical-device installation and updater testing have not been completed.
- **Windows x64:** Build 3A03 is published. CI packaging checks passed. Manual UTM testing confirmed that RDT Connect opens, but the VM froze during HTTPS certificate setup, preventing further functional testing. In-app updater testing is planned in VMware.
- **Linux:** Qualification and release integration are handled separately. Linux downloads will be listed when the corresponding release assets are published and verified.

**Updater note:** The Windows Dev 11 updater feeds are published and point to Build 2A07 (ARM64) and Build 3A03 (x64). In-app updater installation and restart verification in VMware remains pending.

## Developer notes

- `src/` — desktop UI.
- `src-tauri/` — native app, permissions, and updater configuration.
- `scripts/build-macos-with-marker.sh` — macOS app and updater build.
- `scripts/build-custom-dmg.py` — custom installer packaging and verification.
- `scripts/test-build-versioning.cjs` — build-version checks.
- `updates/dev/` — development updater manifests. Platform-specific feed and signature compatibility is being verified separately; do not assume automatic updates are qualified on every platform.

Builds require macOS build tools, Node.js/npm, Rust/Cargo, and a compatible Python environment for the bundled backend. Signed release builds require the existing updater signing key through the protected local release setup.

Custom installer assets are local-only inputs at `~/rdt_connect/DMG_Build_Assets/`. Verify their checksums before packaging. Never commit or upload that asset folder, signing keys, tokens, or private configuration.
