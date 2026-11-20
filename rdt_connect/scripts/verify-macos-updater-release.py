#!/usr/bin/env python3
"""Fail-closed verification of the configured RDT Connect macOS release."""

import base64
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from macos_release_identity import derive_from_project


ROOT = Path(__file__).resolve().parents[2]
CONNECT = ROOT / "rdt_connect"
KEY = Path.home() / ".tauri/rdt-connect-updater.key.pub"
FEEDS = (
    CONNECT / "updates/dev/latest.json",
    CONNECT / "updates/dev/darwin/aarch64/latest.json",
)


def require(condition, message):
    if not condition:
        raise SystemExit("FAIL: " + message)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run_visible(command, cwd):
    print("+", " ".join(map(str, command)), flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def main():
    try:
        identity = derive_from_project(CONNECT)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"FAIL: cannot derive release identity: {exc}") from exc

    stage = CONNECT / identity.stage_directory
    archive = stage / identity.archive_name
    signature = stage / identity.signature_name
    dmg = stage / identity.dmg_name

    print(f"Release version: {identity.version}")
    print(f"Build marker: {identity.marker}")
    print(f"Release tag: {identity.tag}")
    print(f"Stage directory: {stage}")

    for path in (archive, signature, dmg, KEY, *FEEDS):
        require(path.is_file() and path.stat().st_size > 0, f"missing or empty file: {path}")

    print("Running updater feed generator regression tests...", flush=True)
    run_visible(
        [sys.executable, str(CONNECT / "scripts/test-generate-updater-feed.py")],
        ROOT,
    )

    print("Running macOS release identity regression tests...", flush=True)
    run_visible(
        [sys.executable, str(CONNECT / "scripts/test_macos_release_identity.py")],
        ROOT,
    )

    print("Running Rust updater policy tests...", flush=True)
    run_visible(
        ["cargo", "test", "--manifest-path", str(CONNECT / "src-tauri/Cargo.toml")],
        ROOT,
    )

    config = json.loads(
        (CONNECT / "src-tauri/tauri.conf.json").read_text(encoding="utf-8")
    )
    configured_key = (
        config.get("plugins", {}).get("updater", {}).get("pubkey", "").strip()
    )
    encoded_key = KEY.read_text(encoding="utf-8").strip()
    encoded_signature = signature.read_text(encoding="utf-8").strip()

    require(bool(configured_key), "Tauri updater public key missing")
    require(encoded_key == configured_key, "public key file differs from Tauri config")

    try:
        decoded_key = base64.b64decode(encoded_key, validate=True)
        decoded_signature = base64.b64decode(encoded_signature, validate=True)
    except Exception as exc:
        raise SystemExit(f"FAIL: Base64 decoding failed: {exc}") from exc

    require(
        decoded_key.startswith(b"untrusted comment:"),
        "decoded public key has an unexpected minisign header",
    )
    require(
        decoded_signature.startswith(b"untrusted comment:"),
        "decoded signature has an unexpected minisign header",
    )

    parsed = []
    for path in FEEDS:
        data = json.loads(path.read_text(encoding="utf-8"))
        require(data.get("version") == identity.version, f"wrong version in {path}")
        platform = data.get("platforms", {}).get("darwin-aarch64")
        require(isinstance(platform, dict), f"missing darwin-aarch64 in {path}")
        require(
            platform.get("url") == identity.archive_url,
            f"unexpected archive URL in {path}",
        )
        require(
            platform.get("signature") == encoded_signature,
            f"feed signature differs from staged signature in {path}",
        )
        parsed.append(platform)

    require(
        parsed[0]["signature"] == parsed[1]["signature"],
        "feed signatures differ",
    )

    minisign = shutil.which("minisign")
    require(minisign, "minisign executable not found")
    with tempfile.NamedTemporaryFile(prefix="rdt-minisign-pub-") as pub:
        with tempfile.NamedTemporaryFile(prefix="rdt-minisign-sig-") as sig:
            pub.write(decoded_key)
            pub.flush()
            sig.write(decoded_signature)
            sig.flush()
            result = subprocess.run(
                [
                    minisign, "-V", "-m", str(archive),
                    "-x", sig.name, "-p", pub.name,
                ],
                capture_output=True,
                text=True,
            )
    require(
        result.returncode == 0,
        "minisign verification failed: "
        + (result.stderr.strip() or result.stdout.strip()),
    )

    listing = subprocess.run(
        ["tar", "-tzf", str(archive)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    require(listing.returncode == 0, "updater archive listing failed: " + listing.stderr.strip())
    members = listing.stdout.splitlines()
    require(bool(members), "updater archive is empty")
    require(
        not any("librispeech" in item.lower() for item in members),
        "LibriSpeech content found in updater archive",
    )
    print(f"PASS: updater archive readable ({len(members)} entries); no LibriSpeech content")
    run_visible(["hdiutil", "verify", str(dmg)], ROOT)

    print("PASS: app version and marker resolve consistently")
    print("PASS: both feeds match derived version, URL, and detached signature")
    print("PASS: public key matches Tauri configuration")
    print("PASS: archive signature verified by minisign")
    print("PASS: updater archive is readable")
    print("PASS: DMG integrity verified")
    for path in (archive, signature, dmg):
        print(f"{path.name}: {path.stat().st_size} bytes  SHA256 {sha256(path)}")
    print(f"READY: {identity.version} release assets and feeds pass prepublication gate")


if __name__ == "__main__":
    main()
