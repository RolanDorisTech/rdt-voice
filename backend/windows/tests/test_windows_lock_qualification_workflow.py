from __future__ import annotations
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "windows-python-lock-qualification.yml"


class WindowsPythonLockQualificationWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")

    def test_is_manual_only_and_read_only(self):
        self.assertRegex(self.text, r"(?m)^on:\s*\n\s+workflow_dispatch:")
        self.assertNotRegex(self.text, r"(?m)^\s+(push|pull_request|schedule|workflow_call):")
        self.assertRegex(self.text, r"(?m)^permissions:\s*\n\s+contents:\s+read\s*$")
        self.assertIn("persist-credentials: false", self.text)
        self.assertNotIn("tauri build", self.text.lower())
        self.assertNotIn("create-release", self.text.lower())
        self.assertNotIn("gh release", self.text.lower())
        self.assertNotRegex(self.text, r"(?im)^\s*git\s+(push|commit)\b")

    def test_requires_exact_source_and_input_hash_confirmation(self):
        self.assertIn("expected_source_sha:", self.text)
        self.assertIn("expected_direct_requirements_sha256:", self.text)
        self.assertIn("^[0-9a-fA-F]{40}$", self.text)
        self.assertIn("^[0-9a-fA-F]{64}$", self.text)
        self.assertIn("SOURCE_SHA=$actualSha", self.text)
        self.assertIn("DIRECT_REQUIREMENTS_SHA256=$directHash", self.text)

    def test_uses_native_runners_and_exact_cpython(self):
        self.assertIn("runner: windows-11-arm", self.text)
        self.assertIn("runner: windows-2022", self.text)
        self.assertIn("python-version: '3.13.16'", self.text)
        self.assertIn("architecture: ${{ matrix.python-architecture }}", self.text)
        self.assertIn("NATIVE_ARCHITECTURE_QUALIFICATION=PASS", self.text)
        self.assertIn("TARGET_ARCH", self.text)

    def test_generates_fully_hashed_bounded_and_wheel_only_locks(self):
        for option in (
            "--generate-hashes", "--allow-unsafe", "--no-reuse-hashes",
            "--rebuild", "--upgrade", "--uploaded-prior-to=2026-10-23T00:00:00Z",
            "--pip-args=--only-binary=:all:", "--no-emit-index-url", "--no-config",
        ):
            self.assertIn(option, self.text)
        self.assertIn("requirements-windows-arm64.txt", self.text)
        self.assertIn("requirements-windows-x64.txt", self.text)
        self.assertIn("lock-file: requirements-windows-arm64.txt", self.text)
        self.assertIn("lock-file: requirements-windows-x64.txt", self.text)
        self.assertIn('$env:LOCK_FILE', self.text)
        self.assertIn('("requirements-windows-$arch.txt")', self.text)
        self.assertIn(
            "lock-candidates/${{ matrix.artifact-subdir }}/requirements-windows-${{ matrix.artifact-subdir }}.txt",
            self.text,
        )
        self.assertNotRegex(
            self.text,
            r"requirements-windows-(?:arm64|x64)\.lock\b",
        )
        self.assertIn("validate_windows_requirements_lock.py", self.text)

    def test_requires_hash_enforced_wheel_download_and_offline_install(self):
        self.assertIn("pip download", self.text)
        self.assertGreaterEqual(self.text.count("--require-hashes"), 2)
        self.assertGreaterEqual(self.text.count("--only-binary=:all:"), 2)
        self.assertIn("--no-index", self.text)
        self.assertIn("pip check", self.text)
        self.assertIn("INSTALLED_GRAPH_MATCHES_LOCK=PASS", self.text)
        self.assertIn("wheel-inventory.sha256", self.text)

    def test_no_installer_or_release_artifact_is_generated(self):
        self.assertIn("RDT-Windows-Python-Lock-Candidate-", self.text)
        self.assertNotIn("*-setup.exe", self.text)
        self.assertNotIn("latest.windows.json", self.text)
        self.assertIn("SCOPE=DEPENDENCY_LOCK_QUALIFICATION_ONLY_NO_INSTALLER_BUILD", self.text)


if __name__ == "__main__":
    unittest.main()
