import hashlib
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CONNECT = ROOT / "rdt_connect"
GENERATOR = CONNECT / "scripts/generate-embedded-rdt-voice.py"
VENDOR = CONNECT / "vendor/rdt-voice-r82-alpha"
MANIFEST = CONNECT / "src-tauri/resources/rdt_voice_web_manifest.json"


class EmbeddedVoiceResourceGeneratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = GENERATOR.read_text(encoding="utf-8")
        cls.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    def test_vendored_r82_snapshot_matches_all_62_manifest_hashes(self):
        expected = self.manifest["alpha_source_files"]
        self.assertEqual(len(expected), 62)
        actual = {
            path.relative_to(VENDOR).as_posix()
            for path in VENDOR.rglob("*") if path.is_file()
        }
        self.assertEqual(actual, set(expected))
        for name, digest in expected.items():
            path = VENDOR / name
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest, name)

    def test_generator_uses_root_assets_and_pinned_support_map(self):
        self.assertIn('root_assets = repo / "assets"', self.source)
        self.assertIn("connect_support_files", self.source)
        self.assertIn("alpha_source_files", self.source)
        self.assertIn("copy_file_verified", self.source)

    def test_generator_runs_runtime_preparer_and_r82_verifier(self):
        self.assertIn("prepare-offline-runtime.py", self.source)
        self.assertIn("verify-alpha-r82-staging.py", self.source)
        self.assertIn("generated_manifest != expected_manifest", self.source)
        self.assertIn("actual_files != recorded_files", self.source)

    def test_generator_checks_csp_oom_contract_and_promotes_transactionally(self):
        self.assertIn("check-embedded-rdt-voice.mjs", self.source)
        self.assertIn("prepare-embedded-rdt-voice.mjs", self.source)
        self.assertIn("previous-web", self.source)
        self.assertIn("previous-notices", self.source)
        self.assertIn("os.replace", self.source)

    def test_generator_is_not_tied_to_macos_packager(self):
        self.assertNotIn("prepare-macos-bundle.sh", self.source)
        self.assertNotIn("MLX", self.source)

    def test_contract_validation_generates_resources_before_whisper_tests(self):
        script = (ROOT / ".github/scripts/validate-platform-contracts.sh").read_text()
        self.assertLess(
            script.index("generate-embedded-rdt-voice.py"),
            script.index('run_suite "Whisper / hardware / release contracts"'),
        )

    def test_linux_generates_r82_before_rust_resource_tests(self):
        workflow = (ROOT / ".github/workflows/linux-native-qualification.yml").read_text()
        generator = workflow.index("Generate canonical R82 resources before Rust tests")
        rust_tests = workflow.index("Run shared Rust Linux TLS tests")
        package = workflow.index("Build Linux Debian and signed AppImage artifacts")
        self.assertLess(generator, rust_tests)
        self.assertLess(rust_tests, package)
        self.assertEqual(workflow.count("generate-embedded-rdt-voice.py"), 1)
        self.assertNotIn("Prepare OpenCC license notices for Rust tests", workflow)
    def test_generator_prepares_shared_opencc_and_runtime_notices_for_all_platforms(self):
        self.assertIn('repo / "licenses" / "third-party" / "opencc-js"', self.source)
        self.assertIn('staged_notices / "opencc-js"', self.source)
        self.assertIn('staged_notices / "rdt-offline-runtime"', self.source)
        self.assertIn('staged_notices / "BUILD-NOTICES.md"', self.source)
        self.assertIn("previous-opencc-notices", self.source)
        self.assertIn("previous-build-notices", self.source)
        linux = (ROOT / ".github/workflows/linux-native-qualification.yml").read_text()
        self.assertNotIn('cp -R licenses/third-party/opencc-js/.', linux)
        self.assertNotIn('cp index.html worker.js pcm-worklet.js models.manifest.json "$WEB/"', linux)
        self.assertNotIn('cp -R locales assets "$WEB/"', linux)
    def test_native_build_workflows_generate_resources_before_packaging(self):
        linux = (ROOT / ".github/workflows/linux-native-qualification.yml").read_text()
        x64 = (ROOT / ".github/workflows/windows-x64-installer.yml").read_text()
        arm64 = (ROOT / ".github/workflows/windows-arm64-installer.yml").read_text()
        self.assertLess(linux.index("generate-embedded-rdt-voice.py"),
                        linux.index("Build Linux Debian and signed AppImage artifacts"))
        self.assertLess(x64.index("generate-embedded-rdt-voice.py"), x64.index("Build frontend"))
        self.assertLess(arm64.index("generate-embedded-rdt-voice.py"), arm64.index("Build frontend"))

    def test_linux_generates_r82_before_python_resource_dependent_tests(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[3]
        workflow = (root / ".github/workflows/linux-native-qualification.yml").read_text(
            encoding="utf-8"
        )
        generator = "- name: Generate canonical R82 resources before Rust tests"
        regression = "- name: Run Linux server regression suite"

        self.assertEqual(workflow.count(generator), 1, "R82 generator step must exist exactly once")
        self.assertEqual(regression.count("- name:"), 1)
        self.assertEqual(workflow.count(regression), 1, "Linux regression step must exist exactly once")
        self.assertLess(
            workflow.index(generator),
            workflow.index(regression),
            "R82 resources must be generated before Python tests inspect embedded resources",
        )


if __name__ == "__main__":
    unittest.main()
