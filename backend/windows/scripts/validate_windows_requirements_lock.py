#!/usr/bin/env python3
"""Validate a Windows architecture-specific pip hash lock before packaging.

This performs static lock-shape checks and verifies the running Python process is
native to the requested Windows architecture. Pip's subsequent
`install --require-hashes --only-binary=:all:` performs the authoritative artifact
hash and wheel-compatibility checks against the actual index artifacts.
"""
from __future__ import annotations

import argparse
import platform
import re
import sys
from pathlib import Path

PACKAGE_LINE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[A-Za-z0-9._,-]+\])?"
    r"==(?P<version>[A-Za-z0-9][A-Za-z0-9.!+_-]*)"
    r"(?:\s*;\s*.+?)?(?:\s+\\)?$"
)
HASH_TOKEN = re.compile(r"--hash=sha256:([0-9a-fA-F]{64})\b")
HASH_ONLY = re.compile(r"^\s*--hash=sha256:[0-9a-fA-F]{64}(?:\s+\\)?\s*$")


def canonicalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_lock(text: str) -> dict[str, dict[str, object]]:
    """Return package->version/hash data; reject unpinned or unhashed entries."""
    packages: dict[str, dict[str, object]] = {}
    active_name: str | None = None

    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("--hash="):
            if active_name is None or not HASH_ONLY.fullmatch(raw_line):
                raise ValueError(f"line {number}: malformed or unattached hash entry")
            digest = HASH_TOKEN.search(line)
            assert digest is not None
            hashes = packages[active_name]["hashes"]
            assert isinstance(hashes, set)
            hashes.add(digest.group(1).lower())
            continue
        if line.startswith("--") or line.startswith(("-r ", "-c ", "-e ", "git+", "http:", "https:")):
            raise ValueError(f"line {number}: lock options, includes, or direct URLs are not allowed")

        # pip-compile may emit hashes inline or as following continuation lines.
        package_part = line.split("--hash=", 1)[0].rstrip().removesuffix("\\").rstrip()
        match = PACKAGE_LINE.fullmatch(package_part)
        if not match:
            raise ValueError(f"line {number}: expected an exact name==version pin, got {line!r}")

        name = canonicalize(match.group("name"))
        if name in packages:
            raise ValueError(f"line {number}: duplicate package entry: {name}")
        inline_hashes = {h.lower() for h in HASH_TOKEN.findall(line)}
        packages[name] = {"version": match.group("version"), "hashes": inline_hashes}
        active_name = name

    if not packages:
        raise ValueError("lock has no package entries")
    missing = sorted(name for name, data in packages.items() if not data["hashes"])
    if missing:
        raise ValueError("every package must have at least one SHA-256 hash; missing: " + ", ".join(missing))
    return packages


def parse_direct_requirements(text: str) -> dict[str, str]:
    """Parse the exact direct-pin source used to generate architecture locks."""
    direct: dict[str, str] = {}
    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "#" in line:
            line = line.split("#", 1)[0].strip()
        match = PACKAGE_LINE.fullmatch(line)
        if not match or ";" in line or "\\" in line:
            raise ValueError(f"direct requirements line {number}: expected exact name==version pin, got {raw_line!r}")
        name = canonicalize(match.group("name"))
        if name in direct:
            raise ValueError(f"direct requirements line {number}: duplicate pin {name}")
        direct[name] = match.group("version")
    if not direct:
        raise ValueError("direct requirements input has no pins")
    return direct


def check_direct_pins(lock_packages: dict[str, dict[str, object]], direct_requirements_text: str) -> None:
    direct = parse_direct_requirements(direct_requirements_text)
    stale = []
    for name, version in direct.items():
        locked = lock_packages.get(name)
        if locked is None:
            stale.append(f"{name} missing from lock")
        elif locked["version"] != version:
            stale.append(f"{name}: direct pin {version}, lock has {locked['version']}")
    if stale:
        raise ValueError("lock is stale relative to direct dependency pins: " + "; ".join(stale))


def check_process_architecture(target_arch: str, machine: str | None = None) -> None:
    machine = (machine or platform.machine()).strip().lower()
    expected = {"arm64": {"arm64", "aarch64"}, "x64": {"amd64", "x86_64", "x64"}}
    if machine not in expected[target_arch.lower()]:
        raise ValueError(
            f"native architecture mismatch: requested {target_arch}, Python reports {machine!r}; "
            "generate/qualify Windows locks on the corresponding native Windows runner"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--target-arch", required=True, choices=("ARM64", "x64"))
    parser.add_argument("--direct-requirements", required=True, type=Path, help="direct pinned inputs used for this lock")
    parser.add_argument("--static-only", action="store_true", help="test-only static validation; skips process architecture")
    args = parser.parse_args()
    try:
        if not args.lock.is_file():
            raise ValueError(f"architecture-qualified dependency lock is missing: {args.lock}")
        parsed = parse_lock(args.lock.read_text(encoding="utf-8"))
        if not args.direct_requirements.is_file():
            raise ValueError(f"direct dependency input is missing: {args.direct_requirements}")
        check_direct_pins(parsed, args.direct_requirements.read_text(encoding="utf-8"))
        if not args.static_only:
            if sys.platform != "win32":
                raise ValueError("dependency lock qualification must run on native Windows")
            check_process_architecture(args.target_arch)
        print(f"LOCK_PATH={args.lock}")
        print(f"TARGET_ARCH={args.target_arch}")
        print(f"LOCK_PACKAGES={len(parsed)}")
        print(f"DIRECT_PIN_COUNT={len(parse_direct_requirements(args.direct_requirements.read_text(encoding='utf-8')))}")
        print("LOCK_STATIC_VALIDATION=PASS")
        if not args.static_only:
            print("LOCK_PROCESS_ARCHITECTURE=PASS")
            print("NOTE: pip --require-hashes --only-binary=:all: must still pass to verify selected wheel hashes and native wheel availability.")
        return 0
    except (OSError, ValueError) as exc:
        print(f"LOCK_VALIDATION_FAILED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
