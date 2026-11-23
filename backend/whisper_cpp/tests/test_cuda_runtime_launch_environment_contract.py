import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.whisper_cpp import engine as engine_module


class CudaRuntimeLaunchEnvironmentTests(unittest.TestCase):
    def test_linux_cuda_worker_gets_sibling_runtime_library_path(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = Path(temp)
            worker = backend / "rdt_whisper_worker_cuda"
            worker.write_bytes(b"worker fixture")
            runtime = backend / "cuda-runtime"
            runtime.mkdir()

            with (
                patch.object(engine_module, "_worker_variant", return_value="cuda"),
                patch.object(engine_module.sys, "platform", "linux"),
                patch.dict(os.environ, {"LD_LIBRARY_PATH": "/existing/libs"}, clear=True),
            ):
                env = engine_module._worker_launch_environment(worker)

            actual_paths = env["LD_LIBRARY_PATH"].split(os.pathsep)
            self.assertEqual(Path(actual_paths[0]).resolve(), runtime.resolve())
            self.assertEqual(os.pathsep.join(actual_paths[1:]), "/existing/libs")

    def test_windows_cuda_worker_gets_sibling_runtime_path(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = Path(temp)
            worker = backend / "rdt_whisper_worker_cuda.exe"
            worker.write_bytes(b"worker fixture")
            runtime = backend / "cuda-runtime"
            runtime.mkdir()
            existing_path = r"C:\Windows\System32"

            class WindowsOSProxy:
                name = "nt"
                pathsep = ";"

                def __getattr__(self, attribute):
                    return getattr(os, attribute)

            with (
                patch.object(engine_module, "_worker_variant", return_value="cuda"),
                patch.object(engine_module, "os", WindowsOSProxy()),
                patch.dict(os.environ, {"PATH": existing_path}, clear=True),
            ):
                env = engine_module._worker_launch_environment(worker)

            actual_paths = env["PATH"].split(";")
            self.assertEqual(Path(actual_paths[0]).resolve(), runtime.resolve())
            self.assertEqual(";".join(actual_paths[1:]), existing_path)

    def test_cpu_worker_does_not_receive_cuda_runtime_path(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = Path(temp)
            worker = backend / "rdt_whisper_worker"
            worker.write_bytes(b"worker fixture")
            (backend / "cuda-runtime").mkdir()

            with (
                patch.object(engine_module, "_worker_variant", return_value="cpu"),
                patch.object(engine_module.sys, "platform", "linux"),
                patch.dict(os.environ, {"LD_LIBRARY_PATH": "/existing/libs"}, clear=True),
            ):
                env = engine_module._worker_launch_environment(worker)

            self.assertEqual(env["LD_LIBRARY_PATH"], "/existing/libs")


if __name__ == "__main__":
    unittest.main()
