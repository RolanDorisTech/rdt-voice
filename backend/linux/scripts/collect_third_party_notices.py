#!/usr/bin/env python3
"""Collect and verify license material for the Linux packaged components."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import re
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path, PurePosixPath
from typing import Iterable


LICENSE_NAME = re.compile(
    r"^(?:LICENSE|LICENCE|COPYING|NOTICE|COPYRIGHT|THIRD[._ -]?PARTY(?:[._ -]?LICENSES?)?)(?:$|[._ -])",
    re.IGNORECASE,
)
LICENSE_DIR_NAMES = {
    "license", "licenses", "licence", "licences", "notice", "notices",
    "third_party_notices", "third-party-notices", "third.party.notices",
}
RUNTIME_ROOTS_FILE = Path("backend/linux/runtime-package-roots.txt")
WHISPER_PIN_FILE = Path("backend/whisper_cpp/engine-pins.json")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def safe_relative(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise RuntimeError(f"unsafe notice path: {value!r}")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or relative.as_posix() != value
        or value in {"", "."}
    ):
        raise RuntimeError(f"unsafe notice path: {value!r}")
    return relative


def copy_checked(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise RuntimeError(f"notice source is not a regular file: {source}")
    if source.stat().st_size == 0:
        raise RuntimeError(f"notice source is empty: {source}")
    if destination.is_symlink():
        raise RuntimeError(f"notice destination is a symlink: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if not destination.is_file() or sha256(source) != sha256(destination):
            raise RuntimeError(f"conflicting existing notice file: {destination}")
        return
    shutil.copy2(source, destination)


def is_license_material(path: PurePosixPath) -> bool:
    if LICENSE_NAME.match(path.name):
        return True
    return any(part.lower() in LICENSE_DIR_NAMES for part in path.parts[:-1])


def _append_notice_section(destination: Path, heading: str, body: str) -> None:
    path = destination / "BUILD-NOTICES.md"
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("Linux BUILD-NOTICES.md is missing or unsafe")
    text = path.read_text(encoding="utf-8")
    if heading not in text:
        path.write_text(text.rstrip() + "\n\n" + heading + "\n\n" + body.strip() + "\n", encoding="utf-8")


def _load_manifest(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"notice manifest is missing or unsafe: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"notice manifest is invalid: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"notice manifest root is not an object: {path}")
    return value


def _copy_opencc(repo_root: Path, destination: Path) -> None:
    opencc = repo_root / "licenses" / "third-party" / "opencc-js"
    required = ("LICENSE", "NOTICE.md", "THIRD_PARTY_LICENSES.md")
    missing = [name for name in required if not (opencc / name).is_file()]
    if missing:
        raise RuntimeError(f"OpenCC-JS notice inputs are incomplete: {missing}")
    for source in sorted(opencc.rglob("*")):
        if source.is_symlink():
            raise RuntimeError(f"OpenCC-JS notice tree contains a symlink: {source}")
        if not source.is_file():
            continue
        relative = PurePosixPath(source.relative_to(opencc).as_posix())
        safe_relative(relative.as_posix())
        copy_checked(source, destination / "opencc-js" / Path(*relative.parts))


def _collect_whisper_notices(
    repo_root: Path,
    whisper_source: Path,
    destination: Path,
) -> dict:
    source = whisper_source.resolve(strict=True)
    if not source.is_dir() or whisper_source.is_symlink():
        raise RuntimeError(f"pinned whisper.cpp source directory is missing or unsafe: {whisper_source}")

    pins_path = repo_root / WHISPER_PIN_FILE
    pins = _load_manifest(pins_path)
    expected_commit = pins.get("source_commit")
    if not isinstance(expected_commit, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", expected_commit):
        raise RuntimeError("whisper.cpp pin manifest has no valid 40-character source_commit")

    try:
        actual_commit = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"cannot verify pinned whisper.cpp source commit: {exc}") from exc

    if actual_commit.lower() != expected_commit.lower():
        raise RuntimeError(
            f"whisper.cpp source mismatch: expected {expected_commit}; found {actual_commit}"
        )

    found: list[tuple[Path, PurePosixPath]] = []
    for path in sorted(source.rglob("*")):
        relative_parts = path.relative_to(source).parts
        if ".git" in relative_parts:
            continue
        if path.is_symlink():
            relative = PurePosixPath(path.relative_to(source).as_posix())
            if is_license_material(relative):
                raise RuntimeError(f"pinned whisper.cpp license material is a symlink: {path}")
            continue
        if not path.is_file():
            continue
        relative = PurePosixPath(path.relative_to(source).as_posix())
        if is_license_material(relative):
            safe_relative(relative.as_posix())
            found.append((path, relative))

    required = {"LICENSE", "ggml/LICENSE"}
    present = {relative.as_posix() for _, relative in found}
    missing = sorted(required - present)
    if missing:
        raise RuntimeError(f"pinned whisper.cpp/GGML license files are missing: {missing}")

    copied = []
    for path, relative in found:
        target = destination / "whisper.cpp" / Path(*relative.parts)
        copy_checked(path, target)
        copied.append({
            "path": target.relative_to(destination).as_posix(),
            "size_bytes": target.stat().st_size,
            "sha256": sha256(target),
        })

    _append_notice_section(
        destination,
        "## whisper.cpp and GGML",
        (
            f"The `whisper.cpp/` directory contains upstream license/notice files "
            f"collected from the verified source checkout at commit `{actual_commit}`. "
            "The root Whisper license and GGML license are both required. "
            "The worker source pin is recorded in `backend/whisper_cpp/engine-pins.json`."
        ),
    )
    return {
        "source_commit": actual_commit,
        "pinned_commit": expected_commit,
        "license_notice_files": copied,
    }


def _copy_ffmpeg_notice_tree(source_root: Path, destination_root: Path) -> None:
    if source_root.is_symlink() or not source_root.is_dir():
        raise RuntimeError(f"FFmpeg notice directory is missing or unsafe: {source_root}")
    source_resolved = source_root.resolve(strict=True)
    destination_root.mkdir(parents=True, exist_ok=True)
    destination_resolved = destination_root.resolve(strict=True)
    if source_resolved == destination_resolved:
        return
    if any(destination_root.iterdir()):
        raise RuntimeError(f"FFmpeg notice destination is not empty: {destination_root}")

    for source in sorted(source_root.rglob("*")):
        if source.is_symlink():
            raise RuntimeError(f"FFmpeg notice tree contains a symlink: {source}")
        if not source.is_file():
            continue
        relative = PurePosixPath(source.relative_to(source_root).as_posix())
        safe_relative(relative.as_posix())
        copy_checked(source, destination_root / Path(*relative.parts))


def _collect_ffmpeg_notices(ffmpeg_notices: Path, destination: Path) -> dict:
    target = destination / "ffmpeg"
    _copy_ffmpeg_notice_tree(ffmpeg_notices, target)

    manifest_path = target / "FFMPEG-BUILD-MANIFEST.json"
    build = _load_manifest(manifest_path)
    if (
        build.get("schema") != "rdt-linux-ffmpeg-bundle-v1"
        or build.get("platform") != "linux"
        or build.get("version") != "9.1"
        or build.get("license") != "LGPL-2.1-or-later"
        or build.get("gpl_enabled") is not False
        or build.get("nonfree_enabled") is not False
        or build.get("dynamic_linking") is not True
    ):
        raise RuntimeError("FFmpeg build manifest does not prove the expected LGPL-only Linux build")

    source = build.get("source")
    if not isinstance(source, dict):
        raise RuntimeError("FFmpeg build manifest source evidence is missing")
    archive_name = source.get("source_archive")
    archive_relative = safe_relative(archive_name)
    archive = target / Path(*archive_relative.parts)
    signature = target / Path(*(archive_relative.parts[:-1] + (archive_relative.name + ".asc",)))
    key_file = target / "source" / "ffmpeg-devel.asc"

    required = [
        target / "COPYING.LGPLv2.1",
        target / "FFMPEG-LICENSE-INFO.txt",
        target / "FFMPEG-VERSION.txt",
        target / "FFMPEG-BUILD-CONFIGURATION.txt",
        archive,
        signature,
        key_file,
    ]
    missing = [str(path.relative_to(target)) for path in required if path.is_symlink() or not path.is_file() or path.stat().st_size == 0]
    if missing:
        raise RuntimeError(f"FFmpeg license/source evidence is incomplete: {missing}")

    license_text = (target / "FFMPEG-LICENSE-INFO.txt").read_text(encoding="utf-8", errors="replace")
    if not re.search(r"(?i)(license|LGPL)", license_text):
        raise RuntimeError("FFmpeg -L evidence contains no recognizable license information")

    if source.get("sha256") != sha256(archive):
        raise RuntimeError("FFmpeg source archive SHA-256 differs from its build manifest")
    if source.get("signature_sha256") != sha256(signature):
        raise RuntimeError("FFmpeg detached signature SHA-256 differs from its build manifest")

    records = build.get("notice_files")
    if not isinstance(records, list) or not records:
        raise RuntimeError("FFmpeg build manifest notice_files inventory is missing")

    expected: dict[str, dict] = {}
    for item in records:
        if not isinstance(item, dict):
            raise RuntimeError("FFmpeg notice inventory contains a malformed record")
        relative = safe_relative(item.get("path"))
        name = relative.as_posix()
        size = item.get("size_bytes")
        digest = item.get("sha256")
        if (
            name in expected
            or type(size) is not int
            or size <= 0
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise RuntimeError("FFmpeg notice inventory contains a duplicate or invalid record")
        path = target / Path(*relative.parts)
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"FFmpeg notice inventory file is missing or unsafe: {name}")
        if path.stat().st_size != size or sha256(path) != digest:
            raise RuntimeError(f"FFmpeg notice inventory hash/size mismatch: {name}")
        expected[name] = {"size_bytes": size, "sha256": digest}

    actual_names = {
        path.relative_to(target).as_posix()
        for path in target.rglob("*")
        if path.is_file() and not path.is_symlink() and path != manifest_path
    }
    if actual_names != set(expected):
        raise RuntimeError("FFmpeg notice directory differs from its build-manifest file inventory")

    source_size = archive.stat().st_size
    _append_notice_section(
        destination,
        "## FFmpeg — Linux bundled runtime",
        (
            "The `ffmpeg/` directory contains the verified FFmpeg 9.1 source archive, "
            "detached signature, signing-key material, LGPL-2.1 license text, `-L` "
            "license output, build configuration, and hashed build manifest. "
            "The manifest records LGPL-2.1-or-later, dynamic linking, and GPL/nonfree "
            "features disabled."
        ),
    )
    return {
        "manifest": "ffmpeg/FFMPEG-BUILD-MANIFEST.json",
        "manifest_sha256": sha256(manifest_path),
        "version": build["version"],
        "architecture": build.get("architecture", ""),
        "license": build["license"],
        "gpl_enabled": build["gpl_enabled"],
        "nonfree_enabled": build["nonfree_enabled"],
        "runtime_size_bytes": build.get("runtime_size_bytes", 0),
        "source_size_bytes": source_size,
    }


def _distribution_index(site_packages: Path) -> dict[str, metadata.Distribution]:
    if not site_packages.is_dir():
        raise RuntimeError(f"Python site-packages directory is missing: {site_packages}")
    result: dict[str, metadata.Distribution] = {}
    for dist in metadata.distributions(path=[str(site_packages)]):
        name = dist.metadata.get("Name")
        if not name:
            continue
        normalized = normalize_name(name)
        if normalized in result:
            raise RuntimeError(f"duplicate installed Python distribution metadata: {normalized}")
        result[normalized] = dist
    return result


def _runtime_closure(
    distributions: dict[str, metadata.Distribution],
    roots: list[str],
    marker_environment: dict[str, str] | None = None,
) -> dict[str, metadata.Distribution]:
    try:
        from packaging.markers import default_environment
        from packaging.requirements import Requirement
    except ImportError as exc:
        raise RuntimeError("the package environment must provide packaging to resolve Python runtime dependencies") from exc
    environment = {**default_environment(), **(marker_environment or {})}
    pending: list[tuple[str, frozenset[str]]] = [
        (normalize_name(root), frozenset()) for root in roots
    ]
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
            raise RuntimeError(
                f"Linux Python runtime dependency is not installed in the package environment: {name}"
            )
        chosen[name] = dist

        for raw_requirement in dist.requires or []:
            try:
                requirement = Requirement(raw_requirement)
            except Exception as exc:
                raise RuntimeError(
                    f"invalid Requires-Dist metadata in {name}: {raw_requirement!r}: {exc}"
                ) from exc
            if requirement.marker:
                active_extras = requested_extras or frozenset({""})
                if not any(
                    requirement.marker.evaluate({**environment, "extra": extra})
                    for extra in active_extras
                ):
                    continue
            pending.append((
                normalize_name(requirement.name),
                frozenset(requirement.extras),
            ))
    return dict(sorted(chosen.items()))


def _license_candidates(
    dist: metadata.Distribution,
    site_packages: Path,
) -> list[tuple[Path, str]]:
    found: dict[tuple[str, str], tuple[Path, str]] = {}
    for item in dist.files or []:
        relative = PurePosixPath(str(item))
        if not is_license_material(relative):
            continue
        safe_relative(relative.as_posix())
        candidate = Path(dist.locate_file(str(item)))
        if candidate.is_symlink():
            raise RuntimeError(f"Python distribution license material is a symlink: {candidate}")
        try:
            source = candidate.resolve(strict=True)
            stored_relative = source.relative_to(site_packages.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"Python distribution license path escapes site-packages or is missing: {candidate}"
            ) from exc
        if source.is_file() and source.stat().st_size:
            # Preserve the path recorded by distribution metadata, not the
            # site-packages parent directory, in the output notice tree.
            metadata_relative = relative.as_posix()
            found[(str(source), metadata_relative)] = (source, metadata_relative)
    return sorted(found.values(), key=lambda value: value[1].lower())


def _copy_distribution_licenses(
    dist: metadata.Distribution,
    site_packages: Path,
    destination: Path,
    category: str,
) -> dict:
    name = dist.metadata.get("Name", "unknown")
    version = dist.version
    candidates = _license_candidates(dist, site_packages)
    if not candidates:
        license_expression = (
            dist.metadata.get("License-Expression")
            or dist.metadata.get("License")
            or "unspecified"
        )
        raise RuntimeError(
            f"no license/notice text found for included Python distribution "
            f"{name}=={version} (metadata License={license_expression!r}); "
            "refusing to emit an incomplete Linux notice bundle"
        )

    root = destination / "python" / category / f"{normalize_name(name)}-{version}"
    copied = []
    for source, relative in candidates:
        safe = safe_relative(relative)
        target = root / Path(*safe.parts)
        copy_checked(source, target)
        copied.append({
            "path": target.relative_to(destination).as_posix(),
            "size_bytes": target.stat().st_size,
            "sha256": sha256(target),
        })

    return {
        "name": name,
        "version": version,
        "category": category,
        "license_expression": dist.metadata.get("License-Expression") or "",
        "license_metadata": dist.metadata.get("License") or "",
        "files": copied,
    }


def _collect_cpython_license(
    destination: Path,
    base_prefix: Path | None = None,
) -> dict:
    if sys.implementation.name.lower() != "cpython":
        raise RuntimeError(
            f"unsupported packaged Python implementation: {sys.implementation.name}"
        )
    base = (base_prefix or Path(sys.base_prefix)).resolve(strict=True)
    version = ".".join(str(part) for part in sys.version_info[:3])
    candidates = [
        base / "LICENSE",
        base / "LICENSE.txt",
        base / "LICENSE.md",
    ]

    stdlib_value = sysconfig.get_paths().get("stdlib")
    if stdlib_value:
        stdlib = Path(stdlib_value)
        try:
            stdlib_root = stdlib.resolve(strict=True)
            stdlib_root.relative_to(base)
        except (OSError, ValueError):
            pass
        else:
            candidates.extend([
                stdlib_root / "LICENSE",
                stdlib_root / "LICENSE.txt",
                stdlib_root / "LICENSE.md",
            ])

    versioned_docs = base / "share" / "doc" / (
        f"python{sys.version_info.major}.{sys.version_info.minor}"
    )
    candidates.append(versioned_docs / "copyright")

    source = None
    for candidate in candidates:
        if candidate.is_symlink():
            raise RuntimeError(f"CPython license candidate is a symlink: {candidate}")
        if not candidate.is_file() or candidate.stat().st_size == 0:
            continue
        resolved = candidate.resolve(strict=True)
        try:
            resolved.relative_to(base)
        except ValueError as exc:
            raise RuntimeError(
                f"CPython license path escapes interpreter prefix: {candidate}"
            ) from exc
        source = resolved
        break

    if source is None:
        raise RuntimeError(
            "CPython license text was not found under the active interpreter prefix"
        )

    target = destination / "python" / "cpython" / "LICENSE"
    copy_checked(source, target)
    return {
        "implementation": sys.implementation.name,
        "version": version,
        "license_file": target.relative_to(destination).as_posix(),
        "size_bytes": target.stat().st_size,
        "sha256": sha256(target),
    }


def _collect_python_notices(
    repo_root: Path,
    destination: Path,
    runtime_roots_file: Path | None,
    site_packages: Path | None,
    marker_environment: dict[str, str] | None = None,
    python_base_prefix: Path | None = None,
) -> dict:
    roots_path = runtime_roots_file or (repo_root / RUNTIME_ROOTS_FILE)
    if roots_path.is_symlink() or not roots_path.is_file():
        raise RuntimeError(f"Linux Python runtime roots file is missing or unsafe: {roots_path}")
    roots = [
        line.strip()
        for line in roots_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not roots:
        raise RuntimeError("Linux Python runtime roots file contains no package roots")

    if site_packages is None:
        purelib = sysconfig.get_paths().get("purelib")
        if not purelib:
            raise RuntimeError("cannot determine package Python site-packages directory")
        site_packages = Path(purelib)
    site_packages = site_packages.resolve(strict=True)

    distributions = _distribution_index(site_packages)
    runtime = _runtime_closure(distributions, roots, marker_environment)
    components = [
        _copy_distribution_licenses(dist, site_packages, destination, "runtime")
        for dist in runtime.values()
    ]

    pyinstaller = distributions.get("pyinstaller")
    if pyinstaller is None:
        raise RuntimeError(
            "PyInstaller distribution metadata is missing; cannot include its embedded bootloader license"
        )
    pyinstaller_component = _copy_distribution_licenses(
        pyinstaller,
        site_packages,
        destination,
        "embedded-build-components",
    )
    components.append(pyinstaller_component)
    cpython_component = _collect_cpython_license(destination, python_base_prefix)
    _append_notice_section(
        destination,
        "## CPython interpreter",
        "The `python/cpython/LICENSE` file contains the license text for the CPython runtime used to build the frozen backend.",
    )

    _append_notice_section(
        destination,
        "## Python runtime and PyInstaller bootloader",
        (
            "The `python/runtime/` directory contains license/notice text for the "
            "installed transitive dependency closure of the Linux package roots: "
            + ", ".join(roots)
            + ". The `python/embedded-build-components/` directory contains the "
            "PyInstaller distribution license for its bootloader embedded in the "
            "frozen backend. License-name metadata alone is not treated as a substitute "
            "for the copied license text."
        ),
    )
    return {
        "components": components,
        "runtime_roots": roots,
        "runtime_distribution_count": len(runtime),
        "cpython": cpython_component,
    }


def collect(
    repo_root: Path,
    destination: Path,
    *,
    whisper_source: Path | None = None,
    ffmpeg_notices: Path | None = None,
    include_python_runtime: bool = False,
    runtime_roots_file: Path | None = None,
    site_packages: Path | None = None,
    marker_environment: dict[str, str] | None = None,
    python_base_prefix: Path | None = None,
) -> dict:
    repo_root = repo_root.resolve(strict=True)
    if destination.is_symlink():
        raise RuntimeError(f"notice destination must not be a symlink: {destination}")
    destination.mkdir(parents=True, exist_ok=True)

    root_license = repo_root / "LICENSE"
    history = repo_root / "LICENSE_HISTORY.md"
    if root_license.is_symlink() or not root_license.is_file():
        raise RuntimeError(f"repository-root LICENSE is missing or unsafe: {root_license}")
    copy_checked(root_license, destination / "Repository-LICENSE.txt")
    if history.is_file():
        copy_checked(history, destination / "LICENSE-HISTORY.txt")
    _copy_opencc(repo_root, destination)

    build_notices = destination / "BUILD-NOTICES.md"
    base_sections = [
        "# Third-party notices — Linux packaged components",
        "",
        "- **Repository source license:** `Repository-LICENSE.txt` is an unchanged copy of the repository-root `LICENSE`. It is not a separately asserted RDT Connect license grant.",
        "- **License history:** `LICENSE-HISTORY.txt` is included when present to preserve the repository's license-history context.",
        "- **OpenCC-JS:** `opencc-js/` contains checked-in upstream license and notice files, including bundled dictionary-data license material.",
        "- **CUDA:** CUDA Toolkit runtime notices and license materials are added only when a CUDA worker is staged. NVIDIA driver libraries are not redistributed.",
        "",
        "The machine-readable `NOTICE-MANIFEST.json` records staged file paths, byte sizes, and SHA-256 digests. It is an inventory of collected materials, not a legal opinion.",
        "",
    ]
    if build_notices.exists():
        if build_notices.is_symlink() or not build_notices.is_file():
            raise RuntimeError("existing Linux BUILD-NOTICES.md is not a regular file")
        existing_text = build_notices.read_text(encoding="utf-8")
        if not existing_text.startswith("# Third-party notices — Linux packaged components"):
            raise RuntimeError(f"unexpected existing Linux BUILD-NOTICES.md: {build_notices}")
    else:
        build_notices.write_text("\n".join(base_sections), encoding="utf-8")

    manifest_path = destination / "NOTICE-MANIFEST.json"
    if manifest_path.exists():
        manifest = _load_manifest(manifest_path)
    else:
        manifest = {"schema": "rdt-third-party-notices-v1"}
    if manifest.get("schema") != "rdt-third-party-notices-v1":
        raise RuntimeError("Linux NOTICE-MANIFEST.json schema is unsupported")

    manifest["base_notice_sources"] = [
        "repository-root:LICENSE",
        "repository-root:LICENSE_HISTORY.md" if history.is_file() else None,
        "licenses/third-party/opencc-js",
    ]
    manifest["base_notice_sources"] = [
        item for item in manifest["base_notice_sources"] if item is not None
    ]

    if whisper_source is not None:
        manifest["whisper_cpp"] = _collect_whisper_notices(
            repo_root, whisper_source, destination
        )

    if ffmpeg_notices is not None:
        manifest["ffmpeg"] = _collect_ffmpeg_notices(ffmpeg_notices, destination)

    if include_python_runtime:
        python_data = _collect_python_notices(
            repo_root,
            destination,
            runtime_roots_file,
            site_packages,
            marker_environment,
            python_base_prefix,
        )
        manifest["cpython_runtime"] = python_data["cpython"]
        manifest["python_distributions"] = python_data["components"]
        manifest["python_runtime_root_packages"] = python_data["runtime_roots"]
        manifest["python_runtime_distribution_count"] = python_data["runtime_distribution_count"]

    files = []
    for path in sorted(destination.rglob("*")):
        if path.is_symlink():
            raise RuntimeError(f"Linux notice output contains a symlink: {path}")
        if not path.is_file() or path.name in {
            "NOTICE-MANIFEST.json",
            "NOTICE-MANIFEST.json.tmp",
            "BUILD-NOTICES.md",
        }:
            continue
        files.append({
            "path": path.relative_to(destination).as_posix(),
            "size_bytes": path.stat().st_size,
            "sha256": sha256(path),
        })
    manifest["files"] = files
    manifest["notice_file_count"] = len(files)

    temporary = manifest_path.with_name("NOTICE-MANIFEST.json.tmp")
    if temporary.exists():
        raise RuntimeError(f"refusing to overwrite existing temporary manifest: {temporary}")
    temporary.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(manifest_path)

    required = [
        destination / "Repository-LICENSE.txt",
        destination / "opencc-js" / "LICENSE",
        destination / "opencc-js" / "NOTICE.md",
        destination / "opencc-js" / "THIRD_PARTY_LICENSES.md",
        destination / "BUILD-NOTICES.md",
        manifest_path,
    ]
    if whisper_source is not None:
        required.extend([
            destination / "whisper.cpp" / "LICENSE",
            destination / "whisper.cpp" / "ggml" / "LICENSE",
        ])
    if ffmpeg_notices is not None:
        required.extend([
            destination / "ffmpeg" / "COPYING.LGPLv2.1",
            destination / "ffmpeg" / "FFMPEG-LICENSE-INFO.txt",
            destination / "ffmpeg" / "FFMPEG-BUILD-MANIFEST.json",
        ])
    if include_python_runtime:
        required.extend([
            destination / "python" / "embedded-build-components",
        ])
    missing_outputs = []
    for path in required:
        if path.is_dir() and not path.is_symlink():
            if not any(item.is_file() for item in path.rglob("*")):
                missing_outputs.append(str(path.relative_to(destination)))
        elif path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            missing_outputs.append(str(path.relative_to(destination)))
    if missing_outputs:
        raise RuntimeError(f"Linux notice collection incomplete: {missing_outputs}")

    if include_python_runtime and not any(
        (destination / "python" / "runtime").rglob("*")
    ):
        raise RuntimeError("no Python runtime license files were staged")

    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--whisper-source", type=Path)
    parser.add_argument("--ffmpeg-notices", type=Path)
    parser.add_argument("--include-python-runtime", action="store_true")
    parser.add_argument("--runtime-roots-file", type=Path)
    args = parser.parse_args()
    try:
        manifest = collect(
            args.repo_root,
            args.destination,
            whisper_source=args.whisper_source,
            ffmpeg_notices=args.ffmpeg_notices,
            include_python_runtime=args.include_python_runtime,
            runtime_roots_file=args.runtime_roots_file,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"LINUX_THIRD_PARTY_NOTICE_COLLECTION_FAILED: {exc}", file=sys.stderr)
        return 2

    print(f"LINUX_THIRD_PARTY_NOTICES={args.destination}")
    print(f"LINUX_THIRD_PARTY_NOTICE_FILE_COUNT={manifest['notice_file_count']}")
    print(f"LINUX_THIRD_PARTY_WHISPER_CPP={'PASS' if 'whisper_cpp' in manifest else 'NOT_REQUESTED'}")
    print(f"LINUX_THIRD_PARTY_FFMPEG={'PASS' if 'ffmpeg' in manifest else 'NOT_REQUESTED'}")
    print(f"LINUX_THIRD_PARTY_PYTHON_RUNTIME={'PASS' if 'python_distributions' in manifest else 'NOT_REQUESTED'}")
    print("LINUX_THIRD_PARTY_NOTICE_COLLECTION=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
