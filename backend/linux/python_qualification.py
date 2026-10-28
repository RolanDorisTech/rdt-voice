"""Shared deterministic direct-requirements hashing for Linux qualification."""
from __future__ import annotations

import hashlib
from pathlib import Path


def canonical_direct_requirements_bytes(raw: bytes) -> bytes:
    """Decode strict UTF-8, normalize all newline forms to LF, encode UTF-8."""
    text = raw.decode("utf-8", errors="strict")
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def requirements_hashes(path: Path) -> dict[str, str]:
    """Return canonical integrity hash and raw checkout hash for one file."""
    raw = path.read_bytes()
    canonical = canonical_direct_requirements_bytes(raw)
    return {
        "DIRECT_REQUIREMENTS_SHA256": hashlib.sha256(canonical).hexdigest(),
        "DIRECT_REQUIREMENTS_RAW_SHA256": hashlib.sha256(raw).hexdigest(),
    }
