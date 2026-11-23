import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


class WindowsCudaToolkitInstallContractTests(unittest.TestCase):
    def test_download_artifact_uses_existing_release(self):
        arm = (ROOT / ".github/workflows/windows-arm64-installer.yml").read_text()
        self.assertIn("actions/download-artifact@v7.0.0", arm)
        self.assertNotIn("actions/download-artifact@v7.0.2", arm)
        self.assertIn("actions/upload-artifact@v7.0.2", arm)

    def test_x64_installs_full_toolkit_and_verifies_nvcc(self):
        s = (ROOT / ".github/workflows/windows-x64-installer.yml").read_text()
        step = s.split("- name: Install CUDA 13.5 toolkit", 1)[1].split(
            "\n      - name:", 1
        )[0]
        self.assertIn("& $installer -s", step)
        self.assertNotIn("& $installer -s nvcc_13.5", step)
        self.assertIn("nvcc.exe", step)
        self.assertIn("CUDA_NVCC_PATH", step)
        self.assertIn("CUDA_PATH=$cudaRoot", step)

    def test_arm64_package_requires_runtime_not_compiler(self):
        s = (ROOT / ".github/workflows/windows-arm64-installer.yml").read_text()
        step = s.split(
            "- name: Install and verify native ARM64 CUDA runtime (no compiler or driver)",
            1
        )[1].split("\n      - name:", 1)[0]
        self.assertNotIn("nvcc.exe", step.lower())
        self.assertNotIn("nvcc_13.5", step)
