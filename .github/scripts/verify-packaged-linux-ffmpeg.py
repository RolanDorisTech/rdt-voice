#!/usr/bin/env python3
"""Verify the installed Linux package's private FFmpeg runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys


class VerificationError(RuntimeError):
    pass


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_inventory(backend_dir: pathlib.Path) -> pathlib.Path:
    if backend_dir.is_symlink() or not backend_dir.is_dir():
        raise VerificationError("backend directory missing or unsafe")
    backend_dir = backend_dir.resolve(strict=True)

    root = backend_dir / "ffmpeg"
    if root.is_symlink() or not root.is_dir():
        raise VerificationError("private FFmpeg directory missing or unsafe")
    root = root.resolve(strict=True)

    manifest_path = backend_dir / "rdt-linux-backend-bundle.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise VerificationError("backend bundle manifest missing or unsafe")
    try:
        bundle = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError(f"cannot read backend manifest: {exc}") from exc

    if not isinstance(bundle, dict) or bundle.get("schema") != "rdt-linux-backend-bundle-v2":
        raise VerificationError("backend manifest schema invalid")
    record = bundle.get("ffmpeg")
    if not isinstance(record, dict) or record.get("version") != "9.1":
        raise VerificationError("FFmpeg manifest version invalid")
    expected = record.get("files")
    if not isinstance(expected, dict):
        raise VerificationError("FFmpeg file inventory missing")

    actual = {}
    regular_count = 0
    symlink_count = 0
    runtime_size = 0

    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        package_path = "ffmpeg/" + relative

        if path.is_symlink():
            target = os.readlink(path)
            if (
                not target
                or pathlib.PurePosixPath(target).is_absolute()
                or "\\" in target
            ):
                raise VerificationError(f"unsafe FFmpeg symlink: {relative}")
            try:
                resolved = path.resolve(strict=True)
                resolved_relative = resolved.relative_to(root).as_posix()
            except (OSError, RuntimeError, ValueError) as exc:
                raise VerificationError(f"escaping or broken symlink: {relative}") from exc
            if not resolved.is_file():
                raise VerificationError(f"symlink target is not a regular file: {relative}")
            actual[package_path] = {
                "type": "symlink",
                "link_target": target,
                "resolved_path": resolved_relative,
                "resolved_size_bytes": resolved.stat().st_size,
                "resolved_sha256": sha256(resolved),
            }
            symlink_count += 1
        elif path.is_file():
            size = path.stat().st_size
            actual[package_path] = {
                "type": "file",
                "bytes": size,
                "sha256": sha256(path),
            }
            regular_count += 1
            runtime_size += size

        elif path.is_dir():
            continue
        else:
            raise VerificationError(f"unsupported filesystem object in private FFmpeg tree: {relative}")

    if actual != expected:
        missing = sorted(set(actual) - set(expected))
        extra = sorted(set(expected) - set(actual))
        changed = sorted(
            key for key in set(actual) & set(expected)
            if actual[key] != expected[key]
        )
        raise VerificationError(
            f"inventory mismatch: missing={missing[:4]}, "
            f"extra={extra[:4]}, changed={changed[:4]}"
        )

    for key, value in (
        ("regular_file_count", regular_count),
        ("symlink_count", symlink_count),
        ("runtime_size_bytes", runtime_size),
    ):
        if type(record.get(key)) is not int or record[key] != value:
            raise VerificationError(f"manifest {key} mismatch")

    build_hash = record.get("build_manifest_sha256")
    if not isinstance(build_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", build_hash):
        raise VerificationError("builder-manifest SHA-256 invalid")

    for name in ("ffmpeg", "ffprobe"):
        binary = root / "bin" / name
        if binary.is_symlink() or not binary.is_file() or not os.access(binary, os.X_OK):
            raise VerificationError(f"private {name} executable missing or unsafe")

    print(f"INSTALLED_FFMPEG_FILES={regular_count}")
    print(f"INSTALLED_FFMPEG_SYMLINKS={symlink_count}")
    print(f"INSTALLED_FFMPEG_RUNTIME_SIZE_BYTES={runtime_size}")
    print("INSTALLED_FFMPEG_MANIFEST_MATCH=PASS")
    return root


def verify_runtime(root: pathlib.Path) -> None:
    lib_dir = root / "lib"
    if lib_dir.is_symlink() or not lib_dir.is_dir():
        raise VerificationError("private FFmpeg lib directory missing or unsafe")
    lib_root = lib_dir.resolve(strict=True)

    env = {key: value for key, value in os.environ.items() if not key.startswith("LD_")}
    env["PATH"] = "/usr/bin:/bin"

    readelf = pathlib.Path("/usr/bin/readelf")
    ldd = pathlib.Path("/usr/bin/ldd")
    if not readelf.is_file() or not ldd.is_file():
        raise VerificationError("readelf or ldd missing; install binutils in test container")

    private_dependency = re.compile(r"^\s*(lib(?:av|sw|postproc)[^\s]*)\s+=>\s+(\S+)")

    for name in ("ffmpeg", "ffprobe"):
        binary = root / "bin" / name
        dynamic = subprocess.run(
            [str(readelf), "-d", str(binary)],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
        ).stdout
        print(f"===== {name} ELF DYNAMIC SECTION =====")
        print(dynamic)
        if "(RPATH)" not in dynamic or "$ORIGIN/../lib" not in dynamic or "(RUNPATH)" in dynamic:
            raise VerificationError(f"{name} lacks the required private DT_RPATH")

        result = subprocess.run(
            [str(ldd), str(binary)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
        )
        print(f"===== {name} LDD =====")
        print(result.stdout)
        if result.returncode or "not found" in result.stdout:
            raise VerificationError(f"{name} has unresolved shared-library dependencies")

        private_count = 0
        for line in result.stdout.splitlines():
            match = private_dependency.match(line)
            if not match:
                continue
            try:
                pathlib.Path(match.group(2)).resolve(strict=True).relative_to(lib_root)
            except (OSError, RuntimeError, ValueError) as exc:
                raise VerificationError(
                    f"{name} resolves {match.group(1)} outside private FFmpeg lib"
                ) from exc
            private_count += 1

        if private_count == 0:
            raise VerificationError(f"{name} has no private libav/libsw dependencies")
        print(f"{name.upper()}_PRIVATE_FFMPEG_DEPENDENCIES={private_count}")

        print(f"===== RESTRICTED-PATH {name} VERSION SMOKE =====")
        subprocess.run([str(binary), "-version"], check=True, env=env)

    print("INSTALLED_PRIVATE_FFMPEG_RUNTIME=PASS")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend-dir", required=True, type=pathlib.Path)
    args = parser.parse_args()
    try:
        root = verify_inventory(args.backend_dir)
        verify_runtime(root)
    except (OSError, VerificationError, subprocess.CalledProcessError) as exc:
        print(f"INSTALLED_PRIVATE_FFMPEG_FAILED: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
