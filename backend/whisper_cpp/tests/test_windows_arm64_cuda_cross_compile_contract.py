from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[3]


class WindowsArm64CudaCrossCompileContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = (ROOT / ".github/workflows/windows-arm64-installer.yml").read_text(encoding="utf-8")
        cls.cross_script = (ROOT / ".github/scripts/build-windows-arm64-cuda-worker.ps1").read_text(encoding="utf-8")
        cls.bundle = (ROOT / "rdt_connect/scripts/prepare-windows-bundle.ps1").read_text(encoding="utf-8")
        cls.stager = (ROOT / "backend/whisper_cpp/stage_cuda_runtime.py").read_text(encoding="utf-8")

    def test_cross_worker_is_built_as_a_separate_x64_host_job(self):
        self.assertIn("build-cuda-arm64-worker:", self.workflow)
        self.assertIn("runs-on: windows-2022", self.workflow)
        self.assertIn("needs: [signing-preflight, build-cuda-arm64-worker]", self.workflow)
        self.assertIn("needs: [signing-preflight, build-cuda-arm64-worker]", self.workflow)
        self.assertIn("uses: actions/download-artifact@v7.0.2", self.workflow)
        self.assertIn("name: rdt-windows-arm64-cuda-worker", self.workflow)
        self.assertIn("rdt-windows-arm64-cuda-worker", self.workflow)
        self.assertNotIn("gh run download", self.workflow)
        self.assertIn("actions/download-artifact@v7.0.2", self.workflow)
        self.assertIn("actions/upload-artifact@v7.0.2", self.workflow)

    def test_nvidia_cross_toolchain_is_pinned_and_arm64_outputs_are_verified(self):
        for value in (
            "cuda_13.5.0_windows_x86_64.exe",
            "cuda_cross_arm64_13.5.0_windows.exe",
            "cudart_cross_13.5",
            "nvcc_cross_13.5",
            "cublas_cross_13.5",
            "lib/arm64",
            "Hostx64",
            "arm64",
            "verify_engine_checkout.py",
            "0xAA64",
            "Get-FileHash",
        ):
            self.assertIn(value, self.cross_script)

    def test_native_arm64_job_keeps_runtime_staging_and_uses_prebuilt_worker(self):
        runtime_start = self.workflow.index(
            "- name: Install and verify native ARM64 CUDA runtime"
        )
        runtime_end = self.workflow.index(
            "- name: Prepare native Windows backend and web resources", runtime_start
        )
        runtime_step = self.workflow[runtime_start:runtime_end]
        self.assertIn("cudart_13.5", runtime_step)
        self.assertIn("cublas_13.5", runtime_step)
        self.assertIn("0xAA64", runtime_step)
        self.assertNotIn("nvcc_13.5", runtime_step)
        self.assertIn("-PrebuiltCudaWorker", self.workflow)
        self.assertIn("-PrebuiltCudaWorkerManifest", self.workflow)
        self.assertIn("-RequireGpuWorker", self.workflow)

    def test_bundle_packager_validates_and_stages_the_pinned_prebuilt_worker(self):
        for value in (
            "PrebuiltCudaWorker",
            "PrebuiltCudaWorkerManifest",
            "rdt-windows-arm64-cuda-worker-v1",
            "0xAA64",
            "whisper_cpp_commit",
            "SHA256",
            "RDT-CUDA-WORKER-PROVENANCE.json",
            "rdt_whisper_worker_cuda.exe",
        ):
            self.assertIn(value, self.bundle)
        self.assertIn("if ($EnableCuda -and -not $hasPrebuiltCudaWorker", self.bundle)
        self.assertIn("Build-WorkerVariant -Variant 'cuda'", self.bundle)

    def test_runtime_stager_searches_nested_bins_and_rejects_wrong_pe_architecture(self):
        self.assertIn("def read_pe_machine", self.stager)
        self.assertIn("cuda_bin.rglob", self.stager)
        self.assertIn("expected_machine", self.stager)
        self.assertIn("resolve_windows_dll(name, search_dirs, expected_machine)", self.stager)


if __name__ == "__main__":
    unittest.main()


class WindowsArm64CudaArtifactHandoffContractTests(unittest.TestCase):
    def test_package_downloads_same_run_artifact_without_gh_cli(self):
        from pathlib import Path
        workflow = Path(".github/workflows/windows-arm64-installer.yml").read_text(encoding="utf-8")
        import re
        package_job_match = re.search(
            r"(?ms)^  package:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
            workflow,
        )
        self.assertIsNotNone(package_job_match, "ARM64 package job missing")
        package_job = package_job_match.group("body")
        self.assertIn("uses: actions/checkout@v7.0.1", package_job)
        self.assertIn("uses: actions/download-artifact@v7.0.2", package_job)
        self.assertLess(
            package_job.index("uses: actions/checkout@v7.0.1"),
            package_job.index("uses: actions/download-artifact@v7.0.2"),
        )
        workflow = Path(".github/workflows/windows-arm64-installer.yml").read_text(
            encoding="utf-8"
        )
        package_match = re.search(r"(?ms)^  package:\s*\n(?P<package>.*?)(?=^  [A-Za-z0-9_-]+:\s*$|\Z)", workflow)
        self.assertIsNotNone(package_match)
        package = package_match.group("package")
        self.assertIn("uses: actions/download-artifact@v7.0.2", package)
        self.assertIn("name: rdt-windows-arm64-cuda-worker", package)
        self.assertIn("RDT_PREBUILT_CUDA_WORKER=$worker", package)
        self.assertIn("RDT_PREBUILT_CUDA_WORKER_MANIFEST=$manifest", package)
        self.assertNotIn("gh run download", package)
        self.assertIn(
            "name: rdt-windows-arm64-cuda-worker",
            workflow.split("  build-cuda-arm64-worker:", 1)[1].split(
                "  signing-preflight:", 1
            )[0],
        )

    def test_cross_build_uses_typed_arm64_cuda_cmake_paths_and_target(self):
        builder = Path(
            ".github/scripts/build-windows-arm64-cuda-worker.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("-DCMAKE_CUDA_HOST_COMPILER:FILEPATH=$clPath", builder)
        self.assertIn("-DCUDAToolkit_LIBRARY_DIR:PATH=$cudaLib", builder)
        self.assertIn("-DCUDA_cublas_LIBRARY:FILEPATH=$cudaLib/cublas.lib", builder)
        self.assertIn("--target rdt_whisper_worker", builder)
        self.assertIn("Hostx64", builder)
        self.assertIn("arm64", builder)
