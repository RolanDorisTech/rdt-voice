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
        self.assertIn("Start-Process -FilePath $installer", step)
        self.assertIn("-Wait -PassThru", step)
        self.assertIn("'nvcc_13.5'", step)
        self.assertIn("'cudart_13.5'", step)
        self.assertIn("'cccl_13.5'", step)
        self.assertIn("'crt_13.5'", step)
        self.assertIn("CUDA_INSTALLER_EXIT_CODE", step)
        install_start = step.index("$installerProcess = Start-Process")
        install_end = step.index("$defaultRoot =", install_start)
        installer_section = step[install_start:install_end]
        self.assertNotIn("if ($LASTEXITCODE -ne 0)", installer_section)
        self.assertIn(
            "if ($installerProcess.ExitCode -notin @(0, 3010))",
            installer_section,
        )
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


    def test_arm64_cross_build_error_interpolation_is_valid(self):
        script = (ROOT / ".github/scripts/build-windows-arm64-cuda-worker.ps1").read_text()
        self.assertIn("${FileName}: $($process.ExitCode)", script)
        self.assertNotIn("for $FileName: $($process.ExitCode)", script)
