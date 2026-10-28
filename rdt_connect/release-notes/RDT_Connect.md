# RDT Connect — Cumulative Development Release Notes

Current build: **1D04**
Version: `0.2.0-dev.11.1D04`
Platform: macOS Apple Silicon (arm64); macOS 14 or later.

The Dev 10 notes below are retained as historical records. Dev 11 macOS updater verification is recorded first.

## Dev 11 — macOS Apple Silicon, Build 1D04

- Corrected the updater archive/signature pairing for the 1D04 release.
- Verified the published `RDT.Connect.app.tar.gz` SHA-256: `bdffd7ef52f33bf3695e99a045d02829696a58a31de7935db9fd711b5f5330d3`.
- Cryptographically verified the decoded release signature against the configured updater public key.
- User-confirmed in-app update from installed Build 1D03 to 1D04 completed successfully; the app displayed the installed-update/reopen prompt.
- The existing 1D04 tag and published release assets remain unchanged.

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
