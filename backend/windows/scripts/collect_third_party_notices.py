#!/usr/bin/env python3
"""Stage auditable third-party license material for the frozen Windows backend.

The script intentionally fails closed when any shipped runtime distribution has no
recoverable license/notice file. It never treats package metadata containing only a
license name as a substitute for license text.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Iterable

from packaging.markers import default_environment
from packaging.requirements import Requirement

LICENSE_NAME = re.compile(
    r"^(?:LICENSE|LICENCE|COPYING|NOTICE|COPYRIGHT|THIRD[._ -]?PARTY(?:[._ -]?LICENSES?)?)(?:$|[._ -])",
    re.IGNORECASE,
)
PYTHON_RUNTIME_ROOTS_FILE = Path("backend/windows/runtime-package-roots.txt")
LICENSE_DIR_NAMES = {"license", "licenses", "licence", "licences", "notice", "notices", "third_party_notices", "third-party-notices", "third.party.notices"}


def is_license_material(path: PurePosixPath | Path) -> bool:
    """Match license files by name and text in upstream license/notice folders."""
    posix = PurePosixPath(str(path).replace("\\", "/"))
    if LICENSE_NAME.match(posix.name):
        return True
    return any(part.lower() in LICENSE_DIR_NAMES for part in posix.parts[:-1])


def normalize_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_file(source: Path, destination: Path, *, relative_to: Path | None = None) -> dict[str, str | int]:
    if not source.is_file():
        raise RuntimeError(f"required notice source is not a regular file: {source}")
    if source.stat().st_size == 0:
        raise RuntimeError(f"required notice source is empty: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    recorded_path = destination.relative_to(relative_to).as_posix() if relative_to else destination.name
    return {
        "path": recorded_path,
        "sha256": sha256_file(destination),
        "size_bytes": destination.stat().st_size,
    }


def _license_candidates(files: Iterable[object], distribution: metadata.Distribution, base: Path) -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    for item in files:
        rel = PurePosixPath(str(item))
        if not is_license_material(rel):
            continue
        # Only copy package-owned regular files. RECORD entries should normally be
        # relative to site-packages; resolving/containment protects against bad paths.
        source = Path(distribution.locate_file(str(item))).resolve()
        try:
            rel_source = source.relative_to(base.resolve())
        except ValueError:
            raise RuntimeError(f"distribution license path escapes site-packages: {source}")
        if source.is_file():
            found.append((source, rel_source.as_posix()))
    unique = {(str(path), rel): (path, rel) for path, rel in found}
    return sorted(unique.values(), key=lambda pair: pair[1].lower())


def _distribution_index(site_packages: Path) -> dict[str, metadata.Distribution]:
    if not site_packages.is_dir():
        raise RuntimeError(f"Python site-packages directory is missing: {site_packages}")
    result: dict[str, metadata.Distribution] = {}
    for dist in metadata.distributions(path=[str(site_packages)]):
        name = dist.metadata.get("Name")
        if name:
            normalized = normalize_name(name)
            if normalized in result:
                raise RuntimeError(f"duplicate installed Python distribution metadata: {normalized}")
            result[normalized] = dist
    return result


def _runtime_closure(distributions: dict[str, metadata.Distribution], roots: list[str], marker_environment: dict[str, str] | None = None) -> dict[str, metadata.Distribution]:
    environment = {**default_environment(), **(marker_environment or {})}
    pending: list[tuple[str, frozenset[str]]] = [(normalize_name(root), frozenset()) for root in roots]
    chosen: dict[str, metadata.Distribution] = {}
    visited: set[tuple[str, frozenset[str]]] = set()

    while pending:
        name, requested_extras = pending.pop()
        state = (name, requested_extras)
        if state in visited:
            continue
        visited.add(state)
        dist = distributions.get(name)
        if dist is None:
            raise RuntimeError(f"runtime dependency is not installed in frozen backend environment: {name}")
        chosen[name] = dist
        for raw_req in dist.requires or []:
            try:
                requirement = Requirement(raw_req)
            except Exception as exc:
                raise RuntimeError(f"invalid Requires-Dist metadata in {name}: {raw_req!r}: {exc}") from exc
            if requirement.marker:
                active_extras = requested_extras or frozenset({""})
                if not any(requirement.marker.evaluate({**environment, "extra": extra}) for extra in active_extras):
                    continue
            dep_name = normalize_name(requirement.name)
            pending.append((dep_name, frozenset(requirement.extras)))
    return dict(sorted(chosen.items()))


def _copy_distribution_licenses(
    dist: metadata.Distribution,
    site_packages: Path,
    destination: Path,
    component_category: str,
) -> dict[str, object]:
    name = dist.metadata.get("Name", "unknown")
    version = dist.version
    candidates = _license_candidates(dist.files or [], dist, site_packages)
    if not candidates:
        license_expression = dist.metadata.get("License-Expression") or dist.metadata.get("License") or "unspecified"
        raise RuntimeError(
            f"no license/notice text found for included Python distribution {name}=={version} "
            f"(metadata License={license_expression!r}); refusing to emit an incomplete notice bundle"
        )
    destination_root = destination / "python" / component_category / f"{normalize_name(name)}-{version}"
    copied = []
    for source, relative in candidates:
        # A RECORD path often starts with a package root or dist-info/licenses/.
        # Keep its in-package relative structure but strip leading traversal/components.
        safe_parts = [part for part in PurePosixPath(relative).parts if part not in ("", ".", "..")]
        rel_path = Path(*safe_parts)
        copied.append(_copy_file(source, destination_root / rel_path, relative_to=destination))
    return {
        "name": name,
        "version": version,
        "category": component_category,
        "license_expression": dist.metadata.get("License-Expression") or "",
        "license_metadata": dist.metadata.get("License") or "",
        "files": copied,
    }


def collect_notices(
    repo_root: Path,
    whisper_source: Path,
    ffmpeg_dir: Path,
    site_packages: Path,
    destination: Path,
    runtime_roots_file: Path | None = None,
    marker_environment: dict[str, str] | None = None,
) -> dict[str, object]:
    repo_root = repo_root.resolve()
    whisper_source = whisper_source.resolve()
    ffmpeg_dir = ffmpeg_dir.resolve()
    site_packages = site_packages.resolve()
    destination = destination.resolve()
    runtime_roots_file = runtime_roots_file or (repo_root / PYTHON_RUNTIME_ROOTS_FILE)

    if destination.exists() and any(destination.iterdir()):
        raise RuntimeError(f"notice destination must be empty before collection: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    copied_source_files: list[dict[str, str | int]] = []

    project_license = repo_root / "LICENSE"
    copied_source_files.append(_copy_file(project_license, destination / "RDT-Connect-LICENSE.txt", relative_to=destination))

    opencc_source = repo_root / "licenses" / "third-party" / "opencc-js"
    if not (opencc_source / "NOTICE.md").is_file():
        raise RuntimeError(f"required OpenCC-JS NOTICE.md is missing: {opencc_source}")
    opencc_files = sorted(p for p in opencc_source.rglob("*") if p.is_file())
    required_opencc_names = {"LICENSE", "NOTICE.md", "THIRD_PARTY_LICENSES.md"}
    present_opencc_names = {p.name for p in opencc_files}
    if not required_opencc_names.issubset(present_opencc_names):
        raise RuntimeError(f"OpenCC-JS license/notice set is incomplete; missing {sorted(required_opencc_names - present_opencc_names)}")
    for source in opencc_files:
        copied_source_files.append(_copy_file(source, destination / "opencc-js" / source.relative_to(opencc_source), relative_to=destination))

    engine_pins_path = repo_root / "backend" / "whisper_cpp" / "engine-pins.json"
    try:
        engine_pins = json.loads(engine_pins_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read whisper.cpp pin manifest: {exc}") from exc
    expected_commit = engine_pins.get("source_commit")
    if not expected_commit:
        raise RuntimeError("whisper.cpp pin manifest has no source_commit")
    try:
        actual_commit = subprocess.run(
            ["git", "-C", str(whisper_source), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, timeout=20,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"cannot verify pinned whisper.cpp source commit: {exc}") from exc
    if actual_commit.lower() != str(expected_commit).lower():
        raise RuntimeError(f"whisper.cpp source mismatch: expected {expected_commit}; found {actual_commit}")
    upstream_notice_files = sorted(
        p for p in whisper_source.rglob("*")
        if p.is_file()
        and is_license_material(PurePosixPath(p.relative_to(whisper_source).as_posix()))
        and ".git" not in p.parts
    )
    if not (whisper_source / "LICENSE").is_file():
        raise RuntimeError(f"pinned whisper.cpp checkout is missing root LICENSE: {whisper_source}")
    if not upstream_notice_files:
        raise RuntimeError("no upstream whisper.cpp/GGML license/notice material found")
    for source in upstream_notice_files:
        copied_source_files.append(_copy_file(source, destination / "whisper.cpp" / source.relative_to(whisper_source), relative_to=destination))

    for executable in ("ffmpeg.exe", "ffprobe.exe"):
        if not (ffmpeg_dir / executable).is_file():
            raise RuntimeError(f"required bundled FFmpeg executable is missing: {ffmpeg_dir / executable}")
    ffmpeg_license_files = sorted(
        p for p in ffmpeg_dir.rglob("*")
        if p.is_file() and is_license_material(PurePosixPath(p.relative_to(ffmpeg_dir).as_posix()))
    )
    if not ffmpeg_license_files:
        raise RuntimeError("FFmpeg distribution has no license/notice files; refusing to guess its licensing")
    for source in ffmpeg_license_files:
        copied_source_files.append(_copy_file(source, destination / "ffmpeg" / source.relative_to(ffmpeg_dir), relative_to=destination))

    try:
        license_output = subprocess.run(
            [str(ffmpeg_dir / "ffmpeg.exe"), "-L"], check=True,
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"could not query the bundled FFmpeg executable's license information: {exc}") from exc
    output_text = (license_output.stdout or "") + (license_output.stderr or "")
    if not output_text.strip() or not re.search(r"(?i)(licen[cs]e|LGPL|GPL)", output_text):
        raise RuntimeError("bundled ffmpeg.exe -L did not report recognizable license information")
    license_info = destination / "ffmpeg" / "ffmpeg-license-info.txt"
    license_info.parent.mkdir(parents=True, exist_ok=True)
    license_info.write_text(output_text.strip() + "\n", encoding="utf-8")
    copied_source_files.append({"path": license_info.relative_to(destination).as_posix(), "sha256": sha256_file(license_info), "size_bytes": license_info.stat().st_size})

    roots = [line.strip() for line in runtime_roots_file.read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")]
    distributions = _distribution_index(site_packages)
    runtime = _runtime_closure(distributions, roots, marker_environment)
    python_components: list[dict[str, object]] = []
    for dist in runtime.values():
        python_components.append(_copy_distribution_licenses(dist, site_packages, destination, "runtime"))

    # PyInstaller's bootloader is embedded in the frozen executable, so its license
    # is a shipped-component notice even though PyInstaller itself is build tooling.
    pyinstaller_dist = distributions.get("pyinstaller")
    if pyinstaller_dist is None:
        raise RuntimeError("PyInstaller distribution metadata is missing; cannot include its bootloader license")
    python_components.append(_copy_distribution_licenses(pyinstaller_dist, site_packages, destination, "embedded-build-components"))

    files = []
    for entry in sorted(destination.rglob("*")):
        if entry.is_file() and entry.name not in {"BUILD-NOTICES.md", "NOTICE-MANIFEST.json"}:
            files.append({"path": entry.relative_to(destination).as_posix(), "sha256": sha256_file(entry), "size_bytes": entry.stat().st_size})
    manifest: dict[str, object] = {
        "schema_version": 1,
        "whisper_cpp": {"source_commit": actual_commit, "pinned_commit": expected_commit, "license_notice_files": [item for item in files if item["path"].startswith("whisper.cpp/")]},
        "ffmpeg": {"executables": {name: sha256_file(ffmpeg_dir / name) for name in ("ffmpeg.exe", "ffprobe.exe")}, "license_notice_files": [item for item in files if item["path"].startswith("ffmpeg/")]},
        "source_notice_files": copied_source_files,
        "python_distributions": python_components,
        "python_runtime_root_packages": roots,
        "python_runtime_distribution_count": len(runtime),
        "notice_file_count": len(files),
    }
    (destination / "NOTICE-MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    build_notices = [
        "# Build notices — Windows packaged components",
        "",
        "This notice bundle was generated from the package inputs collected by the Windows packaging script.",
        "",
        "- **RDT Connect:** `RDT-Connect-LICENSE.txt` contains the repository license.",
        "- **OpenCC-JS:** `opencc-js/` contains the checked-in notice and complete checked-in license files.",
        f"- **whisper.cpp / GGML:** `whisper.cpp/` contains matching license/notice files copied from source commit `{actual_commit}` after pin verification.",
        "- **FFmpeg:** `ffmpeg/` contains every license/notice-named file found alongside the exact staged `ffmpeg.exe` and `ffprobe.exe`, plus `ffmpeg-license-info.txt` captured from `ffmpeg.exe -L`.",
        f"- **Python runtime dependencies:** `python/runtime/` contains license/notice texts from the installed transitive dependency closure of: {', '.join(roots)}.",
        "- **PyInstaller bootloader:** `python/embedded-build-components/` contains the PyInstaller distribution's license material because its bootloader is embedded in the frozen executable.",
        "",
        f"The machine-readable `NOTICE-MANIFEST.json` records {len(files)} staged notice/input files, SHA-256 digests, Python package versions and license metadata. It is evidence of collected materials, not a legal opinion on compatibility or a substitute for release-owner license review.",
        "",
    ]
    (destination / "BUILD-NOTICES.md").write_text("\n".join(build_notices), encoding="utf-8")

    # Final staged-content assertions: every required class must actually appear in
    # the output tree, with at least one Python runtime distribution represented.
    required_paths = [
        destination / "RDT-Connect-LICENSE.txt",
        destination / "opencc-js" / "NOTICE.md",
        destination / "whisper.cpp" / "LICENSE",
        destination / "ffmpeg" / "ffmpeg-license-info.txt",
        destination / "BUILD-NOTICES.md",
        destination / "NOTICE-MANIFEST.json",
    ]
    missing = [str(path.relative_to(destination)) for path in required_paths if not path.is_file() or path.stat().st_size == 0]
    if missing:
        raise RuntimeError("staged third-party notice verification failed; missing/empty: " + ", ".join(missing))
    if not any((destination / "python" / "runtime").rglob("*")):
        raise RuntimeError("no Python runtime license files were staged")
    if not any((destination / "python" / "embedded-build-components").rglob("*")):
        raise RuntimeError("PyInstaller bootloader license material was not staged")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--whisper-source", type=Path, required=True)
    parser.add_argument("--ffmpeg-dir", type=Path, required=True)
    parser.add_argument("--site-packages", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--runtime-roots-file", type=Path)
    args = parser.parse_args()
    try:
        result = collect_notices(args.repo_root, args.whisper_source, args.ffmpeg_dir, args.site_packages, args.destination, args.runtime_roots_file)
        print(f"NOTICE_MANIFEST={args.destination / 'NOTICE-MANIFEST.json'}")
        print(f"NOTICE_FILE_COUNT={result['notice_file_count']}")
        print(f"PYTHON_RUNTIME_DISTRIBUTION_COUNT={result['python_runtime_distribution_count']}")
        print(f"WHISPER_CPP_COMMIT={result['whisper_cpp']['source_commit']}")
        print("THIRD_PARTY_NOTICE_COLLECTION=PASS")
        return 0
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"THIRD_PARTY_NOTICE_COLLECTION_FAILED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
