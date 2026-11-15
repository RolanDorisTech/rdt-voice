#!/usr/bin/env python3
"""Enforce exactly one physical MLX metallib at the active loader path."""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

CANONICAL = Path("mlx/mlx.metallib")
ALIASES = {Path("mlx/lib/mlx.metallib"): "../mlx.metallib", Path("mlx.metallib"): "mlx/mlx.metallib"}
EXPECTED = {CANONICAL, *ALIASES}


def fail(message: str) -> None:
    raise RuntimeError(message)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def entries_named_metallib(internal: Path) -> list[Path]:
    return sorted(internal.rglob("mlx.metallib"))


def normalize_internal(internal: Path, *, verify_only: bool = False) -> str:
    if not internal.is_dir():
        fail(f"MLX internal directory is missing: {internal}")
    internal_real = internal.resolve(strict=True)
    canonical = internal / CANONICAL
    entries = entries_named_metallib(internal)
    physical = [p for p in entries if p.is_file() and not p.is_symlink()]

    # In staging mode, PyInstaller may emit the canonical file at a different
    # MLX-native-library lookup path. Relocate one byte-identical copy once.
    if not canonical.is_file() or canonical.is_symlink():
        if verify_only:
            fail(f"Canonical MLX shader must be a regular file: {canonical}")
        if canonical.is_symlink():
            fail(f"Canonical MLX shader cannot be a symlink: {canonical}")
        candidates = [p for p in physical if p != canonical]
        if not candidates:
            fail(f"No physical MLX shader found under: {internal}")
        canonical.parent.mkdir(parents=True, exist_ok=True)
        seed = candidates[0]
        canonical_hash = sha256(seed)
        for candidate in candidates[1:]:
            if sha256(candidate) != canonical_hash:
                fail(f"Multiple non-identical MLX shaders found; refusing consolidation: {candidate}")
        seed.replace(canonical)
        entries = entries_named_metallib(internal)
        physical = [p for p in entries if p.is_file() and not p.is_symlink()]

    if not canonical.is_file() or canonical.is_symlink():
        fail(f"Canonical MLX shader must be a regular file: {canonical}")
    canonical_real = canonical.resolve(strict=True)
    if not canonical_real.is_relative_to(internal_real):
        fail("Canonical MLX shader resolves outside the app/resource bundle.")
    expected_hash = sha256(canonical)

    # Preflight all copies and symlinks before removing anything.
    for path in entries:
        if path == canonical:
            continue
        if path.is_symlink():
            try:
                resolved = path.resolve(strict=True)
            except (OSError, RuntimeError):
                fail(f"Broken MLX shader symlink: {path}")
            if not resolved.is_relative_to(internal_real):
                fail(f"MLX shader symlink escapes its bundle: {path} -> {path.readlink()}")
            if not resolved.is_file() or sha256(resolved) != expected_hash:
                fail(f"MLX shader symlink does not resolve to identical shader bytes: {path}")
        elif path.is_file():
            if sha256(path) != expected_hash:
                fail(f"MLX shader hash mismatch; refusing removal: {path}")
        else:
            fail(f"Unexpected non-file MLX shader path: {path}")

    # Never create or validate aliases through a parent symlink that escapes the bundle.
    expected_targets = {internal / rel: target for rel, target in ALIASES.items()}
    for alias in expected_targets:
        try:
            alias_parent_real = alias.parent.resolve(strict=False)
        except (OSError, RuntimeError):
            fail(f"Could not resolve MLX alias parent safely: {alias.parent}")
        if not alias_parent_real.is_relative_to(internal_real):
            fail(f"MLX alias parent escapes its bundle: {alias.parent}")

    # Verification mode never mutates: packaging steps use it as a hard gate.
    if verify_only:
        found = {p.relative_to(internal) for p in entries}
        if found != EXPECTED:
            fail(f"Unexpected MLX metallib paths: {sorted(map(str, found))}")
        if len(physical) != 1 or physical[0] != canonical:
            fail(f"Expected one physical MLX shader at {CANONICAL}; found {physical}")
        for alias, target in expected_targets.items():
            if not alias.is_symlink() or alias.readlink().as_posix() != target:
                fail(f"Expected relative MLX alias {alias} -> {target}")
            if alias.resolve(strict=True) != canonical_real:
                fail(f"MLX alias misses canonical shader: {alias}")
        return expected_hash

    # Remove every validated duplicate and any redundant in-bundle alias. A
    # mismatching or external file/symlink has already failed the preflight.
    for path in entries:
        if path == canonical:
            continue
        if path.is_symlink() or path.is_file():
            path.unlink()

    for alias, target in expected_targets.items():
        alias.parent.mkdir(parents=True, exist_ok=True)
        if alias.exists() or alias.is_symlink():
            fail(f"Refusing to overwrite unexpected path while creating MLX alias: {alias}")
        alias.symlink_to(target)

    # Re-scan and verify the exact on-disk invariant.
    final_entries = entries_named_metallib(internal)
    found = {p.relative_to(internal) for p in final_entries}
    if found != EXPECTED:
        fail(f"Final MLX metallib path set differs from contract: {sorted(map(str, found))}")
    final_physical = [p for p in final_entries if p.is_file() and not p.is_symlink()]
    if final_physical != [canonical]:
        fail(f"Expected exactly one physical MLX shader; found: {final_physical}")
    for alias, target in expected_targets.items():
        if not alias.is_symlink() or alias.readlink().as_posix() != target:
            fail(f"Final MLX symlink verification failed: {alias}")
        if alias.resolve(strict=True) != canonical_real:
            fail(f"Final MLX symlink escapes or misses canonical shader: {alias}")
    return expected_hash


