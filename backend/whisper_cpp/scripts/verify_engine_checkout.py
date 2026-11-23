#!/usr/bin/env python3
"""Fail closed unless whisper.cpp source and initialized gitlinks match the pin."""
from __future__ import annotations
import argparse
import json
import subprocess
from pathlib import Path


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], check=True, text=True, capture_output=True)
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--pins", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    pins = json.loads(args.pins.read_text(encoding="utf-8"))
    expected = pins["source_commit"]
    actual = git(args.source, "rev-parse", "HEAD")
    if actual != expected:
        raise SystemExit(f"engine commit mismatch: expected {expected}, got {actual}")
    submodules = git(args.source, "submodule", "status", "--recursive")
    invalid = [line for line in submodules.splitlines() if line and line[0] in "-+U"]
    if invalid:
        raise SystemExit("one or more whisper.cpp submodules are missing or mismatched:\n" + "\n".join(invalid))
    report = {
        "source_repository": pins["source_repository"],
        "version": pins["version"],
        "commit": actual,
        "architecture": __import__("platform").machine(),
        "submodule_status_recursive": submodules.splitlines(),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"ENGINE_COMMIT_VERIFIED={actual}")
    print("SUBMODULE_REVISIONS_VERIFIED=" + (str(len(submodules.splitlines())) if submodules else "0"))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
