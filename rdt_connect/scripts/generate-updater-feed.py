#!/usr/bin/env python3
"""Generate or safely advance one architecture-specific Tauri updater feed."""
import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

TARGET_PREFIXES = {
    "darwin-aarch64": "1",
    "windows-aarch64": "2",
    "windows-x86_64": "3",
    "linux-aarch64": "4",
    "linux-x86_64": "5",
}
MARKER_RE = re.compile(r"^[1-5][A-Z][0-9]{2}$")
VERSION_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)"
    r"\.([1-5][A-Z][0-9]{2})$"
)
IDENT_RE = re.compile(r"^[0-9A-Za-z-]+$")


def parse_version(version, target):
    if not isinstance(version, str):
        raise ValueError("version must be text")
    match = VERSION_RE.fullmatch(version)
    if not match:
        raise ValueError("version must be semver plus a final platform build marker")
    major, minor, patch = map(int, match.group(1, 2, 3))
    prerelease, marker = match.group(4), match.group(5)
    expected = TARGET_PREFIXES.get(target)
    if expected is None:
        raise ValueError(f"unsupported target: {target}")
    if not MARKER_RE.fullmatch(marker) or marker[0] != expected:
        raise ValueError(f"build marker {marker} is invalid for {target}")
    if prerelease is None:
        prerelease_key = (1,)
    else:
        identifiers = prerelease.split(".")
        if any(not IDENT_RE.fullmatch(part) for part in identifiers):
            raise ValueError("invalid semver prerelease identifier")
        if any(len(part) > 1 and part.startswith("0") and part.isdigit()
               for part in identifiers):
            raise ValueError("numeric prerelease identifiers cannot have leading zeroes")
        prerelease_key = (0, *(
            (0, int(part)) if part.isdigit() else (1, part)
            for part in identifiers
        ))
    marker_key = (ord(marker[1]), int(marker[2:]))
    return (major, minor, patch, prerelease_key, marker_key)


def validate_entry(entry, target):
    if not isinstance(entry, dict) or set(entry) != {"url", "signature"}:
        raise ValueError("platform entry must contain exactly url and signature")

    url = entry["url"]
    if not isinstance(url, str) or not url or url != url.strip():
        raise ValueError("artifact URL must be nonempty text")
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("artifact URL is malformed") from exc

    if (parsed.scheme != "https" or not parsed.hostname
            or (port is not None and not 1 <= port <= 65535)
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment
            or any(ch.isspace() for ch in url)
            or parsed.path in ("", "/")):
        raise ValueError(
            "artifact URL must be HTTPS with a path and no credentials, query, or fragment"
        )

    signature = entry["signature"]
    if (not isinstance(signature, str) or not signature.strip() or "\n" in signature or "\r" in signature):
        raise ValueError("signature must be nonempty single-line text")
    return entry


def validate_manifest(data, target):
    expected_keys = {"version", "notes", "pub_date", "platforms"}
    if not isinstance(data, dict) or set(data) != expected_keys:
        raise ValueError("manifest must contain version, notes, pub_date, and platforms")
    parse_version(data["version"], target)
    if not isinstance(data["notes"], str):
        raise ValueError("manifest notes must be text")
    pub_date = data["pub_date"]
    if not isinstance(pub_date, str):
        raise ValueError("pub_date must be text")
    try:
        datetime.strptime(pub_date, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ValueError("pub_date must use YYYY-MM-DDTHH:MM:SSZ") from exc
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", pub_date):
        raise ValueError("pub_date must use YYYY-MM-DDTHH:MM:SSZ")
    platforms = data["platforms"]
    if not isinstance(platforms, dict) or list(platforms) != [target]:
        raise ValueError("manifest must contain only the requested target")
    validate_entry(platforms[target], target)
    return data


def read_existing(path, target):
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("platforms"), dict):
        raise ValueError("existing feed has invalid manifest structure")
    platforms = data["platforms"]
    if list(platforms) != [target]:
        raise ValueError("existing feed must contain only the requested target")
    validate_entry(platforms[target], target)
    return validate_manifest(data, target)


def build_manifest(version, target, url, signature, notes):
    return {
        "version": version,
        "notes": notes,
        "pub_date": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "platforms": {
            target: {"url": url, "signature": signature}
        },
    }


def write_atomic(path, manifest, target):
    path.parent.mkdir(parents=True, exist_ok=True)
    had_original = path.exists()
    original_bytes = path.read_bytes() if had_original else None
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(manifest, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

        parsed = validate_manifest(
            json.loads(temp_path.read_text(encoding="utf-8")), target
        )
        if parsed != manifest:
            raise ValueError("temporary manifest failed validation")

        temp_path.replace(path)
        try:
            final = validate_manifest(
                json.loads(path.read_text(encoding="utf-8")), target
            )
            if final != manifest:
                raise ValueError("final manifest failed validation")
        except Exception:
            if had_original:
                restore_fd, restore_name = tempfile.mkstemp(
                    prefix=f".{path.name}.", suffix=".restore", dir=path.parent
                )
                restore_path = Path(restore_name)
                try:
                    with os.fdopen(restore_fd, "wb") as stream:
                        stream.write(original_bytes)
                        stream.flush()
                        os.fsync(stream.fileno())
                    restore_path.replace(path)
                finally:
                    try:
                        restore_path.unlink()
                    except FileNotFoundError:
                        pass
            else:
                path.unlink()
            raise
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--target", required=True, choices=sorted(TARGET_PREFIXES))
    parser.add_argument("--url", required=True)
    parser.add_argument("--signature", required=True)
    parser.add_argument("--notes", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--update", action="store_true")
    args = parser.parse_args()

    try:
        new_key = parse_version(args.version, args.target)
        validate_entry({"url": args.url, "signature": args.signature}, args.target)
        output = args.output.resolve()

        if output.exists():
            if not args.update:
                raise ValueError("output exists; pass --update to request a validated update")
            existing = read_existing(output, args.target)
            old_key = parse_version(existing["version"], args.target)
            if new_key <= old_key:
                raise ValueError(
                    f"refusing same-version or downgrade: {existing['version']} -> {args.version}"
                )

        manifest = build_manifest(
            args.version, args.target, args.url, args.signature, args.notes
        )
        validate_manifest(manifest, args.target)
        write_atomic(output, manifest, args.target)
        print(f"PASS: wrote {output} for {args.target} {args.version}")
        return 0
    except (ValueError, OSError, json.JSONDecodeError, TypeError, KeyError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
