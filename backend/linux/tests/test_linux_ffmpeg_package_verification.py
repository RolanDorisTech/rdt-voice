from __future__ import annotations

import hashlib
import os
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
HELPER = REPO / ".github/scripts/verify-packaged-linux-ffmpeg.py"
SPEC = importlib.util.spec_from_file_location("linux_ffmpeg_verifier", HELPER)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load installed FFmpeg verifier")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class InstalledFfmpegInventoryTests(unittest.TestCase):
    def make_package(self, base: Path) -> Path:
        backend = base / "backend"
        root = backend / "ffmpeg"
        (root / "bin").mkdir(parents=True)
        (root / "lib").mkdir(parents=True)

        files = {
            "bin/ffmpeg": b"ffmpeg-test-binary",
            "bin/ffprobe": b"ffprobe-test-binary",
            "lib/libavutil.so.1": b"avutil-test-library",
        }
        entries = {}
        total = 0
        for relative, data in files.items():
            path = root / relative
            path.write_bytes(data)
            if relative.startswith("bin/"):
                path.chmod(0o755)
            entries["ffmpeg/" + relative] = {
                "type": "file",
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            total += len(data)

        target = root / "lib/libavutil.so.1"
        (root / "lib/libavutil.so").symlink_to("libavutil.so.1")
        entries["ffmpeg/lib/libavutil.so"] = {
            "type": "symlink",
            "link_target": "libavutil.so.1",
            "resolved_path": "lib/libavutil.so.1",
            "resolved_size_bytes": target.stat().st_size,
            "resolved_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        }

        manifest = {
            "schema": "rdt-linux-backend-bundle-v2",
            "ffmpeg": {
                "version": "9.1",
                "build_manifest_sha256": "a" * 64,
                "files": entries,
                "regular_file_count": len(files),
                "symlink_count": 1,
                "runtime_size_bytes": total,
            },
        }
        (backend / "rdt-linux-backend-bundle.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        return backend

    def test_valid_private_inventory_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            backend = self.make_package(Path(temp))
            self.assertEqual(
                MODULE.verify_inventory(backend),
                (backend / "ffmpeg").resolve(),
            )

    def test_modified_runtime_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            backend = self.make_package(Path(temp))
            (backend / "ffmpeg/lib/libavutil.so.1").write_bytes(b"tampered")
            with self.assertRaises(MODULE.VerificationError):
                MODULE.verify_inventory(backend)

    def test_unexpected_fifo_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            backend = self.make_package(Path(temp))
            os.mkfifo(backend / "ffmpeg/unexpected.pipe")
            with self.assertRaises(MODULE.VerificationError):
                MODULE.verify_inventory(backend)

    def test_absolute_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            backend = self.make_package(base)
            link = backend / "ffmpeg/lib/libavutil.so"
            link.unlink()
            outside = base / "outside.so"
            outside.write_bytes(b"outside")
            link.symlink_to(outside)
            with self.assertRaises(MODULE.VerificationError):
                MODULE.verify_inventory(backend)


class LinuxWorkflowPrivateFfmpegTests(unittest.TestCase):
    def test_clean_container_verifies_private_ffmpeg_without_system_ffmpeg(self) -> None:
        workflow = (REPO / ".github/workflows/linux-native-qualification.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("verify-packaged-linux-ffmpeg.py", workflow)
        self.assertIn('procps binutils ca-certificates "$DEB"', workflow)
        self.assertNotIn('procps ffmpeg ca-certificates "$DEB"', workflow)
        self.assertIn("command -v ffmpeg", workflow)
        self.assertIn("command -v ffprobe", workflow)
        server = (REPO / "backend/linux/server.py").read_text(encoding="utf-8")
        self.assertIn("from media_tools import resolve_media_tool", server)
        ffmpeg_line = "FFMPEG = resolve_media_tool(" + chr(34) + "ffmpeg" + chr(34) + ", " + chr(34) + "FFMPEG_PATH" + chr(34) + ")"
        ffprobe_line = "FFPROBE = resolve_media_tool(" + chr(34) + "ffprobe" + chr(34) + ", " + chr(34) + "FFPROBE_PATH" + chr(34) + ")"
        self.assertIn(ffmpeg_line, server)
        self.assertIn(ffprobe_line, server)
        qualification = (REPO / ".github/scripts/qualify-packaged-linux-backend.py").read_text(encoding="utf-8")
        self.assertIn('env.pop("FFMPEG_PATH", None)', qualification)
        self.assertIn('env.pop("FFPROBE_PATH", None)', qualification)


if __name__ == "__main__":
    unittest.main()
