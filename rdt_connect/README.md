# RDT Connect — Desktop Companion for RDT Voice

RDT Connect is the native desktop companion for RDT Voice. It packages the browser interface and local transcription backend in a macOS application, with controls for starting and stopping the server, model selection, secure local access, and private remote-device pairing.

RDT Connect and the browser-based RDT Voice application live together in this repository but serve different purposes. The browser app can continue to run independently.

## Current Release

- **Development version:** `0.2.0-dev.9`
- **Current packaged platform:** macOS on Apple Silicon (`arm64`)
- **Distribution:** GitHub Releases
- **Status:** Development prerelease, not a stable release.

The local macOS package passed the project's local package-verification checks. A successful local verification does not, by itself, prove a clean-machine installation or a complete updater installation and relaunch.

## macOS Signing and First Launch

This zero-cost distribution uses ad-hoc signing rather than an Apple Developer ID certificate. The app is **not Apple-notarized**.

macOS may display a security warning or prevent the first launch. Review the source and downloaded artifact before deciding whether to open it. Follow Apple's supported, per-app security instructions when appropriate. Do not disable Gatekeeper globally.

The Tauri updater uses a separate signing mechanism. Update functionality depends on publishing the matching signed archive and development manifest and verifying the complete update flow. Do not assume the updater is fully operational solely because local artifacts exist.

## Features

- Start and stop the packaged transcription backend.
- Select supported Whisper models.
- Open the packaged RDT Voice interface.
- Configure trusted local HTTPS where supported.
- Configure private remote access through Tailscale.
- Pair, approve, and revoke browser sessions using the authenticated backend.

Keep backend authentication enabled. Do not expose the backend directly to the public internet or enable Tailscale Funnel for private access.

## Download

Visit the [RDT Voice GitHub Releases page](https://github.com/RolanDorisTech/rdt-voice/releases) for available downloads.

Use a release specifically identified for Apple Silicon Macs. Windows support is a separate future task and requires a Windows-compatible transcription backend as well as a Windows build.

## Project Layout

- `src/` — TypeScript interface, styling, and native localization catalogs.
- `src-tauri/` — Rust application, Tauri configuration, capabilities, and icons.
- `scripts/` — desktop resource preparation and build tools.
- `localization/` — localization documentation, inventory, and validation tools.
- `updates/` — updater manifests for configured release channels.

Generated build output, packaged backend resources, and temporary backups are excluded from normal source publication.

## Developer Build

A macOS development build requires an Apple Silicon Mac, Node.js/npm, Rust/Cargo, the macOS build tools, and the dedicated Python environment used to package the MLX backend.

From this directory, install the frontend dependencies with `npm ci`. Configure `RDT_BUILD_PYTHON` to point to the Python environment containing the required backend and packaging dependencies, then run the project's macOS build script:

```bash
npm run build:desktop:macos
```

Generating signed updater artifacts additionally requires the correct Tauri updater signing key and passphrase. Keep the private key outside the repository, build resources, and release assets. Only the public key belongs in the application configuration.

## Important Limitations

- The current packaged backend targets macOS on Apple Silicon.
- Windows requires a compatible backend implementation and its own build and runtime tests.
- Ad-hoc signing is not equivalent to Developer ID signing or Apple notarization.
- Local package verification does not replace real installation, security-warning, remote-access, and end-to-end updater tests.

See the root [RDT Voice README](../README.md) for the browser application's instructions and backend documentation.
