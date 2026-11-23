import json
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from backend.whisper_cpp import stage_cuda_runtime as stager


class CudaRuntimeStagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cuda = self.root / "cuda"
        self.doc = self.cuda / "doc"
        self.doc.mkdir(parents=True)
        (self.doc / "EULA.txt").write_text("CUDA toolkit license fixture\n")
        self.destination = self.root / "bundle" / "cuda-runtime"
        self.destination.parent.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_linux_stages_toolkit_runtime_and_excludes_driver(self):
        lib_dir = self.cuda / "targets" / "x86_64-linux" / "lib"
        lib_dir.mkdir(parents=True)
        cudart = lib_dir / "libcudart.so.13"
        cudart.write_bytes(b"CUDA runtime fixture")
        worker = self.root / "rdt_whisper_worker_cuda"
        worker.write_bytes(b"ELF worker fixture")
        output = (
            f"libcudart.so.13 => {cudart} (0x0001)\n"
            "libcuda.so.1 => not found\n"
            "libc.so.6 => /usr/lib/libc.so.6 (0x0002)\n"
        )
        with patch.object(
            stager.subprocess, "run",
            return_value=CompletedProcess(["ldd"], 0, output, ""),
        ):
            manifest_path = stager.stage_linux([worker], self.cuda, self.destination)
        self.assertTrue((self.destination / "libcudart.so.13").is_file())
        self.assertFalse((self.destination / "libcuda.so.1").exists())
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(manifest["dependencies"], ["libcudart.so.13"])
        self.assertTrue(manifest["licenses"])
        self.assertTrue((self.destination / "NOTICE.md").is_file())

    def test_linux_rejects_missing_non_driver_runtime(self):
        worker = self.root / "worker"
        worker.write_bytes(b"ELF fixture")
        output = "libcublas.so.13 => not found\n"
        with patch.object(
            stager.subprocess, "run",
            return_value=CompletedProcess(["ldd"], 0, output, ""),
        ):
            with self.assertRaisesRegex(RuntimeError, "unresolved"):
                stager.stage_linux([worker], self.cuda, self.destination)

    def test_linux_rejects_cuda_runtime_resolved_outside_toolkit(self):
        outside = self.root / "outside"
        outside.mkdir()
        cudart = outside / "libcudart.so.13"
        cudart.write_bytes(b"outside CUDA runtime")
        worker = self.root / "worker"
        worker.write_bytes(b"ELF fixture")
        output = f"libcudart.so.13 => {cudart} (0x0001)\n"
        with patch.object(
            stager.subprocess, "run",
            return_value=CompletedProcess(["ldd"], 0, output, ""),
        ):
            with self.assertRaisesRegex(RuntimeError, "outside toolkit root"):
                stager.stage_linux([worker], self.cuda, self.destination)

    def test_windows_recursively_stages_toolkit_dlls_not_driver(self):
        cuda_bin = self.cuda / "bin"
        cuda_bin.mkdir()
        worker = self.root / "rdt_whisper_worker_cuda.exe"
        worker.write_bytes(b"PE worker fixture")
        cudart = cuda_bin / "cudart64_13.dll"
        cublas = cuda_bin / "cublas64_13.dll"
        cublas_lt = cuda_bin / "cublasLt64_13.dll"
        for item in (cudart, cublas, cublas_lt):
            item.write_bytes(item.name.encode())

        def imports(_dumpbin, binary):
            name = Path(binary).name.lower()
            return {
                worker.name.lower(): ["cudart64_13.dll", "nvcuda.dll"],
                cudart.name.lower(): ["cublas64_13.dll"],
                cublas.name.lower(): ["cublasLt64_13.dll"],
                cublas_lt.name.lower(): [],
            }.get(name, [])

        with (
            patch.object(stager, "find_dumpbin", return_value="dumpbin"),
            patch.object(stager, "dumpbin_imports", side_effect=imports),
        ):
            manifest_path = stager.stage_windows([worker], self.cuda, self.destination)

        for item in (cudart, cublas, cublas_lt):
            self.assertTrue((self.destination / item.name).is_file())
        self.assertFalse((self.destination / "nvcuda.dll").exists())
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(len(manifest["dependencies"]), 3)
        self.assertTrue(manifest["licenses"])

    def test_notice_attachment_copies_manifest_and_license(self):
        lib_dir = self.cuda / "lib64"
        lib_dir.mkdir()
        cudart = lib_dir / "libcudart.so.13"
        cudart.write_bytes(b"CUDA runtime fixture")
        worker = self.root / "worker"
        worker.write_bytes(b"ELF fixture")
        output = f"libcudart.so.13 => {cudart} (0x0001)\n"
        with patch.object(
            stager.subprocess, "run",
            return_value=CompletedProcess(["ldd"], 0, output, ""),
        ):
            stager.stage_linux([worker], self.cuda, self.destination)
        target = stager.attach_notices(self.destination, self.root / "notices")
        self.assertTrue((target / "NOTICE.md").is_file())
        self.assertTrue((target / "cuda-runtime-manifest.json").is_file())
        self.assertTrue(list((target / "licenses").rglob("*")))


    def test_external_cuda_license_directory_is_included(self):
        external = self.root / "external-cuda-license"
        external.mkdir()
        (external / "EULA.txt").write_text("External CUDA license fixture\n")
        lib_dir = self.cuda / "lib64"
        lib_dir.mkdir()
        cudart = lib_dir / "libcudart.so.13"
        cudart.write_bytes(b"CUDA runtime fixture")
        worker = self.root / "worker"
        worker.write_bytes(b"ELF fixture")
        output = f"libcudart.so.13 => {cudart} (0x0001)\n"

        with patch.dict("os.environ", {"CUDA_LICENSE_DIR": str(external)}):
            with patch.object(
                stager.subprocess, "run",
                return_value=CompletedProcess(["ldd"], 0, output, ""),
            ):
                manifest_path = stager.stage_linux(
                    [worker], self.cuda, self.destination
                )

        manifest = json.loads(manifest_path.read_text())
        entries = [
            name for name in manifest["licenses"]
            if name.startswith("licenses/external/configured/")
        ]
        self.assertTrue(entries)
        for name in entries:
            self.assertTrue((self.destination / name).is_file())


    def test_cuda_runtime_notice_manifest_merge_and_bundle_copy(self):
        import hashlib
        import json

        runtime = self.destination
        runtime.mkdir(parents=True)
        license_file = runtime / "licenses" / "EULA.txt"
        license_file.parent.mkdir(parents=True)
        license_file.write_text("CUDA license fixture\n")
        (runtime / "NOTICE.md").write_text("CUDA notice fixture\n")
        runtime_manifest = {
            "platform": "linux",
            "dependencies": [],
            "licenses": ["licenses/EULA.txt"],
        }
        (runtime / "cuda-runtime-manifest.json").write_text(
            json.dumps(runtime_manifest)
        )

        notices = self.root / "shared-notices"
        notices.mkdir()
        existing = notices / "existing-license.txt"
        existing.write_text("Existing package notice\n")
        prior_manifest = {
            "schema": "rdt-third-party-notices-v1",
            "files": [{
                "path": existing.name,
                "sha256": hashlib.sha256(existing.read_bytes()).hexdigest(),
                "size_bytes": existing.stat().st_size,
            }],
            "notice_file_count": 1,
        }
        (notices / "NOTICE-MANIFEST.json").write_text(
            json.dumps(prior_manifest)
        )
        (notices / "BUILD-NOTICES.md").write_text("# Existing notices\n")

        bundle = self.root / "backend-bundle"
        stager.attach_notices(runtime, notices, bundle)
        result = json.loads(
            (notices / "NOTICE-MANIFEST.json").read_text()
        )
        entries = {entry["path"]: entry for entry in result["files"]}

        self.assertIn(existing.name, entries)
        self.assertIn(
            "cuda-runtime-notices/cuda-runtime-manifest.json", entries
        )
        self.assertIn("cuda-runtime-notices/NOTICE.md", entries)
        license_key = "cuda-runtime-notices/licenses/EULA.txt"
        self.assertIn(license_key, entries)
        self.assertEqual(
            entries[license_key]["sha256"],
            hashlib.sha256(license_file.read_bytes()).hexdigest(),
        )
        self.assertEqual(result["notice_file_count"], len(result["files"]))
        self.assertFalse(
            result["cuda_runtime"]["driver_libraries_redistributed"]
        )
        self.assertTrue(
            (bundle / "cuda-runtime-notices/cuda-runtime-manifest.json").is_file()
        )

        stager.attach_notices(runtime, notices)
        build_text = (notices / "BUILD-NOTICES.md").read_text()
        self.assertEqual(build_text.count("## CUDA Toolkit runtime"), 1)


if __name__ == "__main__":
    unittest.main()
