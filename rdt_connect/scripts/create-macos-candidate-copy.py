#!/usr/bin/env python3
"""Create a complete, isolated source copy for a macOS candidate build."""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

EXCLUDED_DIRS = {
    ".git", "build", "node_modules", ".venv", "venv", "__pycache__",
    ".pytest_cache", "dist", "target", ".cache", "coverage", ".turbo",
}
EXCLUDED_SUFFIXES = {".zip", ".dmg", ".pkg", ".app"}


def create_candidate(source_root: Path, destination: Path) -> Path:
    source_root = source_root.resolve(strict=True)
    destination = destination.absolute()
    if not (source_root / "rdt_connect/package.json").is_file():
        raise RuntimeError("Repository root does not contain rdt_connect/package.json.")
    if not (source_root / "backend/apple/server.py").is_file():
        raise RuntimeError("Repository root does not contain backend/apple/server.py.")
    if destination.exists() or destination.is_symlink():
        raise RuntimeError(f"Candidate destination already exists; refusing overwrite: {destination}")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)

        def ignore(_directory: str, names: list[str]) -> set[str]:
            ignored = {name for name in names if name in EXCLUDED_DIRS}
            ignored.update(
                name for name in names
                if Path(name).suffix.lower() in EXCLUDED_SUFFIXES
            )
            return ignored

        shutil.copytree(source_root, destination, symlinks=True, ignore=ignore)
        source_modules = source_root / "rdt_connect/node_modules"
        candidate_modules = destination / "rdt_connect/node_modules"
        if source_modules.is_dir() and not candidate_modules.exists():
            candidate_modules.symlink_to(source_modules.resolve(), target_is_directory=True)
        return destination
    except Exception:
        if destination.exists() and not destination.is_symlink():
            shutil.rmtree(destination)
        raise


def main() -> int:
    script = Path(__file__).resolve()
    source_root = script.parents[2]
    default_destination = source_root / "build/benchmark-1d10-candidate-20261105"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=default_destination)
    args = parser.parse_args()
    try:
        candidate = create_candidate(source_root, args.destination)
    except Exception as error:
        print(f"FAIL: {error}")
        return 2
    print(f"CREATED: {candidate}")
    print(f"BUILD SOURCE: {candidate / 'rdt_connect'}")
    print(f"BACKEND SOURCE: {candidate / 'backend/apple/server.py'}")
    print(f"NODE MODULES: {'shared read-only input' if (candidate / 'rdt_connect/node_modules').is_symlink() else 'not linked'}")
    print("PRESERVED: original source tree, installed app, and existing build artifacts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
