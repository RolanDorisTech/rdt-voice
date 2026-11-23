import queue
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.whisper_cpp import engine as engine_module


class SelectedProfileFallbackTests(unittest.TestCase):
    def test_changed_selected_gpu_hash_is_skipped_and_cpu_starts(self):
        with tempfile.TemporaryDirectory(prefix="rdt-profile-fallback-") as td:
            root = Path(td)
            gpu = root / "rdt_whisper_worker_cuda"
            cpu = root / "rdt_whisper_worker"
            gpu.write_bytes(b"changed GPU worker")
            cpu.write_bytes(b"packaged CPU worker")
            gpu.chmod(0o755)
            cpu.chmod(0o755)

            engine = engine_module.PersistentWhisperEngine.__new__(
                engine_module.PersistentWhisperEngine
            )
            engine.process = None
            engine.model_path = root / "model.bin"
            engine.model_id = "test-model"
            engine.selection_mode = "automatic"
            engine.requested_provider = "auto"
            engine.provider_preference = "auto"
            engine._worker_override = False
            engine._first_start_profile_selection = {
                "worker_path": str(gpu),
                "worker_sha256": "0" * 64,
            }
            engine._first_start_profile_rejection = None
            engine._active_first_start_profile_selection = None
            engine._first_start_profile_reverified = False
            engine._bad_worker_paths = set()
            engine._active_worker_path = None
            engine._fallback_reason = None
            engine._lines = queue.Queue(maxsize=16)
            engine.stop = lambda: setattr(engine, "process", None)

            launched = []
            ready = (
                b"READY\tx64\twhisper.cpp/v1.9.5@"
                b"d1be6fde11ac6e0407606b4e42fe72d34add8037\tCPU\tCPU\n"
            )

            def fake_popen(command, **kwargs):
                name = Path(command[0]).name
                launched.append(name)
                engine._lines.put(ready)
                process = Mock()
                process.poll.return_value = None
                process.stdin = object()
                process.stdout = object()
                return process

            with (
                patch.object(
                    engine, "worker_candidates", return_value=[gpu, cpu]
                ),
                patch.object(
                    engine_module.subprocess, "Popen", side_effect=fake_popen
                ),
                patch.object(engine_module.threading, "Thread") as thread_class,
                patch.object(engine_module.platform, "machine", return_value="x86_64"),
            ):
                thread_class.return_value.start.return_value = None
                engine.start()

            self.assertEqual(launched, [cpu.name])
            self.assertEqual(engine.worker_variant, "cpu")
            self.assertIn(str(gpu.resolve()), engine._bad_worker_paths)
            self.assertIsNone(engine._active_first_start_profile_selection)
            self.assertTrue(engine._fallback_reason)
            self.assertIsNotNone(engine.process)


if __name__ == "__main__":
    unittest.main()
