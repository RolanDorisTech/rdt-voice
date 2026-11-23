#!/usr/bin/env python3
"""Build source/run/marker-bound manifests for RDT Connect release assets."""
import argparse
import base64
import hashlib
import json
import re
from pathlib import Path

MARKERS = {
    ("windows", "ARM64"): re.compile(r"2[A-KM-Z][0-9]{2}"),
    ("windows", "x64"): re.compile(r"3[A-KM-Z][0-9]{2}"),
    ("linux", "ARM64"): re.compile(r"4L[0-9]{3}"),
    ("linux", "x64"): re.compile(r"5L[0-9]{3}"),
}
BASE_VERSION = re.compile(
    r"^[0-9]+\.[0-9]+\.[0-9]+-(?:[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)$"
)
AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".opus"}

def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()

def collect_evidence(items, manifest_path):
    result = {}
    for raw_item in items:
        if raw_item.is_symlink():
            raise ValueError("evidence path must not be a symlink: " + str(raw_item))
        if not raw_item.exists() or not (raw_item.is_dir() or raw_item.is_file()):
            raise ValueError("evidence path missing or invalid: " + str(raw_item))
        item = raw_item.resolve()
        candidates = sorted(item.rglob("*")) if item.is_dir() else [item]
        for path in candidates:
            if path == manifest_path.resolve() or path.is_symlink() or not path.is_file():
                continue
            if path.suffix.lower() in AUDIO_SUFFIXES:
                continue
            if any(part.lower() in {"models", "model-cache", "model_weights"} for part in path.parts):
                continue
            key = path.relative_to(item).as_posix() if item.is_dir() else path.name
            if key in result:
                raise ValueError("duplicate evidence path: " + key)
            result[key] = {
                "size_bytes": path.stat().st_size,
                "sha256": digest(path),
            }
    return dict(sorted(result.items()))

def build_manifest(args):
    marker_pattern = MARKERS.get((args.platform, args.architecture))
    if marker_pattern is None or not marker_pattern.fullmatch(args.marker):
        raise ValueError("platform/architecture/build marker mismatch")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", args.source_sha):
        raise ValueError("source SHA must be a 40-character commit")
    if args.repository != "RolanDorisTech/rdt-voice":
        raise ValueError("unexpected repository")
    for value, label in ((args.run_id, "run ID"), (args.run_attempt, "run attempt")):
        if not value.isdigit() or int(value) < 1:
            raise ValueError("invalid " + label)

    config = json.loads(args.tauri_config.read_text(encoding="utf-8"))
    version = config.get("version")
    if not isinstance(version, str) or "." not in version:
        raise ValueError("Tauri version missing or invalid")
    base_version, stamped_marker = version.rsplit(".", 1)
    if not BASE_VERSION.fullmatch(base_version) or stamped_marker != args.marker:
        raise ValueError("Tauri version does not match requested build marker")

    encoded_key = config.get("plugins", {}).get("updater", {}).get("pubkey", "")
    try:
        public_key = base64.b64decode(encoded_key, validate=True)
    except Exception as exc:
        raise ValueError("configured updater public key is invalid base64") from exc
    if not public_key:
        raise ValueError("configured updater public key is empty")

    assets = {}
    for raw_path in args.asset:
        path = Path(raw_path).resolve()
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError("missing or empty asset: " + str(path))
        if path.name in assets:
            raise ValueError("duplicate asset basename: " + path.name)
        assets[path.name] = path
    if not assets:
        raise ValueError("no release assets supplied")

    for name, path in assets.items():
        if name.endswith(".sha256"):
            target_name = name[:-7]
            target = assets.get(target_name)
            if target is None:
                raise ValueError("checksum has no matching asset: " + name)
            fields = path.read_text(encoding="ascii").split()
            if not fields or not re.fullmatch(r"[0-9a-fA-F]{64}", fields[0]):
                raise ValueError("malformed checksum sidecar: " + name)
            if digest(target).lower() != fields[0].lower():
                raise ValueError("SHA-256 mismatch for " + target_name)
        if name.endswith(".sig"):
            encoded = path.read_text(encoding="ascii").strip()
            if "\n" in encoded or "\r" in encoded:
                raise ValueError("signature must be a single line: " + name)
            try:
                signature = base64.b64decode(encoded, validate=True)
            except Exception as exc:
                raise ValueError("invalid base64 signature: " + name) from exc
            if not signature:
                raise ValueError("empty signature: " + name)

    names = set(assets)
    if args.platform == "windows":
        installers = [name for name in names if name.endswith("-setup.exe")]
        if len(installers) != 1:
            raise ValueError("expected exactly one NSIS installer")
        installer = installers[0]
        required = {
            installer + ".sha256", installer + ".sig", installer + ".sig.sha256",
            "RDT-Connect-Windows-" + args.architecture + "-DISTRIBUTION.zip",
            "RDT-Connect-Windows-" + args.architecture + "-DISTRIBUTION.zip.sha256",
            "RDT-Connect-Windows-Installation-Guide.zip",
            "RDT-Connect-Windows-Installation-Guide.zip.sha256",
        }
    else:
        appimages = [name for name in names if name.endswith(".AppImage")]
        debs = [name for name in names if name.endswith(".deb")]
        if len(appimages) != 1 or len(debs) != 1:
            raise ValueError("Linux requires exactly one AppImage and one .deb")
        appimage = appimages[0]
        required = {
            appimage + ".sig", appimage + ".sha256", appimage + ".sig.sha256",
        }

    missing = required - names
    if missing:
        raise ValueError("required release assets missing: " + ", ".join(sorted(missing)))

    if not args.artifact_name or not args.qualification_artifact_name:
        raise ValueError("artifact and qualification artifact names are required")

    manifest_path = args.output.resolve()
    evidence = collect_evidence(args.evidence, manifest_path)
    asset_records = [
        {"name": name, "size_bytes": path.stat().st_size, "sha256": digest(path)}
        for name, path in sorted(assets.items())
    ]
    return {
        "schema_version": 1,
        "product": "RDT Connect",
        "repository": args.repository,
        "source": {"sha": args.source_sha.lower(), "ref": args.source_ref},
        "workflow": {
            "name": args.workflow_name,
            "run_id": int(args.run_id),
            "run_attempt": int(args.run_attempt),
        },
        "target": {
            "platform": args.platform,
            "architecture": args.architecture,
            "version": version,
            "base_version": base_version,
            "build_marker": args.marker,
        },
        "updater_public_key_sha256": hashlib.sha256(public_key).hexdigest(),
        "release_artifact_names": sorted(set(args.artifact_name)),
        "qualification_artifact_names": sorted(set(args.qualification_artifact_name)),
        "assets": asset_records,
        "evidence": evidence,
    }

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, choices=["windows", "linux"])
    parser.add_argument("--architecture", required=True, choices=["x64", "ARM64"])
    parser.add_argument("--marker", required=True)
    parser.add_argument("--tauri-config", required=True, type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--workflow-name", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--artifact-name", action="append", default=[])
    parser.add_argument("--qualification-artifact-name", action="append", default=[])
    parser.add_argument("--asset", action="append", default=[])
    parser.add_argument("--evidence", action="append", default=[], type=Path)
    args = parser.parse_args()

    manifest = build_manifest(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print("PASS: release manifest " + str(args.output))
    print("PASS: " + args.platform + "/" + args.architecture + " " +
          manifest["target"]["version"] + " assets=" + str(len(manifest["assets"])) +
          " evidence=" + str(len(manifest["evidence"])))

if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit("STOP: release manifest: " + str(exc))
