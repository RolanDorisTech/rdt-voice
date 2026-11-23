#!/usr/bin/env python3
"""Stage only toolkit-owned CUDA runtime dependencies for packaged workers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

DRIVER_LIBRARIES = {
    "libcuda.so", "libcuda.so.1", "nvcuda.dll",
    "nvapi.dll", "nvapi64.dll", "nvml.dll",
}

CUDA_RUNTIME_PREFIX = re.compile(
    r"^(?:lib)?(?:cudart|cublas|cusparse|cusolver|curand|cufft|"
    r"nvrtc|nvrtc-builtins|nvJitLink|nvfatbin|nvptxcompiler|"
    r"nvvm|nvToolsExt|cupti|cuinj)",
    re.IGNORECASE,
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def is_driver(name: str) -> bool:
    lowered = name.lower()
    return (
        lowered in DRIVER_LIBRARIES
        or lowered.startswith("libnvidia-")
        or lowered.startswith("libnvidia_")
    )


def license_sources(cuda_root: Path) -> list[tuple[Path, Path]]:
    roots: list[tuple[Path, str, bool]] = [
        (cuda_root.resolve(), "toolkit", True)
    ]

    configured = os.environ.get("CUDA_LICENSE_DIR", "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_dir():
            roots.append((candidate.resolve(), "configured", False))

    if sys.platform.startswith("linux"):
        patterns = (
            "cuda*", "nvidia-cuda*", "libcublas*", "libcudart*",
            "libcusparse*", "libcusolver*", "libcurand*", "libcufft*",
            "libnvrtc*",
        )
        for base in (Path("/usr/share/doc"), Path("/usr/local/share/doc")):
            if base.is_dir():
                for pattern in patterns:
                    roots.extend(
                        (item.resolve(), "system-docs", False)
                        for item in base.glob(pattern)
                        if item.is_dir()
                    )

    found: dict[str, tuple[Path, Path]] = {}
    for root, namespace, is_toolkit in roots:
        scan_roots = (
            [root / name for name in ("doc", "docs", "share/doc")]
            if is_toolkit else [root]
        )
        for scan_root in scan_roots:
            if not scan_root.is_dir():
                continue
            for path in scan_root.rglob("*"):
                if not path.is_file():
                    continue
                name = path.name.lower()
                if not (
                    any(token in name for token in ("eula", "license", "licence", "notice"))
                    or name == "copyright"
                ):
                    continue
                resolved = path.resolve()
                if not inside(resolved, root):
                    continue
                relative = (
                    resolved.relative_to(root)
                    if is_toolkit
                    else Path("external") / namespace / root.name / resolved.relative_to(root)
                )
                found[str(resolved)] = (resolved, relative)

    return sorted(found.values(), key=lambda item: str(item[1]).lower())


def copy_unique(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if sha256(target) != sha256(source):
            raise RuntimeError(f"runtime filename collision: {target.name}")
        return
    shutil.copy2(source, target)


def stage_licenses(cuda_root: Path, destination: Path) -> list[str]:
    sources = license_sources(cuda_root)
    if not sources:
        raise RuntimeError(
            "CUDA Toolkit EULA/license material was not found; refusing incomplete runtime bundle"
        )
    staged = []
    for source, relative in sources:
        target = destination / "licenses" / relative
        copy_unique(source, target)
        staged.append(target.relative_to(destination).as_posix())
    return staged


def write_manifest(
    destination: Path,
    platform_name: str,
    dependencies: list[str],
    licenses: list[str],
) -> Path:
    files = {}
    for path in sorted(p for p in destination.rglob("*") if p.is_file()):
        relative = path.relative_to(destination).as_posix()
        files[relative] = {"bytes": path.stat().st_size, "sha256": sha256(path)}
    manifest = {
        "schema": "rdt-cuda-runtime-bundle-v1",
        "platform": platform_name,
        "dependencies": sorted(set(dependencies), key=str.lower),
        "licenses": licenses,
        "excluded_driver_libraries": sorted(DRIVER_LIBRARIES),
        "files": files,
    }
    target = destination / "cuda-runtime-manifest.json"
    target.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return target


def linux_library_dirs(cuda_root: Path, destination: Path) -> list[Path]:
    dirs = [destination / "does-not-exist"]
    for candidate in (
        cuda_root / "lib64",
        cuda_root / "targets" / "x86_64-linux" / "lib",
        cuda_root / "targets" / "aarch64-linux" / "lib",
        cuda_root / "targets" / "sbsa-linux" / "lib",
    ):
        if candidate.is_dir() and "stubs" not in {part.lower() for part in candidate.parts}:
            dirs.append(candidate)
    targets = cuda_root / "targets"
    if targets.is_dir():
        dirs.extend(
            path for path in targets.glob("*/lib")
            if path.is_dir() and "stubs" not in {part.lower() for part in path.parts}
        )
    return list(dict.fromkeys(dirs))


def parse_ldd(output: str) -> list[tuple[str, str]]:
    unresolved = []
    found = []
    for line in output.splitlines():
        missing = re.match(r"^\s*(\S+)\s+=>\s+not found", line)
        if missing:
            if not is_driver(missing.group(1)):
                unresolved.append(missing.group(1))
            continue
        match = re.match(r"^\s*(\S+)\s+=>\s+(/[^\s]+)", line)
        if match:
            found.append((match.group(1), match.group(2)))
            continue
        direct = re.match(r"^\s*(/[^ ]+)\s+\(0x[0-9a-fA-F]+\)", line)
        if direct:
            path = Path(direct.group(1))
            found.append((path.name, str(path)))
    if unresolved:
        raise RuntimeError("unresolved worker dependencies: " + ", ".join(sorted(set(unresolved))))
    return found


def stage_linux(binaries: list[Path], cuda_root: Path, destination: Path) -> Path:
    cuda_root = cuda_root.resolve(strict=True)
    destination.mkdir(parents=True, exist_ok=False)
    search_dirs = linux_library_dirs(cuda_root, destination)
    env = os.environ.copy()
    old_path = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = os.pathsep.join(
        [str(destination), *(str(p) for p in search_dirs if p.exists()), old_path]
    ).strip(os.pathsep)

    dependencies = []
    for binary in binaries:
        binary = binary.resolve(strict=True)
        result = subprocess.run(
            ["ldd", str(binary)],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        if result.returncode != 0 and "not a dynamic executable" not in output.lower():
            raise RuntimeError(f"ldd failed for {binary.name}: {output.strip()}")

        for name, raw_path in parse_ldd(output):
            if is_driver(name):
                continue
            source = Path(raw_path)
            if not source.is_file():
                continue
            resolved = source.resolve(strict=True)
            if "stubs" in {part.lower() for part in resolved.parts}:
                continue
            if not inside(resolved, cuda_root):
                if CUDA_RUNTIME_PREFIX.match(name):
                    raise RuntimeError(
                        f"CUDA toolkit dependency resolved outside toolkit root: {name} -> {resolved}"
                    )
                continue
            target = destination / name
            copy_unique(resolved, target)
            dependencies.append(name)

    licenses = stage_licenses(cuda_root, destination)
    notice = [
        "# CUDA Toolkit runtime notice",
        "",
        "This package contains only toolkit-owned CUDA runtime dependencies listed",
        "in `cuda-runtime-manifest.json`. NVIDIA driver components such as",
        "`libcuda.so.1` are not redistributed. A compatible NVIDIA driver must be",
        "installed by the user for CUDA acceleration.",
        "",
        "CUDA Toolkit EULA/license materials are included in `licenses/`.",
        "",
        "Bundled dependencies:",
        *(f"- `{name}`" for name in sorted(set(dependencies), key=str.lower)),
    ]
    (destination / "NOTICE.md").write_text("\n".join(notice) + "\n", encoding="utf-8")
    return write_manifest(destination, "linux", dependencies, licenses)


def find_dumpbin() -> str:
    executable = shutil.which("dumpbin.exe") or shutil.which("dumpbin")
    if executable:
        return executable
    program_files = os.environ.get("ProgramFiles(x86)", "")
    vswhere = (
        Path(program_files) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
        if program_files else None
    )
    if vswhere and vswhere.is_file():
        result = subprocess.run(
            [str(vswhere), "-latest", "-products", "*", "-property", "installationPath"],
            capture_output=True,
            text=True,
            check=False,
        )
        installation = (result.stdout or "").strip()
        if installation:
            root = Path(installation) / "VC" / "Tools" / "MSVC"
            matches = []
            for host in ("Hostx64", "Hostarm64", "Hostx86"):
                matches.extend(root.glob(f"*/bin/{host}/*/dumpbin.exe"))
            if matches:
                return str(sorted(matches, key=lambda p: str(p).lower())[-1])
    raise RuntimeError("Visual Studio dumpbin.exe was not found")


def dumpbin_imports(dumpbin: str, binary: Path) -> list[str]:
    result = subprocess.run(
        [dumpbin, "/DEPENDENTS", str(binary)],
        capture_output=True,
        text=True,
        check=False,
    )
    output = (result.stdout or "") + "\n" + (result.stderr or "")
    if result.returncode != 0:
        raise RuntimeError(f"dumpbin dependency inspection failed: {binary.name}")
    return [
        line.strip() for line in output.splitlines()
        if re.fullmatch(r"[A-Za-z0-9_.+-]+\.dll", line.strip(), re.IGNORECASE)
    ]


def resolve_windows_dll(name: str, dirs: list[Path]) -> Path | None:
    for directory in dirs:
        candidate = directory / name
        if candidate.is_file():
            return candidate.resolve()
        if directory.is_dir():
            match = next(
                (p for p in directory.iterdir() if p.is_file() and p.name.lower() == name.lower()),
                None,
            )
            if match:
                return match.resolve()
    return None


def stage_windows(binaries: list[Path], cuda_root: Path, destination: Path) -> Path:
    cuda_root = cuda_root.resolve(strict=True)
    cuda_bin = cuda_root / "bin"
    if not cuda_bin.is_dir():
        raise RuntimeError(f"CUDA Toolkit bin directory missing: {cuda_bin}")
    destination.mkdir(parents=True, exist_ok=False)
    dumpbin = find_dumpbin()
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    search_dirs = [
        cuda_bin,
        destination,
        *(Path(p) for p in os.environ.get("PATH", "").split(os.pathsep) if p),
        system_root / "System32",
    ]
    search_dirs.extend(Path(p).resolve().parent for p in binaries)
    pending = [Path(p).resolve(strict=True) for p in binaries]
    visited = set()
    dependencies = []

    while pending:
        binary = pending.pop()
        key = str(binary).lower()
        if key in visited:
            continue
        visited.add(key)
        for name in dumpbin_imports(dumpbin, binary):
            lowered = name.lower()
            if is_driver(name) or lowered.startswith(("api-ms-win-", "ext-ms-win-")):
                continue
            source = resolve_windows_dll(name, search_dirs)
            if source is None:
                raise RuntimeError(f"unresolved Windows import {name} from {binary.name}")
            if not inside(source, cuda_root):
                if CUDA_RUNTIME_PREFIX.match(name):
                    raise RuntimeError(
                        f"CUDA Toolkit DLL resolved outside toolkit root: {name} -> {source}"
                    )
                continue
            if is_driver(name):
                continue
            target = destination / name
            copy_unique(source, target)
            dependencies.append(name)
            pending.append(target.resolve())
            search_dirs.insert(1, destination)

    licenses = stage_licenses(cuda_root, destination)
    notice = [
        "# CUDA Toolkit runtime notice",
        "",
        "This package contains toolkit-owned CUDA runtime DLLs listed in",
        "`cuda-runtime-manifest.json`. `nvcuda.dll` and other NVIDIA driver",
        "components are not redistributed. A compatible NVIDIA driver must be",
        "installed by the user for CUDA acceleration.",
        "",
        "CUDA Toolkit EULA/license materials are included in `licenses/`.",
        "",
        "Bundled dependencies:",
        *(f"- `{name}`" for name in sorted(set(dependencies), key=str.lower)),
    ]
    (destination / "NOTICE.md").write_text("\n".join(notice) + "\n", encoding="utf-8")
    return write_manifest(destination, "windows", dependencies, licenses)


def attach_notices(
    runtime_dir: Path,
    notices_root: Path,
    bundle_root: Path | None = None,
) -> Path:
    import hashlib
    import json
    import shutil

    runtime_dir = runtime_dir.resolve(strict=True)
    notices_root.mkdir(parents=True, exist_ok=True)
    manifest = runtime_dir / "cuda-runtime-manifest.json"
    notice = runtime_dir / "NOTICE.md"
    if not manifest.is_file() or not notice.is_file():
        raise RuntimeError("CUDA runtime manifest or NOTICE.md is missing")

    data = json.loads(manifest.read_text(encoding="utf-8"))
    target = notices_root / "cuda-runtime-notices"
    target.mkdir(parents=True, exist_ok=True)

    def copy_checked(source: Path, destination: Path) -> None:
        if source.is_symlink() or not source.is_file():
            raise RuntimeError(f"CUDA notice source is not a regular file: {source}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.is_symlink() or destination.read_bytes() != source.read_bytes():
                raise RuntimeError(f"Conflicting CUDA notice file: {destination}")
        else:
            shutil.copy2(source, destination)

    copy_checked(manifest, target / manifest.name)
    copy_checked(notice, target / notice.name)

    raw_licenses = data.get("licenses", [])
    if isinstance(raw_licenses, dict):
        raw_licenses = list(raw_licenses.keys())
    if not isinstance(raw_licenses, list):
        raise RuntimeError("CUDA runtime license inventory is invalid")

    license_paths = []
    for item in raw_licenses:
        if isinstance(item, str):
            relative_name = item
        elif isinstance(item, dict):
            relative_name = item.get("path") or item.get("file")
        else:
            relative_name = None
        if not isinstance(relative_name, str) or not relative_name:
            raise RuntimeError("CUDA runtime contains an invalid license path")

        relative = Path(relative_name)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError(f"Unsafe CUDA license path: {relative_name}")

        source = runtime_dir / relative
        destination = target / relative
        copy_checked(source, destination)
        license_paths.append(destination.relative_to(notices_root).as_posix())

    build_notices = notices_root / "BUILD-NOTICES.md"
    existing_text = (
        build_notices.read_text(encoding="utf-8")
        if build_notices.is_file()
        else "# Third-party build notices\n"
    )
    section = (
        "## CUDA Toolkit runtime\n\n"
        "Toolkit-owned CUDA runtime dependencies and applicable license "
        "materials are included under `cuda-runtime-notices/` and inventoried "
        "in `NOTICE-MANIFEST.json`. NVIDIA driver libraries are not bundled; "
        "a compatible driver must be installed separately for CUDA acceleration.\n"
    )
    if "## CUDA Toolkit runtime" not in existing_text:
        build_notices.write_text(
            existing_text.rstrip() + "\n\n" + section,
            encoding="utf-8",
        )

    manifest_path = notices_root / "NOTICE-MANIFEST.json"
    if manifest_path.is_file():
        combined = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(combined, dict):
            raise RuntimeError("Existing NOTICE-MANIFEST.json is not an object")
    else:
        combined = {"schema": "rdt-third-party-notices-v1"}

    files = []
    for path in sorted(notices_root.rglob("*")):
        if (
            not path.is_file()
            or path.is_symlink()
            or path.name in {
                "NOTICE-MANIFEST.json",
                "NOTICE-MANIFEST.json.tmp",
                "BUILD-NOTICES.md",
            }
        ):
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        files.append({
            "path": path.relative_to(notices_root).as_posix(),
            "sha256": digest,
            "size_bytes": path.stat().st_size,
        })

    combined["files"] = files
    combined["notice_file_count"] = len(files)
    combined["cuda_runtime"] = {
        "notice_directory": target.relative_to(notices_root).as_posix(),
        "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "license_paths": license_paths,
        "driver_libraries_redistributed": False,
    }

    temporary = manifest_path.with_name("NOTICE-MANIFEST.json.tmp")
    temporary.write_text(
        json.dumps(combined, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(manifest_path)

    if bundle_root is not None:
        bundle_target = Path(bundle_root) / "cuda-runtime-notices"
        for path in sorted(target.rglob("*")):
            if path.is_file():
                copy_checked(
                    path,
                    bundle_target / path.relative_to(target),
                )

    return target



def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    stage = sub.add_parser("stage")
    stage.add_argument("--platform", choices=("linux", "windows"), required=True)
    stage.add_argument("--cuda-root", type=Path, required=True)
    stage.add_argument("--destination", type=Path, required=True)
    stage.add_argument("--binary", type=Path, action="append", required=True)
    notices = sub.add_parser("attach-notices")
    notices.add_argument("--runtime-dir", type=Path, required=True)
    notices.add_argument("--notices-root", type=Path, required=True)
    notices.add_argument("--bundle-root", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "attach-notices":
            output = attach_notices(
                args.runtime_dir, args.notices_root, args.bundle_root
            )
            print(f"CUDA_RUNTIME_NOTICES={output}")
            return 0
        if args.platform == "linux":
            output = stage_linux(args.binary, args.cuda_root, args.destination)
        else:
            output = stage_windows(args.binary, args.cuda_root, args.destination)
        data = json.loads(output.read_text(encoding="utf-8"))
        print(f"CUDA_RUNTIME_MANIFEST={output}")
        print(f"CUDA_RUNTIME_DEPENDENCY_COUNT={len(data['dependencies'])}")
        print(f"CUDA_RUNTIME_LICENSE_COUNT={len(data['licenses'])}")
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"CUDA_RUNTIME_STAGING_FAILED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
