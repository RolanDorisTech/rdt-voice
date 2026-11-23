#!/usr/bin/env python3
"""Merge common Connect notices into a platform collector's notice inventory."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile


MANIFEST_NAME = "RDT-CONNECT-SHARED-NOTICES-MANIFEST.json"
SCHEMA = "rdt-connect-shared-third-party-notices-v1"
BUILD_SECTION = "## RDT Connect embedded RDT Voice frontend and offline runtime"


def fail(message: str) -> None:
    raise RuntimeError(message)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_files(root: Path) -> list[Path]:
    if root.is_symlink() or not root.is_dir():
        fail(f"notice root is missing or unsafe: {root}")
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            fail(f"symlink is not allowed in notice tree: {path}")
        if path.is_file():
            files.append(path)
    return files


def merge_build_notices(source: Path, target: Path) -> dict:
    if target.is_symlink() or not target.is_file():
        fail("platform BUILD-NOTICES.md is missing or unsafe")
    source_text = source.read_text(encoding="utf-8")
    lines = source_text.splitlines()
    if not lines or not lines[0].startswith("# "):
        fail("shared BUILD-NOTICES.md has an unexpected heading")
    body = "\\n".join(lines[1:]).strip()
    if not body:
        fail("shared BUILD-NOTICES.md has no content")

    section = f"{BUILD_SECTION}\\n\\n{body}\\n"
    existing = target.read_text(encoding="utf-8")
    if BUILD_SECTION in existing:
        if existing.count(BUILD_SECTION) != 1 or section not in existing:
            fail("existing shared R82 build-notice section differs; refusing overwrite")
    else:
        target.write_text(existing.rstrip() + "\\n\\n" + section, encoding="utf-8")

    return {
        "bytes": target.stat().st_size,
        "sha256": sha256(target),
        "source_bytes": source.stat().st_size,
        "source_sha256": sha256(source),
    }


def merge_shared_notices(shared_root: Path, platform_root: Path) -> dict:
    shared_files = safe_files(shared_root)
    safe_files(platform_root)
    if not shared_files:
        fail(f"shared notice source is empty: {shared_root}")

    stage = Path(tempfile.mkdtemp(
        prefix=".rdt-notices-stage-", dir=platform_root.parent
    ))
    backup = platform_root.parent / f".{platform_root.name}.rdt-notices-backup"

    try:
        shutil.copytree(platform_root, stage, dirs_exist_ok=True)
        records = {}

        for source in shared_files:
            relative = source.relative_to(shared_root)
            name = relative.as_posix()
            if PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts:
                fail(f"unsafe shared notice path: {name}")
            if name == MANIFEST_NAME:
                fail(f"reserved shared notice path: {name}")

            target = stage / relative
            if name == "BUILD-NOTICES.md":
                records[name] = merge_build_notices(source, target)
                continue

            if target.exists():
                if target.is_symlink() or not target.is_file():
                    fail(f"shared notice conflicts with non-file destination: {name}")
                if sha256(target) != sha256(source):
                    fail(f"refusing to overwrite differing platform notice: {name}")
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)

            records[name] = {
                "bytes": target.stat().st_size,
                "sha256": sha256(target),
                "source_sha256": sha256(source),
            }

        manifest = {
            "schema": SCHEMA,
            "source": "resources/third_party_notices",
            "file_count": len(records),
            "files": records,
        }
        manifest_path = stage / MANIFEST_NAME
        encoded = json.dumps(manifest, indent=2, sort_keys=True) + "\n"

        if manifest_path.exists():
            if manifest_path.is_symlink() or not manifest_path.is_file():
                fail("existing shared notice manifest destination is unsafe")
            if manifest_path.read_text(encoding="utf-8") != encoded:
                fail("existing shared notice manifest differs; refusing implicit replacement")
        else:
            temporary = stage / (MANIFEST_NAME + ".tmp")
            temporary.write_text(encoded, encoding="utf-8")
            os.replace(temporary, manifest_path)

        for name, record in records.items():
            deployed = stage / PurePosixPath(name)
            if (
                deployed.stat().st_size != record["bytes"]
                or sha256(deployed) != record["sha256"]
            ):
                fail(f"merged shared notice verification failed: {name}")

        safe_files(stage)
        if backup.exists() or backup.is_symlink():
            fail(f"stale notice transaction backup exists: {backup}")

        os.replace(platform_root, backup)
        try:
            os.replace(stage, platform_root)
        except BaseException:
            os.replace(backup, platform_root)
            raise

        try:
            shutil.rmtree(backup)
        except BaseException:
            # Restore the original platform inventory if backup cleanup fails.
            failed_new = platform_root.with_name(platform_root.name + ".failed-promotion")
            os.replace(platform_root, failed_new)
            os.replace(backup, platform_root)
            shutil.rmtree(failed_new, ignore_errors=True)
            raise

        print(
            f"PASS: merged {len(records)} shared notice entries; "
            "platform BUILD-NOTICES content preserved"
        )
        return manifest
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        if backup.exists() and not platform_root.exists():
            os.replace(backup, platform_root)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--platform", choices=("linux", "windows"), required=True)
    args = parser.parse_args()

    resources = args.repo_root.resolve() / "rdt_connect/src-tauri/resources"
    shared = resources / "third_party_notices"
    platform = resources / f"{args.platform}-third-party-notices"
    manifest = merge_shared_notices(shared, platform)
    print(
        f"PASS: verified {MANIFEST_NAME} "
        f"({manifest['file_count']} shared entries)"
    )


if __name__ == "__main__":
    main()
