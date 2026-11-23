import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "rdt_connect/scripts/merge-platform-third-party-notices.py"

spec = importlib.util.spec_from_file_location("platform_notice_merge", SCRIPT)
merge_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(merge_module)


def merge_patch(target, patch):
    if not isinstance(patch, dict):
        return patch
    result = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = merge_patch(result.get(key), value)
    return result


class PlatformSharedNoticeMergeTests(unittest.TestCase):
    def make_trees(self, root):
        shared = root / "shared"
        platform = root / "linux-third-party-notices"
        (shared / "opencc-js").mkdir(parents=True)
        (shared / "rdt-offline-runtime").mkdir()
        platform.mkdir()
        (shared / "BUILD-NOTICES.md").write_text(
            "# RDT Connect shared build notices\n\n"
            "R82 embedded frontend is pinned.\n"
            "OpenCC-JS licenses are in opencc-js/.\n"
            "Offline runtime notices are in rdt-offline-runtime/.\n"
        )
        (shared / "opencc-js/LICENSE").write_text("OpenCC license\n")
        (shared / "rdt-offline-runtime/NOTICE.md").write_text("runtime notice\n")
        (platform / "NOTICE-MANIFEST.json").write_text('{"platform":true}\n')
        (platform / "BUILD-NOTICES.md").write_text(
            "# Linux packaged components\n\n"
            "Platform-specific FFmpeg, CUDA, whisper.cpp and Python notices.\n"
        )
        (platform / "ffmpeg").mkdir()
        (platform / "ffmpeg/LICENSE.txt").write_text("FFmpeg license\n")
        return shared, platform

    def test_native_overlays_leave_exactly_one_notice_source(self):
        base = json.loads(
            (ROOT / "rdt_connect/src-tauri/tauri.conf.json").read_text()
        )
        for platform in ("linux", "windows"):
            overlay = json.loads(
                (ROOT / f"rdt_connect/src-tauri/tauri.{platform}.conf.json").read_text()
            )
            effective = merge_patch(base, overlay)
            resources = effective["bundle"]["resources"]
            matches = [
                source for source, destination in resources.items()
                if destination == "third_party_notices"
            ]
            self.assertEqual(
                matches, [f"resources/{platform}-third-party-notices"]
            )
        self.assertEqual(
            base["bundle"]["resources"]["resources/third_party_notices"],
            "third_party_notices",
        )

    def test_merge_preserves_platform_manifest_and_combines_build_notices(self):
        with tempfile.TemporaryDirectory() as temp:
            shared, platform = self.make_trees(Path(temp))
            original = (platform / "BUILD-NOTICES.md").read_text()
            result = merge_module.merge_shared_notices(shared, platform)

            final = (platform / "BUILD-NOTICES.md").read_text()
            self.assertIn(original.strip(), final)
            self.assertIn("FFmpeg, CUDA, whisper.cpp and Python", final)
            self.assertIn(merge_module.BUILD_SECTION, final)
            self.assertEqual(final.count(merge_module.BUILD_SECTION), 1)
            self.assertEqual(
            __import__("json").loads((platform / "NOTICE-MANIFEST.json").read_text()),
            {"platform": True},
        )
            self.assertEqual(
                (platform / "ffmpeg/LICENSE.txt").read_text(),
                'FFmpeg license\n',
            )
            self.assertTrue((platform / "opencc-js/LICENSE").is_file())
            self.assertTrue(
                (platform / "rdt-offline-runtime/NOTICE.md").is_file()
            )

            manifest = json.loads(
                (platform / merge_module.MANIFEST_NAME).read_text()
            )
            self.assertEqual(result["file_count"], 3)
            for relative, record in manifest["files"].items():
                path = platform / relative
                self.assertEqual(path.stat().st_size, record["bytes"])
                self.assertEqual(
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    record["sha256"],
                )
            build_record = manifest["files"]["BUILD-NOTICES.md"]
            self.assertIn("source_sha256", build_record)
            self.assertEqual(
                build_record["source_sha256"],
                hashlib.sha256(
                    (shared / "BUILD-NOTICES.md").read_bytes()
                ).hexdigest(),
            )

    def test_merge_is_idempotent_without_duplicate_build_notice_section(self):
        with tempfile.TemporaryDirectory() as temp:
            shared, platform = self.make_trees(Path(temp))
            first = merge_module.merge_shared_notices(shared, platform)
            before = (platform / "BUILD-NOTICES.md").read_text()
            second = merge_module.merge_shared_notices(shared, platform)
            after = (platform / "BUILD-NOTICES.md").read_text()
            self.assertEqual(first, second)
            self.assertEqual(before, after)
            self.assertEqual(after.count(merge_module.BUILD_SECTION), 1)

    def test_conflict_fails_without_overwriting_platform_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shared = root / "shared"
            platform = root / "linux-third-party-notices"
            (shared / "opencc-js").mkdir(parents=True)
            platform.mkdir()
            (shared / "BUILD-NOTICES.md").write_text(
                "# Shared\n\nCommon notices.\n"
            )
            (platform / "BUILD-NOTICES.md").write_text(
                "# Linux\n\nPlatform notices.\n"
            )
            (shared / "opencc-js/LICENSE").write_text("common\n")
            (platform / "opencc-js").mkdir()
            (platform / "opencc-js/LICENSE").write_text("platform-owned\n")

            original_build = (platform / "BUILD-NOTICES.md").read_text()
            with self.assertRaises(RuntimeError):
                merge_module.merge_shared_notices(shared, platform)
            self.assertEqual(
                (platform / "opencc-js/LICENSE").read_text(),
                "platform-owned\n",
            )
            self.assertEqual(
                (platform / "BUILD-NOTICES.md").read_text(),
                original_build,
            )

    def test_common_build_notice_section_conflict_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            shared, platform = self.make_trees(Path(temp))
            target = platform / "BUILD-NOTICES.md"
            target.write_text(
                target.read_text()
                + "\n"
                + merge_module.BUILD_SECTION
                + "\n\nDifferent common notice content.\n"
            )
            with self.assertRaises(RuntimeError):
                merge_module.merge_shared_notices(shared, platform)

    def test_native_workflows_merge_shared_notices_after_platform_collection(self):
        linux = (ROOT / ".github/workflows/linux-native-qualification.yml").read_text()
        linux_prepare = linux.index("bash rdt_connect/scripts/prepare-linux-bundle.sh")
        linux_merge = linux.index("merge-platform-third-party-notices.py")
        linux_build = linux.index("Build Linux Debian and signed AppImage artifacts")
        self.assertLess(linux_prepare, linux_merge)
        self.assertLess(linux_merge, linux_build)
        self.assertIn("--platform linux", linux[linux_merge:linux_build])

        for arch in ("x64", "arm64"):
            workflow = (
                ROOT / f".github/workflows/windows-{arch}-installer.yml"
            ).read_text()
            prepare = workflow.index("prepare-windows-bundle.ps1")
            merge = workflow.index("merge-platform-third-party-notices.py")
            frontend = workflow.index("Build frontend")
            self.assertLess(prepare, merge)
            self.assertLess(merge, frontend)
            self.assertIn("--platform windows", workflow[merge:frontend])

    def test_native_workflows_use_platform_overlay_configs(self):
        linux = (ROOT / ".github/workflows/linux-native-qualification.yml").read_text()
        self.assertIn("--config src-tauri/tauri.linux.conf.json", linux)
        for arch in ("x64", "arm64"):
            workflow = (
                ROOT / f".github/workflows/windows-{arch}-installer.yml"
            ).read_text()
            self.assertIn("--config src-tauri/tauri.windows.conf.json", workflow)


if __name__ == "__main__":
    unittest.main()
