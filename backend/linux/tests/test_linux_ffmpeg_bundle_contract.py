from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from backend.linux.media_tools import resolve_media_tool


REPO = Path(__file__).resolve().parents[3]


class LinuxFfmpegBundleContractTests(unittest.TestCase):
    def test_explicit_override_wins(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            override = str(Path(temp) / "custom-ffmpeg")
            previous = os.environ.get("FFMPEG_PATH")
            os.environ["FFMPEG_PATH"] = override
            try:
                actual = resolve_media_tool(
                    "ffmpeg",
                    "FFMPEG_PATH",
                    frozen=True,
                    executable=Path(temp) / "app",
                    path_lookup=lambda _: "/usr/bin/ffmpeg",
                )
            finally:
                if previous is None:
                    os.environ.pop("FFMPEG_PATH", None)
                else:
                    os.environ["FFMPEG_PATH"] = previous
            self.assertEqual(actual, override)

    def test_frozen_app_prefers_bundled_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / "rdt_voice_backend"
            packaged = root / "ffmpeg" / "bin" / "ffmpeg"
            packaged.parent.mkdir(parents=True)
            executable.write_text("", encoding="utf-8")
            packaged.write_text("#!/bin/sh\n", encoding="utf-8")
            packaged.chmod(0o755)

            actual = resolve_media_tool(
                "ffmpeg",
                "UNSET_MEDIA_TOOL_OVERRIDE",
                frozen=True,
                executable=executable,
                path_lookup=lambda _: "/usr/bin/ffmpeg",
            )
            self.assertEqual(Path(actual).resolve(), packaged.resolve())

    def test_development_mode_uses_path_fallback(self) -> None:
        actual = resolve_media_tool(
            "ffmpeg",
            "UNSET_MEDIA_TOOL_OVERRIDE",
            frozen=False,
            path_lookup=lambda _: "/usr/bin/ffmpeg",
        )
        self.assertEqual(actual, "/usr/bin/ffmpeg")

    def test_ffmpeg_builder_manifest_inventories_symlinks(self) -> None:
        script = (REPO / "backend/linux/scripts/build-linux-ffmpeg.sh").read_text(encoding="utf-8")
        self.assertIn("runtime_symlinks = []", script)
        self.assertIn("link_target = os.readlink(path)", script)
        self.assertIn('"link_target": link_target', script)
        self.assertIn('"resolved_path": resolved_relative', script)
        self.assertIn('"resolved_sha256": digest(resolved)', script)
        self.assertIn('"runtime_symlink_count": len(runtime_symlinks)', script)

    def test_linux_packager_verifies_ffmpeg_manifest_and_symlinks(self) -> None:
        script = (REPO / "rdt_connect/scripts/prepare-linux-bundle.sh").read_text(encoding="utf-8")
        self.assertIn('"${FFMPEG_NOTICE_ROOT}"', script)
        self.assertIn("ffmpeg_build_manifest_path", script)
        self.assertIn("actual_symlinks", script)
        self.assertIn("expected_symlinks != actual_symlinks", script)
        self.assertIn("symlink target is not relocatable", script)
        self.assertIn("PurePosixPath(target).is_absolute()", script)
        self.assertIn("manifest root must be a JSON object", script)
        self.assertIn('type(ffmpeg_build_manifest.get("runtime_file_count")) is not int', script)
        self.assertIn('type(ffmpeg_build_manifest.get("runtime_size_bytes")) is not int', script)
        self.assertIn("safe_relative_path", script)
        self.assertIn("valid_sha256", script)
        self.assertIn("runtime_file_count", script)
        self.assertIn("runtime_symlink_count", script)
        self.assertIn("duplicate or malformed file record", script)
        self.assertIn("duplicate or malformed symlink record", script)
        self.assertIn('"type": "symlink"', script)
        self.assertIn("'symlink_count': len(ffmpeg_symlinks)", script)

    def test_linux_native_workflow_installs_binutils(self) -> None:
        workflow = (
            REPO / ".github/workflows/linux-native-qualification.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("binutils", workflow)

    def test_missing_tool_returns_none(self) -> None:
        actual = resolve_media_tool(
            "ffprobe",
            "UNSET_MEDIA_TOOL_OVERRIDE",
            frozen=False,
            path_lookup=lambda _: None,
        )
        self.assertIsNone(actual)

    def test_linux_builder_pins_and_disables_gpl_and_nonfree(self) -> None:
        script = (
            REPO / "backend/linux/scripts/build-linux-ffmpeg.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('VERSION="9.1"', script)
        self.assertIn("--disable-gpl", script)
        self.assertIn("--disable-nonfree", script)
        self.assertIn("--enable-shared", script)
        self.assertIn("--disable-static", script)
        self.assertIn(
            "FCF986EA15E6E293A5644F10B4322F04D67658D8",
            script,
        )
        self.assertIn('gpg --batch --with-colons --fingerprint --list-keys', script)
        self.assertIn('ACTUAL_PRIMARY_FINGERPRINT', script)
        self.assertIn('EXPECTED_PRIMARY_FINGERPRINT', script)
        self.assertIn('gpg --batch --status-fd 1 --verify "$SIGNATURE" "$TARBALL"', script)
        self.assertIn('$1 == "[GNUPG:]" && $2 == "VALIDSIG"', script)
        self.assertIn('primary = (NF >= 12 ? toupper($12) : signer)', script)
        self.assertIn('FFMPEG_SIGNATURE_PRIMARY_KEY_MATCH=PASS', script)
        self.assertIn("readelf -d", script)
        self.assertIn("readlink -f --", script)
        self.assertIn("FFMPEG_DEP_PATTERN", script)
        self.assertIn("FFmpeg library resolved outside private prefix", script)
        self.assertIn("FFmpeg library target escapes private prefix", script)
        self.assertIn("PRIVATE_FFMPEG_ELF_DEPENDENCIES=PASS", script)
        self.assertIn("env -u LD_LIBRARY_PATH PATH=/usr/bin:/bin", script)
        self.assertIn("'$ORIGIN/../lib'", script)

    def test_linux_packager_requires_bundled_ffmpeg_and_ffprobe(self) -> None:
        script = (
            REPO / "rdt_connect/scripts/prepare-linux-bundle.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("build-linux-ffmpeg.sh", script)
        self.assertIn('stage/"ffmpeg"', script)
        self.assertIn("ffprobe", script)
        self.assertIn("LINUX_BUNDLED_FFMPEG_SMOKE=PASS", script)


if __name__ == "__main__":
    unittest.main(verbosity=2)
