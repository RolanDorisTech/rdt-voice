#!/usr/bin/env python3
"""Fail-closed validation for a Linux pip-tools requirements lock."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

NAME = r"[A-Za-z0-9][A-Za-z0-9._-]*"
VERSION = r"[^\s;#\\]+"
DIRECT_RE = re.compile(rf"^({NAME})==({VERSION})$")
LOCK_PIN_RE = re.compile(rf"^\s*({NAME})==({VERSION})")
HASH_TOKEN_RE = re.compile(r"--hash=([^\s\\]+)")
SHA256_RE = re.compile(r"sha256:[0-9a-fA-F]{64}")


def normalize_name(value: str) -> str:
    """Apply Python package-name normalization."""
    return re.sub(r"[-_.]+", "-", value).lower()


def parse_direct_requirements(path: Path) -> dict[str, dict[str, str]]:
    """Accept only reviewed, exact name==version direct inputs."""
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="strict")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    result: dict[str, dict[str, str]] = {}

    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = DIRECT_RE.fullmatch(stripped)
        if not match:
            raise ValueError(
                f"Unsupported direct requirement syntax at {path}:{number}: "
                f"{stripped!r}; expected an exact name==version pin."
            )
        name, version = match.groups()
        key = normalize_name(name)
        if key in result:
            raise ValueError(f"Duplicate direct requirement: {name}")
        result[key] = {"name": name, "version": version}

    if not result:
        raise ValueError(f"No direct requirements found in {path}")
    return result


def parse_lock(path: Path) -> dict[str, dict[str, Any]]:
    """Parse pip-compile output and require SHA-256 hashes for every package."""
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="strict")
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    result: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None

    def finish() -> None:
        nonlocal current
        if current is None:
            return

        block = "\n".join(current["lines"])
        tokens = HASH_TOKEN_RE.findall(block)
        if not tokens:
            raise ValueError(
                f"Lock entry {current['name']}=={current['version']} "
                "contains no SHA-256 artifact hashes."
            )
        if any(not SHA256_RE.fullmatch(token) for token in tokens):
            raise ValueError(
                f"Lock entry {current['name']}=={current['version']} "
                "contains a malformed or non-SHA-256 hash."
            )

        key = normalize_name(current["name"])
        if key in result:
            raise ValueError(f"Duplicate lock entry: {current['name']}")
        result[key] = {
            "name": current["name"],
            "version": current["version"],
            "sha256_hash_count": len(tokens),
        }
        current = None

    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        match = LOCK_PIN_RE.match(line)
        if match:
            finish()
            current = {
                "name": match.group(1),
                "version": match.group(2),
                "lines": [line],
            }
            continue

        if current is None:
            raise ValueError(
                f"Unexpected lock content at {path}:{number}: {stripped!r}"
            )
        current["lines"].append(line)

    finish()
    if not result:
        raise ValueError(f"No pinned packages found in lock {path}")
    return result


def validate_dependency_lock(
    direct: dict[str, dict[str, str]],
    locked: dict[str, dict[str, Any]],
    installed: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Verify direct pins and, optionally, exact installed-graph equality."""
    mismatches: list[str] = []
    for key, requirement in direct.items():
        entry = locked.get(key)
        if entry is None:
            mismatches.append(f"missing direct package {requirement['name']}")
        elif entry["version"] != requirement["version"]:
            mismatches.append(
                f"{requirement['name']}: direct={requirement['version']} "
                f"lock={entry['version']}"
            )

    if mismatches:
        raise ValueError("Direct/lock mismatch: " + "; ".join(mismatches))

    graph_result: dict[str, Any] = {"checked": False}
    if installed is not None:
        expected = {name: entry["version"] for name, entry in locked.items()}
        actual = {normalize_name(name): version for name, version in installed.items()}
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        wrong = sorted(
            name for name in set(expected) & set(actual)
            if expected[name] != actual[name]
        )
        if missing or extra or wrong:
            raise ValueError(
                f"Installed graph mismatch: missing={missing}, "
                f"extra={extra}, wrong_versions={wrong}"
            )
        graph_result = {
            "checked": True,
            "package_count": len(actual),
            "matches_lock": True,
        }

    return {
        "direct_requirements_count": len(direct),
        "lock_package_count": len(locked),
        "lock_entries_have_sha256_hashes": True,
        "direct_pins_match_lock": True,
        "installed_graph": graph_result,
        "direct_requirements": direct,
        "locked_packages": locked,
    }


def load_installed_graph(path: Path) -> dict[str, str]:
    """Read pip list --format=json output."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read installed package graph {path}: {exc}") from exc

    if not isinstance(value, list):
        raise ValueError("Installed package graph must be a JSON array.")

    packages: dict[str, str] = {}
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ValueError("Malformed installed package graph entry.")
        name = item["name"]
        version = item.get("version")
        if not isinstance(version, str) or not version:
            raise ValueError(f"Missing installed version for {name}.")
        key = normalize_name(name)
        if key in packages:
            raise ValueError(f"Duplicate installed package: {name}")
        packages[key] = version
    return packages


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--direct-requirements", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--installed-json", type=Path)
    parser.add_argument("--output-json", type=Path)
    args = parser.parse_args()

    try:
        direct = parse_direct_requirements(args.direct_requirements)
        locked = parse_lock(args.lock)
        installed = (
            load_installed_graph(args.installed_json)
            if args.installed_json else None
        )
        report = validate_dependency_lock(direct, locked, installed)
        report["direct_requirements_file"] = str(args.direct_requirements)
        report["lock_file"] = str(args.lock)
        rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.output_json:
            args.output_json.write_text(rendered, encoding="utf-8")
        print(rendered, end="")
        return 0
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"LINUX_LOCK_VALIDATION_FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
