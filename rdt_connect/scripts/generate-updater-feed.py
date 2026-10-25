#!/usr/bin/env python3
"""Generate a validated Tauri updater manifest for one RDT Connect target."""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

TARGET_PREFIXES = {
    "darwin-aarch64": {"1"},
    "windows-aarch64": {"2"},
    "windows-x86_64": {"3"},
    "linux-aarch64": {"4"},
    "linux-x86_64": {"5"},
}

MARKER_RE = re.compile(r"^[1-5][A-Z][0-9]{2}$")
VERSION_RE = re.compile(
    r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$"
)


def validate(version, target, url, signature):
    if target not in TARGET_PREFIXES:
        raise ValueError(f"Unsupported target: {target}")

    if not VERSION_RE.fullmatch(version):
        raise ValueError(f"Invalid version syntax: {version}")

    marker = version.rsplit(".", 1)[-1]
    if not MARKER_RE.fullmatch(marker):
        raise ValueError(
            "Version must end with a four-character build marker, "
            "for example 0.2.0-dev.11.1D01"
        )

    if marker[0] not in TARGET_PREFIXES[target]:
        raise ValueError(
            f"Marker {marker} does not belong to target {target}"
        )

    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("Artifact URL must be an absolute HTTPS URL")

    if not signature.strip():
        raise ValueError("Artifact signature must not be empty")

    return marker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--target", required=True, choices=sorted(TARGET_PREFIXES))
    parser.add_argument("--url", required=True)
    parser.add_argument("--signature", required=True)
    parser.add_argument("--notes", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    try:
        marker = validate(
            args.version, args.target, args.url, args.signature
        )

        output = args.output
        if output.exists():
            raise ValueError(f"Refusing to overwrite existing file: {output}")

        output.parent.mkdir(parents=True, exist_ok=True)
        manifest = {
            "version": args.version,
            "notes": args.notes,
            "pub_date": datetime.now(timezone.utc).isoformat().replace(
                "+00:00", "Z"
            ),
            "platforms": {
                args.target: {
                    "signature": args.signature,
                    "url": args.url,
                }
            },
        }

        # Write atomically so an interrupted write cannot leave a partial feed.
        temporary = output.with_name(output.name + ".tmp")
        if temporary.exists():
            raise ValueError(f"Refusing to overwrite temporary file: {temporary}")
        temporary.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(output)

        # Read back and validate the exact file written.
        written = json.loads(output.read_text(encoding="utf-8"))
        if (
            written["version"] != args.version
            or list(written["platforms"]) != [args.target]
            or written["platforms"][args.target]["signature"] != args.signature
            or written["platforms"][args.target]["url"] != args.url
        ):
            output.unlink(missing_ok=True)
            raise ValueError("Read-back validation failed; output was removed")

        print(f"PASS: validated feed for {args.target}, marker {marker}")
        print(f"Output: {output}")
        print("No existing file was overwritten.")
        return 0

    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