def verify_bundlewide_mlx_layout(bundle: Path) -> None:
    internal = Path("Contents/Resources/rdt_voice_backend/_internal")
    canonical_rel = internal / CANONICAL
    expected = {canonical_rel, *(internal / alias for alias in ALIASES)}
    entries = sorted(bundle.rglob("mlx.metallib"))
    found = {path.relative_to(bundle) for path in entries}

    if found != expected:
        unexpected = sorted(map(str, found - expected))
        missing = sorted(map(str, expected - found))
        fail(
            "Bundle-wide MLX shader layout is not exact; "
            f"unexpected={unexpected}, missing={missing}"
        )

    canonical = bundle / canonical_rel
    if not canonical.is_file() or canonical.is_symlink():
        fail(f"Bundle canonical MLX shader is not one regular file: {canonical}")

    physical = [
        path for path in entries
        if path.is_file() and not path.is_symlink()
    ]
    if physical != [canonical]:
        fail(f"Bundle must contain exactly one physical MLX shader; found={physical}")

    canonical_real = canonical.resolve(strict=True)
    for relative, target in ALIASES.items():
        alias = bundle / internal / relative
        if not alias.is_symlink() or alias.readlink().as_posix() != target:
            fail(f"Bundle alias is not the required relative symlink: {alias}")
        if alias.resolve(strict=True) != canonical_real:
            fail(f"Bundle alias does not resolve to the canonical shader: {alias}")

    print("PASS: entire app bundle contains exactly one physical MLX metallib and no aliases.")


def physical_payload_bytes(bundle: Path) -> int:
    total = 0
    for path in bundle.rglob("*"):
        if path.is_file() and not path.is_symlink():
            total += path.stat().st_size
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app_bundle", nargs="?", type=Path)
    parser.add_argument("--internal-dir", type=Path,
                        help="normalize a PyInstaller _internal directory before app bundling")
    parser.add_argument("--verify-only", action="store_true",
                        help="fail unless the exact one-file/no-alias invariant already holds")
    parser.add_argument("--max-app-mib", type=int,
                        help="fail if the app's physical file payload exceeds this MiB budget")
    parser.add_argument("--target-app-mib", type=int,
                        help="advisory physical app payload target in MiB")
    args = parser.parse_args()
    try:
        if bool(args.app_bundle) == bool(args.internal_dir):
            fail("provide exactly one app_bundle or --internal-dir")
        if args.max_app_mib is not None and (args.internal_dir or args.max_app_mib < 1):
            fail("--max-app-mib requires an app bundle and a positive limit")
        if args.target_app_mib is not None and (
            args.internal_dir or args.target_app_mib < 1
            or (args.max_app_mib is not None and args.target_app_mib > args.max_app_mib)
        ):
            fail("--target-app-mib requires an app bundle and must not exceed the hard limit")
        if args.app_bundle:
            bundle = args.app_bundle
            if not bundle.is_dir():
                fail(f"App bundle does not exist: {bundle}")
            internal = bundle / "Contents/Resources/rdt_voice_backend/_internal"
        else:
            bundle = None
            internal = args.internal_dir
        digest = normalize_internal(internal, verify_only=args.verify_only)
        if bundle is not None:
            verify_bundlewide_mlx_layout(bundle)
        if args.max_app_mib is not None:
            payload = physical_payload_bytes(bundle)
            limit = args.max_app_mib * 1024 * 1024
            if payload > limit:
                fail(f"App payload {payload / 1048576:.1f} MiB exceeds {args.max_app_mib} MiB budget")
            print(f"PASS: physical app payload {payload / 1048576:.1f} MiB <= {args.max_app_mib} MiB")
        if args.target_app_mib is not None:
            target_payload = physical_payload_bytes(bundle)
            if target_payload > args.target_app_mib * 1024 * 1024:
                print(f"WARN: physical app payload exceeds {args.target_app_mib} MiB target")
            else:
                print(f"PASS: physical app payload is within {args.target_app_mib} MiB target")
        action = "verified" if args.verify_only else "normalized"
        print(f"PASS: {action} one physical MLX metallib ({digest[:12]}) and two verified relative symlinks.")
    except (OSError, RuntimeError) as error:
        print(f"ERROR: MLX metallib invariant failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
