#!/usr/bin/env python3
"""Package an already-built RDT Connect .app into the approved custom DMG."""

from pathlib import Path
from datetime import datetime
import argparse
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
ASSETS = Path.home() / "rdt_connect" / "DMG_Build_Assets"
TEMPLATE = BUILD / "custom-dmg-template" / "Installation_Guide.html"
CONFIG = ROOT / "src-tauri" / "tauri.conf.json"
MARKER_FILE = ROOT / "scripts" / "build-id-sequence.txt"


def stop(message):
    raise SystemExit(f"STOP: {message}")


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def run(command):
    print("+", " ".join(map(str, command)))
    subprocess.run(list(map(str, command)), check=True)


def load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        stop(f"cannot read valid JSON from {path}: {exc}")


def verify_canonical_versions(root, expected):
    package = load_json(root / "package.json")
    tauri = load_json(root / "src-tauri" / "tauri.conf.json")
    cargo_text = (root / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8")
    lock_text = (root / "src-tauri" / "Cargo.lock").read_text(encoding="utf-8")

    section = re.search(r"(?ms)^\[package\]\s*(.*?)(?=^\[|\Z)", cargo_text)
    cargo_match = re.search(r'(?m)^version\s*=\s*"([^"]+)"\s*$', section.group(1)) if section else None
    lock_match = re.search(
        r'(?ms)^\[\[package\]\]\s*\nname = "rdt-connect-desktop"\s*\nversion = "([^"]+)"',
        lock_text,
    )
    versions = {
        "package.json": package.get("version"),
        "tauri.conf.json": tauri.get("version"),
        "Cargo.toml": cargo_match.group(1) if cargo_match else None,
        "Cargo.lock": lock_match.group(1) if lock_match else None,
    }
    if any(value != expected for value in versions.values()):
        stop(f"canonical version mismatch; expected {expected!r}, found {versions}")


def find_dmgbuild():
    override = os.environ.get("DMGBUILD_BIN")
    if override:
        p = Path(override).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return p
        stop(f"DMGBUILD_BIN is not executable: {p}")

    found = shutil.which("dmgbuild")
    if found:
        return Path(found)

    candidates = sorted(BUILD.glob("dmg-custom-*/dmgbuild-venv/bin/dmgbuild"))
    candidates = [p for p in candidates if p.is_file() and os.access(p, os.X_OK)]
    if len(candidates) != 1:
        stop(
            "could not select exactly one dmgbuild executable. "
            "Set DMGBUILD_BIN to the approved executable."
        )
    return candidates[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="validate inputs and print the intended output; create no DMG"
    )
    args = parser.parse_args()

    config = load_json(CONFIG)
    manifest = load_json(ASSETS / "assets-manifest.json")
    policy = load_json(ASSETS / "distribution-policy.json")
    layout = load_json(ASSETS / "layout.json")

    classification = "LOCAL_ONLY_BUILD_INPUTS_NEVER_PUBLISH_SEPARATELY"
    if manifest.get("classification") != classification:
        stop("asset manifest classification differs from the approved policy.")
    if policy.get("classification") != classification:
        stop("distribution policy classification differs from the approved policy.")
    manifest_upload = manifest.get("upload_policy", {})
    repository_policy = policy.get("repository_policy", {})
    if manifest_upload.get("upload_asset_library_to_github") is not False:
        stop("asset manifest does not explicitly prohibit uploading the asset library.")
    if repository_policy.get("upload_asset_library_to_github") is not False:
        stop("distribution policy does not explicitly prohibit uploading the asset library.")
    if repository_policy.get("commit_loose_assets") is not False:
        stop("distribution policy does not explicitly prohibit committing loose assets.")

    verified_assets = 0
    for item in manifest.get("files", []):
        p = ASSETS / item["path"]
        if not p.is_file():
            if item.get("required", True):
                stop(f"required asset missing: {p}")
            continue
        if sha256(p) != item.get("sha256"):
            stop(f"approved asset hash mismatch: {p}")
        verified_assets += 1

    source_guide = ASSETS / "Guide" / "Installation_Guide.html"
    guide_assets = ASSETS / "Guide" / ".rdt-connect-install-guide-assets"
    background = ASSETS / layout["background"]

    if not TEMPLATE.is_file():
        stop(f"iconized guide template missing: {TEMPLATE}")
    if sha256(TEMPLATE) != sha256(source_guide):
        stop("iconized guide HTML differs from approved source; refresh the icon template.")
    for p in (guide_assets, background):
        if not p.exists():
            stop(f"required installer input missing: {p}")

    expected_locations = {
        "RDT Connect.app": [484, 140],
        "Applications": [176, 134],
        "Installation_Guide.html": [330, 250],
    }
    expected_visible = [
        "RDT Connect.app", "Applications", "Installation_Guide.html"
    ]
    if layout.get("filesystem") != "APFS" or layout.get("format") != "UDZO":
        stop("layout filesystem/format no longer matches approved APFS/UDZO.")
    if layout.get("window", {}).get("width") != 660 or \
       layout.get("window", {}).get("height") != 400:
        stop("approved Finder window is no longer 660 × 400.")
    if layout.get("icon_size") != 96 or layout.get("text_size") != 12:
        stop("approved Finder icon/text sizes have changed.")
    if layout.get("icon_locations") != expected_locations:
        stop("approved Finder icon positions have changed.")
    if layout.get("visible_root_items") != expected_visible:
        stop("approved visible root items have changed.")

    version = config.get("version", "")
    product = config.get("productName", "")
    identifier = config.get("identifier", "")
    if not re.fullmatch(
        r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
        r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
        r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?",
        version,
    ) or not product or not identifier:
        stop(f"invalid canonical SemVer or app metadata: {version!r}")
    verify_canonical_versions(ROOT, version)

    marker = MARKER_FILE.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[01][A-Z]\d{2}", marker):
        stop(f"invalid build marker: {marker!r}")

    bundle = ROOT / "src-tauri" / "target/release/bundle/macos" / f"{product}.app"
    info_path = bundle / "Contents" / "Info.plist"
    executable = bundle / "Contents" / "MacOS" / "rdt-connect-desktop"
    if not bundle.is_dir() or not info_path.is_file() or not executable.is_file():
        stop(f"built application is missing or incomplete: {bundle}")

    with info_path.open("rb") as f:
        info = plistlib.load(f)
    if info.get("CFBundleIdentifier") != identifier:
        stop("built app bundle identifier differs from tauri.conf.json.")
    schemes = [
        s for entry in info.get("CFBundleURLTypes", [])
        for s in entry.get("CFBundleURLSchemes", [])
    ]
    if "rdtconnect" not in schemes:
        stop("built app bundle does not register the rdtconnect URL scheme.")

    # Refuse to package an old .app left in target/ when the source marker has
    # advanced. BUILD_ID is embedded in the compiled Rust application response.
    try:
        executable_bytes = executable.read_bytes()
    except OSError as exc:
        stop(f"could not read the built RDT Connect executable: {exc}")
    if marker.encode("ascii") not in executable_bytes:
        stop(
            f"built app does not contain active build marker {marker}; "
            "rebuild the app with scripts/build-macos-with-marker.sh before packaging."
        )

    dmgbuild = find_dmgbuild()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = BUILD / f"dmg-custom-{marker}-{stamp}"
    if output.exists():
        stop(f"output folder already exists; refusing to overwrite: {output}")
    dmg = output / f"RDT_Connect_{version}_aarch64.dmg"

    print(f"Validated local asset hashes: {verified_assets}")
    print(f"Internal version: {version}")
    print(f"Canonical SemVer: {version}")
    print(f"Active build marker: {marker}")
    print(f"Registered URL scheme: rdtconnect")
    print(f"dmgbuild: {dmgbuild}")
    print(f"Output: {dmg}")

    if args.dry_run:
        print("PASS: dry-run validation complete; no DMG or output directory created.")
        return

    staging = output / "staging"
    mount = output / "mount"
    output.mkdir(parents=True)
    staging.mkdir()
    mount.mkdir()
    staged_app = staging / f"{product}.app"
    staged_guide = staging / "Installation_Guide.html"
    staged_assets = staging / ".rdt-connect-install-guide-assets"

    run(["/usr/bin/ditto", "--rsrc", "--extattr", "--acl", bundle, staged_app])
    run(["/usr/bin/ditto", "--rsrc", "--extattr", "--acl", TEMPLATE, staged_guide])
    run(["/usr/bin/ditto", "--rsrc", "--extattr", "--acl", guide_assets, staged_assets])

    def pystr(value):
        return repr(str(value))

    settings = f'''application = {pystr(staged_app)}
guide = {pystr(staged_guide)}
assets = {pystr(staged_assets)}
background_image = {pystr(background)}

files = [application, guide, assets]
symlinks = {{"Applications": "/Applications"}}
hide = [".rdt-connect-install-guide-assets"]

format = "UDZO"
filesystem = "APFS"
background = background_image
window_rect = ((100, 100), (660, 400))
default_view = "icon-view"
include_icon_view_settings = True
arrange_by = None
show_status_bar = False
show_tab_view = False
show_toolbar = False
show_pathbar = False
show_sidebar = False
show_icon_preview = False
icon_size = 96
text_size = 12
label_pos = "bottom"

icon_locations = {{
    "RDT Connect.app": (484, 140),
    "Applications": (176, 134),
    "Installation_Guide.html": (330, 250),
}}
'''
    settings_path = output / "dmgbuild-settings.py"
    settings_path.write_text(settings, encoding="utf-8")

    run([dmgbuild, "-s", settings_path, f"RDT Connect {version}", dmg])
    run(["hdiutil", "verify", dmg])

    mounted = False
    try:
        run(["hdiutil", "attach", "-readonly", "-nobrowse",
             "-mountpoint", mount, dmg])
        mounted = True

        mounted_app = mount / f"{product}.app"
        mounted_guide = mount / "Installation_Guide.html"
        mounted_assets = mount / ".rdt-connect-install-guide-assets"
        mounted_exec = mounted_app / "Contents" / "MacOS" / "rdt-connect-desktop"

        for p in (
            mounted_app, mounted_guide, mounted_assets,
            mounted_assets / "Open Anyway Step 1.png",
            mounted_assets / "Open Anyway Step 2.png",
            mounted_assets / "Open Anyway Step 3.png",
            mount / ".DS_Store", mount / ".background.tiff",
        ):
            if not p.exists():
                stop(f"expected custom installer content missing: {p}")
        if not (mount / "Applications").is_symlink():
            stop("Applications item is not a symbolic link.")
        if sha256(executable) != sha256(mounted_exec):
            stop("mounted app executable hash differs from the built app.")

        with (mounted_app / "Contents" / "Info.plist").open("rb") as f:
            mounted_info = plistlib.load(f)
        mounted_schemes = [
            s for entry in mounted_info.get("CFBundleURLTypes", [])
            for s in entry.get("CFBundleURLSchemes", [])
        ]
        if "rdtconnect" not in mounted_schemes:
            stop("the mounted app does not register rdtconnect.")

        for attr in ("com.apple.FinderInfo", "com.apple.ResourceFork"):
            result = subprocess.run(
                ["/usr/bin/xattr", "-p", attr, str(mounted_guide)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if result.returncode != 0:
                stop(f"custom guide icon metadata missing after packaging: {attr}")

        print("PASS: disk image verified, app hash matches, URL scheme present,")
        print("      Applications link, Finder background, guide and icon metadata checked.")
    finally:
        if mounted:
            subprocess.run(["hdiutil", "detach", str(mount), "-quiet"], check=True)

    digest = sha256(dmg)
    report = output / "VERIFICATION.txt"
    report.write_text(
        f"RDT Connect custom DMG verification\n"
        f"Build marker: {marker}\n"
        f"Internal version: {version}\n"
        f"Canonical SemVer: {version}\n"
        f"DMG filename: {dmg.name}\n"
        f"DMG SHA-256: {digest}\n"
        f"DMG bytes: {dmg.stat().st_size}\n"
        f"Built app executable SHA-256: {sha256(executable)}\n"
        f"URL scheme: rdtconnect\n"
        f"Approved input asset hashes checked: {verified_assets}\n"
        f"Disk image verification: PASS\n"
        f"Mounted installer content checks: PASS\n"
        f"Publication status: NOT PUBLISHED\n",
        encoding="utf-8",
    )
    print(f"PASS: custom DMG verified locally: {dmg}")
    print(f"SHA-256: {digest}")
    print(f"Verification report: {report}")
    print("NOT PUBLISHED. This script never deploys or uploads artifacts.")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        stop(f"command failed with exit code {exc.returncode}: {exc.cmd}")
