"""Derive RDT Connect macOS release identity from its version and build marker."""

import re
from dataclasses import dataclass
from pathlib import Path


VERSION_PATTERN = re.compile(
    r"^(?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)-"
    r"(?P<series>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)\."
    r"(?P<marker>[1-5][A-Z][0-9]{2})$"
)


@dataclass(frozen=True)
class ReleaseIdentity:
    version: str
    marker: str
    tag: str
    stage_directory: str
    archive_name: str
    signature_name: str
    dmg_name: str
    archive_url: str


def derive_release_identity(version: str, marker: str) -> ReleaseIdentity:
    if not isinstance(version, str) or not isinstance(marker, str):
        raise ValueError("version and marker must be strings")

    match = VERSION_PATTERN.fullmatch(version)
    if not match:
        raise ValueError("version must end in a valid platform build marker")

    version_marker = match.group("marker")
    if marker != version_marker:
        raise ValueError(
            f"build marker mismatch: version says {version_marker}, marker file says {marker}"
        )

    if marker[0] != "1":
        raise ValueError("this release verifier supports macOS Apple Silicon only")

    base = ".".join(
        (match.group("major"), match.group("minor"), match.group("patch"))
    )
    series = match.group("series")
    tag = f"rdt-connect-v{base}-{series}-{marker.lower()}"
    dmg_name = f"RDT_Connect_{version}_aarch64.dmg"
    archive_name = "RDT.Connect.app.tar.gz"
    signature_name = archive_name + ".sig"
    archive_url = (
        "https://github.com/RolanDorisTech/rdt-voice/releases/download/"
        f"{tag}/{archive_name}"
    )

    return ReleaseIdentity(
        version=version,
        marker=marker,
        tag=tag,
        stage_directory=f"build/release-assets-{marker}",
        archive_name=archive_name,
        signature_name=signature_name,
        dmg_name=dmg_name,
        archive_url=archive_url,
    )


def derive_from_project(connect_root: Path) -> ReleaseIdentity:
    import json

    package = json.loads((connect_root / "package.json").read_text(encoding="utf-8"))
    config = json.loads(
        (connect_root / "src-tauri/tauri.conf.json").read_text(encoding="utf-8")
    )
    marker = (
        connect_root / "scripts/build-id-sequence.txt"
    ).read_text(encoding="utf-8").strip()

    version = package.get("version")
    if version != config.get("version"):
        raise ValueError("package.json and tauri.conf.json versions differ")

    return derive_release_identity(version, marker)
