from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend/whisper_cpp"))
from lifetime_statistics import record_lifetime_transcription


class LifetimeStatisticsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "lifetime-statistics.json"
        patcher = patch.dict(os.environ, {"RDT_CONNECT_STATS_PATH": str(self.path)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def record(self, upload=123, audio=2.5, inference=0.25, model="large-v3-turbo"):
        record_lifetime_transcription(
            upload, audio, inference, {"provider": "CUDA", "device": "GPU 0"}, model
        )

    def test_aggregates_native_schema_and_never_stores_transcripts(self):
        self.record()
        self.record(upload=7, audio=1.25, inference=0.1, model="base")
        data = json.loads(self.path.read_text())
        self.assertEqual(data["schema"], "rdt-connect-lifetime-statistics-v1")
        self.assertEqual(data["totalTranscriptions"], 2)
        self.assertEqual(data["totalUploadBytes"], 130)
        self.assertAlmostEqual(data["totalAudioSeconds"], 3.75)
        self.assertAlmostEqual(data["totalInferenceMilliseconds"], 350.0)
        self.assertTrue(data["firstTranscriptionAt"])
        self.assertTrue(data["lastTranscriptionAt"])
        self.assertEqual(data["hardwareUsage"]["CUDA (GPU 0)"]["transcriptions"], 2)
        self.assertEqual(data["modelUsage"]["base"]["transcriptions"], 1)
        self.assertEqual(set(data), {
            "schema",
            "firstTranscriptionAt",
            "lastTranscriptionAt",
            "totalTranscriptions",
            "totalAudioSeconds",
            "totalUploadBytes",
            "totalInferenceMilliseconds",
            "hardwareUsage",
            "modelUsage",
        })
        usage_fields = {
            "transcriptions",
            "audioSeconds",
            "uploadBytes",
            "inferenceMilliseconds",
        }
        for usage_map in (data["hardwareUsage"], data["modelUsage"]):
            for entry in usage_map.values():
                self.assertEqual(set(entry), usage_fields)

    def test_concurrent_updates_are_not_lost(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: self.record(upload=1, audio=1, inference=0), range(24)))
        data = json.loads(self.path.read_text())
        self.assertEqual(data["totalTranscriptions"], 24)
        self.assertEqual(data["totalUploadBytes"], 24)

    def test_rejects_relative_statistics_path(self):
        with patch.dict(os.environ, {"RDT_CONNECT_STATS_PATH": "relative/stats.json"}):
            with self.assertRaises(RuntimeError):
                self.record()

    def test_rejects_invalid_existing_schema(self):
        self.path.write_text('{"schema":"wrong"}')
        with self.assertRaises(RuntimeError):
            self.record()

    def test_refuses_symlink_target(self):
        target = self.path.with_name("preserved.json")
        target.write_text('{"preserve":true}')
        try:
            self.path.symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable on this runner: {exc}")
        with self.assertRaises(RuntimeError):
            self.record()
        self.assertEqual(json.loads(target.read_text()), {"preserve": True})

    def test_rejects_oversized_statistics(self):
        self.path.write_bytes(b" " * 1_048_577)
        with self.assertRaises(RuntimeError):
            self.record()

    def test_sources_rust_schema_bundles_and_all_native_workflows_require_module(self):
        for platform in ("linux", "windows"):
            source = (ROOT / f"backend/{platform}/server.py").read_text()
            self.assertIn(
                "from lifetime_statistics import record_lifetime_transcription", source
            )
            self.assertIn("record_lifetime_transcription(", source)

        windows_server = (ROOT / "backend/windows/server.py").read_text()
        self.assertIn("_SHARED_WHISPER_CPP", windows_server)
        self.assertIn("sys.path.insert", windows_server)

        linux_bundle = (ROOT / "rdt_connect/scripts/prepare-linux-bundle.sh").read_text()
        windows_bundle = (ROOT / "rdt_connect/scripts/prepare-windows-bundle.ps1").read_text()
        self.assertIn("--hidden-import lifetime_statistics", linux_bundle)
        self.assertIn("'--hidden-import','lifetime_statistics'", windows_bundle)
        self.assertIn("'--paths',(Join-Path $RepoRoot 'backend/whisper_cpp')", windows_bundle)

        native = (ROOT / "rdt_connect/src-tauri/src/main.rs").read_text()
        self.assertRegex(native, r'\.env\(\s*"RDT_CONNECT_STATS_PATH"\s*,')
        self.assertIn("lifetime-statistics.json", native)
        self.assertIn('"rdt-connect-lifetime-statistics-v1"', native)

        workflows = (
            ".github/workflows/linux-native-qualification.yml",
            ".github/workflows/windows-x64-installer.yml",
            ".github/workflows/windows-arm64-installer.yml",
        )
        for name in workflows:
            self.assertIn(
                "python backend/whisper_cpp/tests/test_lifetime_statistics.py",
                (ROOT / name).read_text(),
                f"{name} must execute the lifetime statistics regression test",
            )


if __name__ == "__main__":
    unittest.main()
