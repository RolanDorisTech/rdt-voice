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
import struct
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


def resolve_app_bundle(product, marker, app_override=None, cargo_target_dir=None):
    """Resolve the marker-isolated app bundle; never default to shared src-tauri/target."""
    if app_override:
        return Path(app_override).expanduser().resolve()

    target_root = (
        Path(cargo_target_dir).expanduser().resolve()
        if cargo_target_dir
        else BUILD / f"rdt-connect-{marker}-cargo-target"
    )
    return target_root / "release" / "bundle" / "macos" / f"{product}.app"


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

    source_guide = ROOT / "assets" / "macos-dmg-template" / "Installation_Guide.html"
    if not source_guide.is_file():
        stop(f"approved repository Installation_Guide.html is missing: {source_guide}")
    TEMPLATE.parent.mkdir(parents=True, exist_ok=True)
    # Preserve the checked-in guide's macOS FinderInfo and ResourceFork.
    # Path.write_bytes() silently drops both extended attributes.
    def read_xattr_bytes(path, attr, label):
        result = subprocess.run(
            ["/usr/bin/xattr", "-px", attr, str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            stop(f"{label} is missing required metadata: {attr}")
        try:
            return bytes.fromhex(result.stdout)
        except ValueError:
            stop(f"could not decode {attr} metadata for {label}")

    copy_result = subprocess.run(
        [
            "/usr/bin/ditto", "--rsrc", "--extattr", "--acl",
            str(source_guide), str(TEMPLATE),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if copy_result.returncode != 0:
        stop(f"could not stage Installation_Guide.html with metadata: {copy_result.stderr.strip()}")

    for attr in ("com.apple.FinderInfo", "com.apple.ResourceFork"):
        source_value = read_xattr_bytes(source_guide, attr, "repository guide")
        staged_value = read_xattr_bytes(TEMPLATE, attr, "staged guide")
        if source_value != staged_value:
            stop(f"staged guide metadata differs from repository source: {attr}")
    print("PASS: staged Installation Guide preserves FinderInfo and ResourceFork exactly.")
    print(f"Staged approved Installation Guide: {TEMPLATE}")
    # Apply the permanent repository icon to the staged guide, never Downloads.
    guide_icon = ROOT / "assets" / "macos-dmg-template" / "rdt_connect_guide_icon_240.png"
    icon_helper = ROOT / "scripts" / "apply-macos-file-icon.swift"
    if not guide_icon.is_file():
        stop(f"permanent Installation Guide icon is missing: {guide_icon}")
    if not icon_helper.is_file():
        stop(f"native Finder icon helper is missing: {icon_helper}")

    icon_bytes = guide_icon.read_bytes()
    if len(icon_bytes) < 24 or icon_bytes[:8] != bytes.fromhex("89504e470d0a1a0a"):
        stop("Installation Guide icon is not a valid PNG.")
    icon_width = int.from_bytes(icon_bytes[16:20], "big")
    icon_height = int.from_bytes(icon_bytes[20:24], "big")
    if (icon_width, icon_height) != (240, 240):
        stop(f"expected the permanent 240x240 guide icon, got {icon_width}x{icon_height}.")

    run(["swift", str(icon_helper), str(guide_icon), str(TEMPLATE)])

    finder_info = read_xattr_bytes(TEMPLATE, "com.apple.FinderInfo", "iconized staged guide")
    if len(finder_info) != 32 or not (int.from_bytes(finder_info[8:10], "big") & 0x0400):
        stop("staged Installation Guide does not have Finder's custom-icon flag.")

    resource_fork = read_xattr_bytes(TEMPLATE, "com.apple.ResourceFork", "iconized staged guide")
    if len(resource_fork) < 28:
        stop("staged Installation Guide custom icon resource fork is missing.")
    data_offset, map_offset, data_length, map_length = struct.unpack(">IIII", resource_fork[:16])
    if data_offset + data_length > len(resource_fork) or map_offset + map_length > len(resource_fork):
        stop("staged Installation Guide custom icon resource fork has invalid bounds.")
    resource_map = resource_fork[map_offset:map_offset + map_length]
    type_base = int.from_bytes(resource_map[24:26], "big")
    if type_base + 2 > len(resource_map):
        stop("staged Installation Guide custom icon resource map is invalid.")
    type_count = int.from_bytes(resource_map[type_base:type_base + 2], "big") + 1
    icon_verified = False
    for index in range(type_count):
        entry = type_base + 2 + index * 8
        if entry + 8 > len(resource_map):
            stop("staged Installation Guide resource type list is truncated.")
        kind = resource_map[entry:entry + 4]
        count = int.from_bytes(resource_map[entry + 4:entry + 6], "big") + 1
        refs = type_base + int.from_bytes(resource_map[entry + 6:entry + 8], "big")
        if kind != b"icns":
            continue
        for item in range(count):
            ref = refs + item * 12
            if ref + 12 > len(resource_map):
                stop("staged Installation Guide ICNS reference is truncated.")
            resource_id = int.from_bytes(resource_map[ref:ref + 2], "big", signed=True)
            relative = int.from_bytes(resource_map[ref + 5:ref + 8], "big")
            position = data_offset + relative
            if position + 4 > len(resource_fork):
                stop("staged Installation Guide ICNS resource offset is invalid.")
            payload_size = int.from_bytes(resource_fork[position:position + 4], "big")
            payload = resource_fork[position + 4:position + 4 + payload_size]
            if resource_id == -16455 and payload[:4] == b"icns" and int.from_bytes(payload[4:8], "big") == len(payload):
                icon_verified = True
    if not icon_verified:
        stop("staged Installation Guide is missing Finder's valid custom ICNS resource.")
    print("PASS: permanent 240x240 PNG applied; Finder flag and ICNS resource verified.")

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
    if not re.fullmatch(r"1[A-Z]\d{2}", marker):
        stop(f"invalid build marker: {marker!r}")

    app_override = os.environ.get("RDT_DMG_APP")
    bundle = resolve_app_bundle(
        product,
        marker,
        app_override=app_override,
        cargo_target_dir=os.environ.get("RDT_CARGO_TARGET_DIR"),
    )
    if app_override and bundle.name != f"{product}.app":
        stop(f"RDT_DMG_APP must point to {product}.app: {bundle}")
    print(f"App bundle: {bundle}")
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
    # RDT Connect staged-app MLX normalization v1
    # ditto may materialize relative metallib symlinks as regular files.
    # Normalize only the disposable DMG staging copy, never the built app.
    mlx_tool = ROOT / "scripts" / "dedupe-macos-mlx-metallib.py"
    if not mlx_tool.is_file():
        stop(f"required MLX metallib normalizer is missing: {mlx_tool}")
    run([sys.executable, str(mlx_tool), str(staged_app)])
    run([sys.executable, str(mlx_tool), str(staged_app), "--verify-only"])

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

        # Verify the actual mounted distribution, not only the source app.
        mlx_check = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "dedupe-macos-mlx-metallib.py"),
                str(mounted_app),
                "--verify-only",
                "--max-app-mib",
                "590",
            ],
            check=False,
        )
        if mlx_check.returncode != 0:
            stop("mounted custom DMG failed the MLX metallib layout/size invariant.")
        print("PASS: mounted custom DMG contains one physical MLX metallib and two relative symlinks.")
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
            staged_value = read_xattr_bytes(TEMPLATE, attr, "iconized staged guide")
            mounted_value = read_xattr_bytes(mounted_guide, attr, "mounted DMG guide")
            if staged_value != mounted_value:
                stop(f"custom guide icon metadata differs after DMG packaging: {attr}")
        print("PASS: mounted guide FinderInfo and ResourceFork match iconized staged guide.")

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
