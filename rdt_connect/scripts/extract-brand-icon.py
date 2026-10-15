#!/usr/bin/env python3
"""Extract the current embedded WebP favicon from root index.html for the desktop shell."""
from __future__ import annotations
import base64
import pathlib
import re
import sys


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: extract-brand-icon.py index.html output.webp", file=sys.stderr)
        return 2
    source = pathlib.Path(sys.argv[1]).expanduser().resolve()
    target = pathlib.Path(sys.argv[2]).expanduser().resolve()
    text = source.read_text(encoding="utf-8")
    match = re.search(
        r'<link\b[^>]*\brel=[\"\']icon[\"\'][^>]*\bhref=[\"\']data:image/webp;base64,([A-Za-z0-9+/=\s]+)[\"\']',
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        # Permit href to appear before rel in the tag.
        tag = re.search(r'<link\b[^>]*\brel=[\"\']icon[\"\'][^>]*>', text, flags=re.IGNORECASE)
        if tag:
            match = re.search(r'\bhref=[\"\']data:image/webp;base64,([A-Za-z0-9+/=\s]+)[\"\']', tag.group(0), flags=re.IGNORECASE)
    if not match:
        print("ERROR: Could not find the embedded WebP favicon in the supplied root index.html.", file=sys.stderr)
        return 3
    payload = re.sub(r'\s+', '', match.group(1))
    data = base64.b64decode(payload, validate=True)
    if not data.startswith(b'RIFF') or data[8:12] != b'WEBP':
        print("ERROR: Extracted favicon does not appear to be WebP data.", file=sys.stderr)
        return 4
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    print(f"Extracted current RDT Voice favicon to {target}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
