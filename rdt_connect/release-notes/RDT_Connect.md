# RDT Connect — Cumulative Development Release Notes

Current build: **0D53**
Version: `0.2.0-dev.10.0D.53`
Platform: macOS on Apple Silicon (arm64); macOS 14 or later.

These notes combine the relevant development changes from Builds 0D52 and 0D53.

## Build 0D52

- Refreshed the signed macOS Apple Silicon updater.
- Added the distinct About build identifier.
- In-app updating from Build 0D50 to Build 0D52 was verified on macOS Apple Silicon.

## Build 0D53

- Restored the dedicated native macOS About panel instead of redirecting About into the in-app settings section.
- Removed the duplicated version string by using numeric macOS bundle build metadata.
- Added the branded About content: **Build 0D53**, “Local AI Made Easy”, © 2026 Rolan & Doris Tech, and separate RDT Voice GitHub and RolanDorisTech YouTube links.
- Rebuilt the custom DMG using the established Finder presentation, custom background, icon placement, and installation guide.
- Generated the updater archive and matching signature using the existing updater keypair; the archive signature was cryptographically verified against the configured public key.

## Installation and updating

- The custom DMG is available for manual installation on macOS Apple Silicon.
- The app is ad-hoc-signed and not notarized by Apple, so macOS may show a first-launch security warning.
- The development update feed now targets the signed 0D53 updater archive.
- Existing installations should receive the update through the configured development updater once this manifest change is merged.

## Build 0D53 artifact integrity

- Custom installer: `RDT_Connect_0.2.0-dev.10.0D.53_aarch64.dmg`
- DMG size: 377195289 bytes
- DMG SHA-256: `91ed7d7f28ebaea98a2e3f35b95be2618ff7b00bf606c521052975fedced533d`
- Updater archive: `RDT.Connect.app.tar.gz`
- Updater signature: `RDT.Connect.app.tar.gz.sig`

See the [RDT Connect GitHub releases](https://github.com/RolanDorisTech/rdt-voice/releases) for downloadable artifacts.
