#!/usr/bin/env python3
"""Verify downloaded RDT Connect artifacts against their qualified-run manifest."""
import argparse
import base64
import hashlib
import json
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path

AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".opus"}
MARKER_PATTERNS = {
    ("windows", "x64"): re.compile(r"3[A-KM-Z][0-9]{2}"),
    ("windows", "ARM64"): re.compile(r"2[A-KM-Z][0-9]{2}"),
    ("linux", "x64"): re.compile(r"5L[0-9]{3}"),
    ("linux", "ARM64"): re.compile(r"4L[0-9]{3}"),
}

def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()

def inventory(root, excluded):
    result = {}
    for path in sorted(root.rglob("*")):
        if path == excluded or path.is_symlink() or not path.is_file():
            continue
        if path.suffix.lower() in AUDIO_SUFFIXES:
            continue
        if any(part.lower() in {"models", "model-cache", "model_weights"} for part in path.parts):
            continue
        key = path.relative_to(root).as_posix()
        result[key] = {"size_bytes": path.stat().st_size, "sha256": digest(path)}
    return result

def verify(args):
    manifest_path = args.manifest.resolve()
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ValueError("unsupported release manifest schema")
    if data.get("repository") != args.repository:
        raise ValueError("manifest repository mismatch")
    if data.get("source", {}).get("sha", "").lower() != args.source_sha.lower():
        raise ValueError("manifest source commit mismatch")
    if data.get("source", {}).get("ref") != args.source_ref:
        raise ValueError("manifest source ref mismatch")

    workflow = data.get("workflow", {})
    if workflow.get("name") != args.workflow_name:
        raise ValueError("manifest workflow identity mismatch")
    if workflow.get("run_id") != int(args.run_id):
        raise ValueError("manifest run ID mismatch")
    if workflow.get("run_attempt") != int(args.run_attempt):
        raise ValueError("manifest run attempt mismatch")

    target = data.get("target", {})
    if target.get("platform") != args.platform or target.get("architecture") != args.architecture:
        raise ValueError("manifest target mismatch")
    marker_pattern = MARKER_PATTERNS.get((args.platform, args.architecture))
    if marker_pattern is None or target.get("build_marker") != args.marker or not marker_pattern.fullmatch(args.marker):
        raise ValueError("manifest build marker/platform mismatch")
    if not target.get("base_version") or target.get("version") != target["base_version"] + "." + args.marker:
        raise ValueError("manifest version does not match marker")

    config = json.loads(args.tauri_config.read_text(encoding="utf-8"))
    configured_version = config.get("version", "")
    version_match = re.fullmatch(
        r"(.+)\.((?:[1-3][A-KM-Z][0-9]{2}|[45]L[0-9]{3}))",
        configured_version,
    )
    if not version_match or version_match.group(1) != target["base_version"]:
        raise ValueError("manifest base version differs from checked-out source")

    encoded_key = config.get("plugins", {}).get("updater", {}).get("pubkey", "")
    try:
        public_key = base64.b64decode(encoded_key, validate=True)
    except Exception as exc:
        raise ValueError("configured updater public key is invalid") from exc
    key_hash = hashlib.sha256(public_key).hexdigest()
    if data.get("updater_public_key_sha256") != key_hash:
        raise ValueError("manifest updater public key fingerprint mismatch")

    attempt = str(args.run_attempt)
    run_id = str(args.run_id)
    if args.platform == "windows":
        arch_name = args.architecture
        lower = "x64" if arch_name == "x64" else "arm64"
        expected_release = sorted([
            "RDT-Connect-Windows-" + arch_name + "-DISTRIBUTION-" + attempt,
            "RDT-Connect-Windows-" + arch_name + "-INSTALLATION-GUIDE-" + attempt,
            "RDT-Connect-Windows-" + arch_name + "-TEST-INSTALLER-" + attempt,
            "rdt-connect-windows-" + lower + "-release-manifest-" + run_id + "-" + attempt,
        ])
        expected_qualification = [
            "rdt-windows-" + lower + "-installed-qualification-" + attempt
        ]
    else:
        name = "rdt-connect-linux-" + args.architecture + "-qualification-" + run_id + "-" + attempt
        expected_release = [name]
        expected_qualification = [name]
    if data.get("release_artifact_names") != expected_release:
        raise ValueError("manifest release artifact names do not match this run attempt")
    if data.get("qualification_artifact_names") != expected_qualification:
        raise ValueError("manifest qualification artifact names do not match this run attempt")

    found = {}
    for root in args.asset_root:
        for path in root.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            if path.name in found:
                raise ValueError("duplicate downloaded release asset: " + path.name)
            found[path.name] = path

    records = data.get("assets", [])
    expected_names = {record["name"] for record in records}
    if len(expected_names) != len(records):
        raise ValueError("manifest contains duplicate asset names")
    if set(found) != expected_names:
        raise ValueError(
            "downloaded assets differ from manifest; missing=" +
            ",".join(sorted(expected_names - set(found))) +
            " extra=" + ",".join(sorted(set(found) - expected_names))
        )

    for record in records:
        name = record["name"]
        path = found[name]
        if path.stat().st_size != record["size_bytes"] or digest(path) != record["sha256"]:
            raise ValueError("release asset hash/size mismatch: " + name)
        if name.endswith(".sha256"):
            target_name = name[:-7]
            if target_name not in found:
                raise ValueError("checksum sidecar target missing: " + name)
            fields = path.read_text(encoding="ascii").split()
            if not fields or fields[0].lower() != digest(found[target_name]).lower():
                raise ValueError("checksum sidecar verification failed: " + name)

    evidence = data.get("evidence", {})
    if not isinstance(evidence, dict) or not evidence:
        raise ValueError("manifest contains no qualification evidence")
    actual_evidence = {}
    for root in args.evidence_root:
        if root.is_symlink() or not root.is_dir():
            raise ValueError("qualification evidence directory missing: " + str(root))
        current = inventory(root, manifest_path)
        overlap = set(actual_evidence) & set(current)
        if overlap:
            raise ValueError("duplicate evidence paths: " + ",".join(sorted(overlap)))
        actual_evidence.update(current)
    if actual_evidence != evidence:
        missing = sorted(set(evidence) - set(actual_evidence))
        extra = sorted(set(actual_evidence) - set(evidence))
        raise ValueError("qualification evidence inventory mismatch; missing=" +
                         ",".join(missing[:8]) + " extra=" + ",".join(extra[:8]))

    if args.platform == "windows":
        distribution = [
            path for path in found.values()
            if path.name == "RDT-Connect-Windows-" + args.architecture + "-DISTRIBUTION.zip"
        ]
        installers = [path for path in found.values() if path.name.endswith("-setup.exe")]
        if len(distribution) != 1 or len(installers) != 1:
            raise ValueError("Windows distribution or installer is ambiguous")
        required_members = []
        installer = installers[0]
        for suffix in ("", ".sha256", ".sig", ".sig.sha256"):
            required_members.append(installer.name + suffix)
        with zipfile.ZipFile(distribution[0]) as archive:
            if archive.testzip() is not None:
                raise ValueError("Windows distribution ZIP CRC validation failed")
            members = {Path(name).name: name for name in archive.namelist() if not name.endswith("/")}
            for name in required_members:
                if name not in members:
                    raise ValueError("distribution ZIP missing signed installer member: " + name)
                if archive.read(members[name]) != found[name].read_bytes():
                    raise ValueError("distribution ZIP content differs from qualified asset: " + name)

    minisign = args.minisign
    signed_payloads = [
        path for path in found.values()
        if path.name.endswith(".exe") or path.name.endswith(".AppImage")
    ]
    if not signed_payloads:
        raise ValueError("no updater payload found for signature verification")
    with tempfile.TemporaryDirectory(prefix="rdt-release-verify-") as directory:
        key_path = Path(directory) / "updater.pub"
        key_path.write_bytes(public_key)
        for payload in signed_payloads:
            sig_path = found.get(payload.name + ".sig")
            if sig_path is None:
                raise ValueError("updater signature missing for " + payload.name)
            decoded = base64.b64decode(sig_path.read_text(encoding="ascii").strip(), validate=True)
            signature_path = Path(directory) / (payload.name + ".minisig")
            signature_path.write_bytes(decoded)
            subprocess.run(
                [minisign, "-Vm", str(payload), "-x", str(signature_path), "-p", str(key_path)],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            print("PASS: updater signature " + payload.name)

    tag = "rdt-connect-v" + target["base_version"] + "-" + args.marker.lower()
    print("PASS: " + args.platform + "/" + args.architecture + " " + target["version"])
    print("PASS: " + str(len(records)) + " release asset hashes; " +
          str(len(evidence)) + " evidence hashes")
    print("RELEASE_TAG=" + tag)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--asset-root", action="append", required=True, type=Path)
    parser.add_argument("--evidence-root", action="append", required=True, type=Path)
    parser.add_argument("--platform", required=True, choices=["windows", "linux"])
    parser.add_argument("--architecture", required=True, choices=["x64", "ARM64"])
    parser.add_argument("--marker", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--workflow-name", required=True)
    parser.add_argument("--tauri-config", required=True, type=Path)
    parser.add_argument("--minisign", default="minisign")
    verify(parser.parse_args())

if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        raise SystemExit("STOP: artifact verification: " + str(exc))
