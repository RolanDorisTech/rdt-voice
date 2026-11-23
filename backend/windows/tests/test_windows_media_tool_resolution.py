from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
WINDOWS_BACKEND = ROOT / "backend" / "windows"
if str(WINDOWS_BACKEND) not in sys.path:
    sys.path.insert(0, str(WINDOWS_BACKEND))

from media_tools import resolve_media_tool


class WindowsMediaToolResolutionTests(unittest.TestCase):
    def test_explicit_override_wins(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            override = str(Path(temp) / "custom-ffmpeg.exe")
            with patch.dict(os.environ, {"RDT_TEST_FFMPEG_OVERRIDE": override}):
                actual = resolve_media_tool(
                    "ffmpeg.exe",
                    "RDT_TEST_FFMPEG_OVERRIDE",
                    frozen=True,
                    executable=Path(temp) / "rdt_voice_backend.exe",
                    path_lookup=lambda _: "C:/host/ffmpeg.exe",
                )
            self.assertEqual(actual, override)

    def test_frozen_app_prefers_executable_beside_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / "rdt_voice_backend.exe"
            packaged = root / "ffmpeg.exe"
            executable.write_bytes(b"")
            packaged.write_bytes(b"")
            with patch.dict(os.environ, {"RDT_TEST_FFMPEG_OVERRIDE": ""}):
                actual = resolve_media_tool(
                    "ffmpeg.exe",
                    "RDT_TEST_FFMPEG_OVERRIDE",
                    frozen=True,
                    executable=executable,
                    path_lookup=lambda _: "C:/host/ffmpeg.exe",
                )
            self.assertEqual(Path(actual).resolve(), packaged.resolve())

    def test_frozen_app_falls_back_to_path_when_bundle_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            actual_path = "C:/host/ffmpeg.exe"
            with patch.dict(os.environ, {"RDT_TEST_FFMPEG_OVERRIDE": ""}):
                actual = resolve_media_tool(
                    "ffmpeg.exe",
                    "RDT_TEST_FFMPEG_OVERRIDE",
                    frozen=True,
                    executable=Path(temp) / "rdt_voice_backend.exe",
                    path_lookup=lambda _: actual_path,
                )
            self.assertEqual(actual, actual_path)

    def test_development_mode_uses_path(self) -> None:
        with patch.dict(os.environ, {"RDT_TEST_FFMPEG_OVERRIDE": ""}):
            actual = resolve_media_tool(
                "ffmpeg.exe",
                "RDT_TEST_FFMPEG_OVERRIDE",
                frozen=False,
                path_lookup=lambda _: "C:/dev-tools/ffmpeg.exe",
            )
        self.assertEqual(actual, "C:/dev-tools/ffmpeg.exe")

    def test_missing_tool_returns_none(self) -> None:
        with patch.dict(os.environ, {"RDT_TEST_FFMPEG_OVERRIDE": ""}):
            actual = resolve_media_tool(
                "ffprobe.exe",
                "RDT_TEST_FFMPEG_OVERRIDE",
                frozen=False,
                path_lookup=lambda _: None,
            )
        self.assertIsNone(actual)

    def test_server_uses_resolver_for_both_tools(self) -> None:
        source = (ROOT / "backend/windows/server.py").read_text(encoding="utf-8")
        self.assertIn('resolve_media_tool("ffmpeg.exe", "FFMPEG_PATH")', source)
        self.assertIn('resolve_media_tool("ffprobe.exe", "FFPROBE_PATH")', source)
        self.assertIn("_ffprobe_sibling.is_file()", source)

    def test_pyinstaller_searches_windows_helper_directory(self) -> None:
        source = (
            ROOT / "rdt_connect/scripts/prepare-windows-bundle.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "'--paths',(Join-Path $RepoRoot 'backend/windows'),",
            source,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
