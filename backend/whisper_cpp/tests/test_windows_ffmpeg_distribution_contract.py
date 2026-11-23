import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


class WindowsFfmpegDistributionContractTests(unittest.TestCase):
    def test_release_and_architecture_pins_are_immutable_lgpl_shared(self):
        pins = json.loads(
            (ROOT / ".github/config/windows-ffmpeg-pins.json").read_text(encoding="utf-8")
        )
        self.assertEqual(pins["release"]["repository"], "FFmpeg-Builds/FFmpeg-Builds")
        self.assertEqual(pins["release"]["tag"], "autobuild-2026-11-23-12-43")
        self.assertEqual(
            pins["release"]["release_commit"],
            "b9ebe81995b58cd4e3177a6c0ad5dc2392f7f7fd",
        )
        self.assertEqual(
            pins["release"]["checksum_sha256"],
            "d0fb40099e1c98a5683457868da3ac7ddddc4ab155836b3cf8cc8b18cb00a96b",
        )
        self.assertEqual(pins["ffmpeg"]["source_commit"],
                         "05c674c9132eb13e65e3fb6e9b4cf1cad1219fd5")
        self.assertEqual(pins["ffmpeg"]["build_variant"], "LGPL shared")
        self.assertEqual(pins["ffmpeg"]["spdx_license"], "LGPL-2.1-or-later")
        self.assertIn("-lgpl-shared.zip", pins["architectures"]["x64"]["asset"])
        self.assertIn("-lgpl-shared.zip", pins["architectures"]["arm64"]["asset"])
        self.assertEqual(pins["architectures"]["x64"]["pe_machine"], 34404)
        self.assertEqual(pins["architectures"]["arm64"]["pe_machine"], 43620)

    def test_downloader_enforces_https_redirects_hashes_architecture_and_shared_licensing(self):
        helper = (ROOT / ".github/scripts/download-pinned-ffmpeg.ps1").read_text()
        for required in (
            "AllowAutoRedirect = $false",
            "release-assets.githubusercontent.com",
            "objects.githubusercontent.com",
            "Uri.Scheme -ne \"https\"",
            "checksum_sha256",
            "Get-FileHash",
            "Expand-Archive",
            "Get-PeMachine",
            "--enable-shared",
            "--enable-gpl|--enable-nonfree",
            "RDT-FFmpeg-PROVENANCE.json",
            "RDT-FFmpeg-SOURCE-AND-LICENSE.txt",
        ):
            self.assertIn(required, helper)
        self.assertLess(helper.index("Get-FileHash -LiteralPath $archive"),
                        helper.index("Expand-Archive -LiteralPath $archive"))

    def test_both_architecture_workflows_use_the_shared_downloader(self):
        for filename, arch in (
            ("windows-x64-installer.yml", "x64"),
            ("windows-arm64-installer.yml", "arm64"),
        ):
            workflow = (ROOT / ".github/workflows" / filename).read_text()
            self.assertIn("download-pinned-ffmpeg.ps1", workflow)
            self.assertIn(f"-Architecture '{arch}'", workflow)
            self.assertIn("RDT_FFMPEG_DIR=$destination", workflow)
            self.assertNotIn("Invoke-WebRequest -Uri $uri", workflow)
            self.assertNotIn("releases/download/latest/", workflow)


if __name__ == "__main__":
    unittest.main()


class WindowsFfmpegPackagerStagingTests(unittest.TestCase):
    def test_windows_packager_stages_shared_dlls_and_provenance(self):
        script = Path("rdt_connect/scripts/prepare-windows-bundle.ps1").read_text(encoding="utf-8")
        self.assertIn("Get-ChildItem -LiteralPath $FfmpegDir -Filter '*.dll' -File -Recurse", script)
        self.assertIn("Copy-Item -LiteralPath $dll.FullName -Destination $backendStage -Force", script)
        self.assertIn("avcodec", script)
        self.assertIn("avformat", script)
        self.assertIn("avutil", script)
        self.assertIn("RDT-FFmpeg-PROVENANCE.json", script)
        self.assertIn("RDT-FFmpeg-SOURCE-AND-LICENSE.txt", script)
