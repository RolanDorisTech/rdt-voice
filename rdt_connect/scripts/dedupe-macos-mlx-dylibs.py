#!/usr/bin/env python3
"""Deduplicate identical PyInstaller MLX dylibs, preserving both lookup paths."""
import argparse, hashlib, sys
from pathlib import Path

NAMES = ("libmlx.dylib", "libjaccl.dylib")

def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def inspect_pair(top, nested):
    tl, nl = top.is_symlink(), nested.is_symlink()
    if tl and nl:
        raise RuntimeError(f"Both paths are symlinks: {top} / {nested}")
    if not tl and not top.is_file():
        raise RuntimeError(f"Missing/non-regular top-level library: {top}")
    if not nl and not nested.is_file():
        raise RuntimeError(f"Missing/non-regular nested library: {nested}")

    if tl:
        if top.readlink().as_posix() != f"mlx/lib/{top.name}" or top.resolve() != nested.resolve():
            raise RuntimeError(f"Unexpected top-level alias: {top}")
        return nested, top, sha(nested), False
    if nl:
        if nested.readlink().as_posix() != f"../../{top.name}" or nested.resolve() != top.resolve():
            raise RuntimeError(f"Unexpected nested alias: {nested}")
        return top, nested, sha(top), False

    a, b = sha(top), sha(nested)
    if a != b:
        raise RuntimeError(f"Refusing non-identical dylibs: {top} / {nested}")
    return top, nested, a, True

def normalize(root, verify=False) -> int:
    root = root.resolve(strict=True)
    nested_dir = root / "mlx/lib"
    if not nested_dir.is_dir():
        raise RuntimeError(f"Missing MLX library directory: {nested_dir}")
    for directory in (root / "mlx", nested_dir):
        try:
            resolved_directory = directory.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise RuntimeError(f"Cannot safely resolve MLX directory: {directory}") from error
        if not resolved_directory.is_relative_to(root):
            raise RuntimeError(f"MLX directory escapes _internal: {directory}")


    for name in NAMES:
        expected = {Path(name), Path("mlx/lib") / name}
        found = {p.relative_to(root) for p in root.rglob(name)}
        if found != expected:
            raise RuntimeError(
                f"Unexpected {name} paths; expected={sorted(map(str, expected))}, "
                f"found={sorted(map(str, found))}"
            )

    # Validate both pairs before making any change.
    pairs = [inspect_pair(root / n, nested_dir / n) for n in NAMES]
    for physical, alias, digest, change in pairs:
        for candidate in (physical, alias):
            try:
                resolved = candidate.resolve(strict=True)
            except (OSError, RuntimeError) as error:
                raise RuntimeError(f"Cannot safely resolve MLX library: {candidate}") from error
            if not resolved.is_relative_to(root):
                raise RuntimeError(f"MLX library escapes _internal: {candidate}")
    bytes_reclaimed = sum(
        alias.stat().st_size
        for physical, alias, digest, change in pairs if change
    )

    if verify:
        for physical, alias, _, change in pairs:
            if change or physical.is_symlink() or not alias.is_symlink():
                raise RuntimeError(f"MLX pair is not deduplicated: {physical} / {alias}")
        return 0

    for physical, alias, _, change in pairs:
        if change:
            alias.unlink()
            alias.symlink_to(f"../../{physical.name}")

    for physical, alias, digest, _ in pairs:
        if (physical.is_symlink() or not physical.is_file()
                or not alias.is_symlink() or alias.resolve() != physical.resolve()
                or sha(alias) != digest):
            raise RuntimeError(f"Post-deduplication verification failed: {physical} / {alias}")

    return bytes_reclaimed

def main():
    p = argparse.ArgumentParser()
    p.add_argument("internal_dir", type=Path)
    p.add_argument("--verify-only", action="store_true")
    a = p.parse_args()
    try:
        reclaimed = normalize(a.internal_dir, a.verify_only)
    except Exception as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    print(
        "PASS: one physical copy per MLX dylib; both lookup paths retained; "
        f"bytes reclaimed this run={reclaimed:,}"
    )
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
