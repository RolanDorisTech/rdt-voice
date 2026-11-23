#!/usr/bin/env python3
"""Validate the effective Linux Tauri updater config after JSON Merge Patch."""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[2]


def merge_patch(target, patch):
    if not isinstance(patch, dict):
        return patch
    result = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = merge_patch(result.get(key), value)
    return result


def validate_linux(base, overlay, workflow):
    effective = merge_patch(base, overlay)
    bundle = effective.get("bundle", {})
    updater = effective.get("plugins", {}).get("updater", {})

    assert bundle.get("createUpdaterArtifacts") is True, \
        "Linux updater artifacts must be enabled in the effective config"
    targets = bundle.get("targets", [])
    assert "deb" in targets and "appimage" in targets, \
        "Linux Debian and AppImage bundle targets are required"

    depends = bundle.get("linux", {}).get("deb", {}).get("depends", [])
    assert "libvulkan1" in depends, "Debian package must declare libvulkan1"
    assert "libayatana-appindicator3-1" in depends, \
        "Debian package must declare the AppIndicator runtime"

    assert updater.get("requireSignedVersion") is True, \
        "Signed updater versions must be required"
    assert updater.get("dangerousInsecureTransportProtocol") is not True, \
        "Insecure updater transport is forbidden"

    pubkey = updater.get("pubkey")
    assert isinstance(pubkey, str) and pubkey.strip(), \
        "Effective updater public key is missing"
    decoded = base64.b64decode(pubkey, validate=True).decode("utf-8")
    lines = decoded.splitlines()
    assert len(lines) >= 2 and lines[0].startswith("untrusted comment: minisign public key:"), \
        "Updater key is not a valid minisign public key envelope"
    assert len(lines[1]) >= 40, "Updater public-key payload is unexpectedly short"

    endpoints = updater.get("endpoints", [])
    assert isinstance(endpoints, list) and endpoints, \
        "Linux updater HTTPS endpoint is missing"
    for endpoint in endpoints:
        parsed = urlsplit(endpoint)
        assert parsed.scheme == "https" and parsed.hostname == "raw.githubusercontent.com", \
            "Updater endpoint must use the approved HTTPS raw.githubusercontent.com host"
        assert parsed.port in (None, 443), "Updater endpoint must use standard HTTPS port"
        assert parsed.username is None and parsed.password is None, \
            "Updater endpoint must not contain embedded credentials"
        assert not parsed.query and not parsed.fragment, \
            "Updater endpoint must not contain query or fragment data"
        assert parsed.path.startswith("/RolanDorisTech/rdt-voice/"), \
            "Updater endpoint points outside the official RDT repository"
        assert "/rdt_connect/updates/dev/" in parsed.path and parsed.path.endswith("/latest.json"), \
            "Updater endpoint must target the RDT development update feed"
        assert "{{target}}" in parsed.path and "{{arch}}" in parsed.path, \
            "Updater endpoint must retain Tauri target and architecture placeholders"

    assert "--bundles deb,appimage" in workflow, \
        "Linux CI must build both Debian and AppImage targets"
    workflow_lower = workflow.lower()
    assert 'base64 --decode "$appimage.sig"' in workflow_lower, "Linux AppImage signature must be decoded for verification"
    assert 'minisign -v -m "$appimage"' in workflow_lower, "Linux AppImage signature must be cryptographically verified"
    assert "tauri-updater.pub" in workflow_lower, "Linux AppImage signature must use the configured updater public key"
    assert "rdt_whisper_worker_dotprod" in workflow, \
        "Linux ARM64 dot-product worker must remain release-gated"

    return effective


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=("linux",), required=True)
    args = parser.parse_args()
    base = json.loads((ROOT / "rdt_connect/src-tauri/tauri.conf.json").read_text())
    overlay = json.loads((ROOT / "rdt_connect/src-tauri/tauri.linux.conf.json").read_text())
    workflow = (ROOT / ".github/workflows/linux-native-qualification.yml").read_text()
    effective = validate_linux(base, overlay, workflow)
    updater = effective["plugins"]["updater"]
    print("PASS: effective Tauri JSON Merge Patch config enables Linux updater artifacts")
    print("PASS: inherited minisign public key and signed-version policy verified")
    print("PASS: Linux updater endpoint is HTTPS and repository-pinned")
    print("PASS: Debian dependencies and AppImage/DEB targets verified")
    print("PASS: AppImage signature and ARM64 dotprod build contracts retained")


if __name__ == "__main__":
    main()
