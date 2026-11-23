import unittest
from pathlib import Path


WORKER = (
    Path(__file__).resolve().parents[1]
    / "worker"
    / "rdt_whisper_worker.cpp"
)
ENGINE = Path(__file__).resolve().parents[1] / "engine.py"
LINUX_API = Path(__file__).resolve().parents[2] / "linux" / "server.py"
WINDOWS_API = Path(__file__).resolve().parents[2] / "windows" / "server.py"


class WorkerOutOfMemoryContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = WORKER.read_text(encoding="utf-8")
        cls.engine = ENGINE.read_text(encoding="utf-8")
        cls.linux_api = LINUX_API.read_text(encoding="utf-8")
        cls.windows_api = WINDOWS_API.read_text(encoding="utf-8")

    def test_uses_pinned_whisper_ggml_log_callback(self):
        self.assertIn("whisper_log_set(rdt_ggml_log_callback, nullptr)", self.source)

    def test_only_explicit_allocator_signals_set_oom_flag(self):
        for marker in (
            "cudaerrormemoryallocation",
            "cudamalloc failed: out of memory",
            "vk_error_out_of_device_memory",
            "vk_error_out_of_host_memory",
        ):
            self.assertIn(marker, self.source.lower())
        self.assertIn("g_device_oom.store(true", self.source)

    def test_model_load_oom_is_distinct_from_generic_load_failure(self):
        self.assertIn("model_load_out_of_memory", self.source)
        self.assertIn("model_load_failed", self.source)

    def test_inference_oom_is_distinct_and_cancellation_takes_priority(self):
        self.assertIn('"out_of_memory" : "inference_failed"', self.source)
        self.assertIn('cancelled ? "cancelled"', self.source)

    def test_original_backend_diagnostics_are_preserved(self):
        self.assertIn("std::cerr << message", self.source)

    def test_worker_stderr_is_not_discarded(self):
        self.assertIn("stderr=None", self.engine)
        self.assertNotIn("stderr=subprocess.DEVNULL", self.engine)


    def test_startup_oom_retries_same_model_once(self):
        self.assertIn("ctx = whisper_init_from_file_with_params", self.source)
        self.assertIn("STARTUP_ERROR\\\\tmodel_load_out_of_memory", self.source)
        self.assertIn('line.rstrip(b"\\r\\n") == b"STARTUP_ERROR\\tmodel_load_out_of_memory"', self.engine)
        self.assertIn('str(attempt_error) != "worker_model_load_out_of_memory"', self.engine)
        self.assertIn('raise RuntimeError("worker_model_load_out_of_memory")', self.engine)

    def test_inference_oom_retries_same_candidate_once_without_demotion(self):
        self.assertIn("oom_attempts = 0", self.engine)
        self.assertIn("oom_attempts += 1", self.engine)
        self.assertIn("if oom_attempts < 2:", self.engine)
        self.assertIn('raise RuntimeError("worker_out_of_memory")', self.engine)

    def test_both_apis_only_show_message_after_second_oom(self):
        for source in (self.linux_api, self.windows_api):
            self.assertIn('str(e) == "worker_out_of_memory"', source)
            self.assertIn("status_code=503", source)
            self.assertIn("ran out of memory twice using the same settings", source)



    def test_model_load_and_inference_oom_never_demote_gpu(self):
        import threading
        from types import SimpleNamespace
        from backend.whisper_cpp.engine import PersistentWhisperEngine

        for reason in (
            "worker_model_load_out_of_memory",
            "worker_out_of_memory",
        ):
            with self.subTest(reason=reason):
                engine = SimpleNamespace(
                    selection_mode="automatic",
                    worker_variant="cuda",
                    _bad_worker_paths=set(),
                    _fallback_reason=None,
                    _state_lock=threading.Lock(),
                    _active_id=None,
                    _cancel_requested_ids=set(),
                    _active_worker_path=None,
                )
                PersistentWhisperEngine._demote_failed_non_cpu_worker(
                    engine, reason
                )
                self.assertEqual(engine._bad_worker_paths, set())
                self.assertIsNone(engine._fallback_reason)


if __name__ == "__main__":
    unittest.main()
