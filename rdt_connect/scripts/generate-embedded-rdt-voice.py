#!/usr/bin/env python3
"""Build and transactionally install the pinned R82 Connect web resources."""
import argparse
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile


def fail(message):
    raise SystemExit("RDT Voice resource generation FAILED: " + message)


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def run(command, *, cwd, env=None):
    print("+", " ".join(str(x) for x in command), flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def safe_relative(value):
    rel = pathlib.PurePosixPath(value)
    if rel.is_absolute() or not rel.parts or ".." in rel.parts or "." in rel.parts:
        fail("unsafe relative path in manifest: " + str(value))
    return rel


def copy_file_verified(source, destination, expected, label):
    if not source.is_file() or source.is_symlink():
        fail(f"{label} is missing or is a symlink: {source}")
    actual = sha256(source)
    if actual != expected:
        fail(f"{label} SHA-256 mismatch: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    if sha256(destination) != expected:
        fail(f"{label} copy SHA-256 mismatch: {destination}")


def inventory(root):
    return {
        path.relative_to(root).as_posix(): sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != ".DS_Store"
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=pathlib.Path,
                        default=pathlib.Path(__file__).resolve().parents[2])
    parser.add_argument("--source-root", type=pathlib.Path)
    args = parser.parse_args()

    repo = args.repo_root.resolve()
    connect = repo / "rdt_connect"
    resources = connect / "src-tauri/resources"
    source = (args.source_root.resolve() if args.source_root else
              connect / "vendor/rdt-voice-r82-alpha")
    expected_manifest_path = resources / "rdt_voice_web_manifest.json"
    preparer = connect / "scripts/prepare-offline-runtime.py"
    verifier = connect / "scripts/verify-alpha-r82-staging.py"
    checker = connect / "scripts/check-embedded-rdt-voice.mjs"
    preflight = connect / "scripts/prepare-embedded-rdt-voice.mjs"

    for path in (source, expected_manifest_path, preparer, verifier, checker, preflight):
        if not path.exists():
            fail("required input is missing: " + str(path))

    expected_manifest = json.loads(expected_manifest_path.read_text(encoding="utf-8"))
    alpha = expected_manifest.get("alpha_source_files")
    support = expected_manifest.get("connect_support_files")
    if not isinstance(alpha, dict) or len(alpha) != 62:
        fail("expected exactly 62 pinned Alpha source fingerprints")
    if not isinstance(support, dict) or len(support) != 7:
        fail("expected exactly seven pinned Connect support asset fingerprints")

    source_map = {}
    for name, record in alpha.items():
        rel = safe_relative(name)
        if not isinstance(record, str) or len(record) != 64:
            fail("invalid Alpha source fingerprint: " + name)
        src = source / pathlib.Path(*rel.parts)
        if not src.is_file() or src.is_symlink():
            fail("vendored Alpha file is missing or symlinked: " + name)
        if sha256(src) != record:
            fail("vendored Alpha SHA-256 mismatch: " + name)
        source_map[name] = src

    actual_vendor_files = {
        p.relative_to(source).as_posix() for p in source.rglob("*") if p.is_file()
    }
    if actual_vendor_files != set(alpha):
        fail(
            f"vendored Alpha inventory mismatch: expected={len(alpha)} "
            f"actual={len(actual_vendor_files)}"
        )
    locale_count = sum(name.startswith("locales/") and name.endswith(".json") for name in alpha)
    if locale_count != expected_manifest.get("alpha_locale_file_count"):
        fail("pinned Alpha locale count disagrees with manifest")

    root_assets = repo / "assets"
    if not root_assets.is_dir():
        fail("repository-root assets directory is missing")

    work = pathlib.Path(tempfile.mkdtemp(prefix=".rdt-r82-resource-stage-", dir=resources))
    try:
        web = work / "rdt_voice_web"
        web.mkdir()
        notices = work / "notices"
        notices.mkdir()
        staged_manifest = work / "rdt_voice_web_manifest.json"
        shutil.copy2(expected_manifest_path, staged_manifest)

        # Match the established packager order: repository-root assets first,
        # then hash-pinned Alpha files, then independently pinned Connect images.
        shutil.copytree(root_assets, web / "assets", dirs_exist_ok=True)
        for name, src in source_map.items():
            rel = safe_relative(name)
            dst = web.joinpath(*rel.parts)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

        for name, expected_hash in support.items():
            rel = safe_relative(name)
            if not rel.parts or rel.parts[0] != "assets":
                fail("Connect support asset must be under assets/: " + name)
            source_asset = root_assets.joinpath(*rel.parts[1:])
            target_asset = web.joinpath(*rel.parts)
            copy_file_verified(source_asset, target_asset, expected_hash, "Connect support asset")

        print(f"PASS: {len(alpha)} vendored Alpha files hash-verified")
        print(f"PASS: {locale_count} Alpha locale JSON files pinned")
        print(f"PASS: {len(support)} Connect support assets hash-verified")

        run(
            [sys.executable, str(preparer), "--repo-root", str(repo),
             "--web-root", str(web), "--notices", str(notices)],
            cwd=repo,
        )
        run(
            [sys.executable, str(verifier), str(source), str(web),
             str(web / "assets"), str(expected_manifest.get("bundle_version")),
             str(staged_manifest)],
            cwd=repo,
        )

        generated_manifest = json.loads(staged_manifest.read_text(encoding="utf-8"))
        if generated_manifest != expected_manifest:
            fail("generated manifest differs from the checked-in expected manifest")
        actual_files = inventory(web)
        recorded_files = generated_manifest.get("files", {})
        if len(actual_files) != 79 or len(recorded_files) != 79:
            fail(f"expected exactly 79 embedded files, got {len(actual_files)}")
        if actual_files != recorded_files:
            fail("generated web tree does not match all 79 manifest hashes")

        notice_source = notices / "rdt-offline-runtime"
        notice_manifest = notice_source / "bundle-runtime-manifest.json"
        if not notice_manifest.is_file():
            fail("generated offline-runtime third-party notices are incomplete")

        env = os.environ.copy()
        env["RDT_EMBEDDED_WEB_ROOT"] = str(web)
        env["RDT_EMBEDDED_WEB_MANIFEST"] = str(staged_manifest)
        env["RDT_VOICE_WEB_SOURCE"] = str(source)
        run(["node", str(checker)], cwd=connect, env=env)

        staged_notices = work / "shared-third-party-notices"
        staged_notices.mkdir()
        shutil.copytree(notice_source, staged_notices / "rdt-offline-runtime")

        opencc_source = repo / "licenses" / "third-party" / "opencc-js"
        if opencc_source.is_symlink() or not opencc_source.is_dir():
            fail("pinned repository OpenCC-JS license source is missing or unsafe")
        for item in opencc_source.rglob("*"):
            if item.is_symlink():
                fail(f"symlink is not allowed in OpenCC-JS notices: {item}")
        shutil.copytree(opencc_source, staged_notices / "opencc-js")

        build_notices = """# RDT Connect shared build notices

The embedded RDT Voice frontend is generated from the pinned canonical R82 snapshot.
Repository-root Connect assets and hash-pinned support assets are included.
OpenCC-JS license materials are included in opencc-js/.
Offline runtime notices are included in rdt-offline-runtime/.
Platform-specific backend and worker notices are collected separately by each native build.
"""
        (staged_notices / "BUILD-NOTICES.md").write_text(
            build_notices, encoding="utf-8"
        )

        required_shared_notices = (
            staged_notices / "BUILD-NOTICES.md",
            staged_notices / "opencc-js" / "LICENSE",
            staged_notices / "opencc-js" / "NOTICE.md",
            staged_notices / "rdt-offline-runtime" / "bundle-runtime-manifest.json",
        )
        for required in required_shared_notices:
            if not required.is_file() or required.is_symlink() or required.stat().st_size == 0:
                fail(f"generated shared notice file is missing or empty: {required}")

        targets = (
            (web, resources / "rdt_voice_web", work / "previous-web"),
            (
                staged_notices / "rdt-offline-runtime",
                resources / "third_party_notices/rdt-offline-runtime",
                work / "previous-notices",
            ),
            (
                staged_notices / "opencc-js",
                resources / "third_party_notices/opencc-js",
                work / "previous-opencc-notices",
            ),
            (
                staged_notices / "BUILD-NOTICES.md",
                resources / "third_party_notices/BUILD-NOTICES.md",
                work / "previous-build-notices",
            ),
        )
        changed = []
        try:
            for staged, target, backup in targets:
                target.parent.mkdir(parents=True, exist_ok=True)
                existed = target.exists()
                if existed:
                    os.replace(target, backup)
                try:
                    os.replace(staged, target)
                except BaseException:
                    if existed and backup.exists():
                        os.replace(backup, target)
                    raise
                changed.append((target, backup, existed))

            post_env = os.environ.copy()
            post_env["RDT_VOICE_WEB_SOURCE"] = str(source)
            run(["node", str(checker)], cwd=connect, env=post_env)
            run(["node", str(preflight)], cwd=connect, env=post_env)

        except BaseException:
            for target, backup, existed in reversed(changed):
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                elif target.exists() or target.is_symlink():
                    target.unlink()
                if existed and backup.exists():
                    os.replace(backup, target)
            raise

        print("PASS: generated and installed 79 embedded R82 files")
        print("PASS: OpenCC-JS, offline runtime and shared build notices installed")
        print("PASS: manifest unchanged; transactional promotion verified")

    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
