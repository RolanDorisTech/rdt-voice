import queue
import tempfile
import threading
import unittest
from pathlib import Path

from backend.whisper_cpp.engine import PersistentWhisperEngine, _encode


class FakeProcess:
    def poll(self):
        return None


class EngineTranscribeFailoverContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.audio = root / "fixture.wav"
        self.audio.write_bytes(b"controlled test audio")

        self.cuda = self._worker(root, "rdt_whisper_worker_cuda")
        self.vulkan = self._worker(root, "rdt_whisper_worker_vulkan")
        self.cpu = self._worker(root, "rdt_whisper_worker_cpu")
        self.candidates = [self.cuda, self.vulkan, self.cpu]

        self.engine = object.__new__(PersistentWhisperEngine)
        self.engine._request_lock = threading.Lock()
        self.engine._state_lock = threading.RLock()
        self.engine._active_id = None
        self.engine._cancel_requested_ids = set()
        self.engine._bad_worker_paths = set()
        self.engine._active_worker_path = None
        self.engine._active_hardware_breaker_context = None
        self.engine._active_first_start_profile_selection = None
        self.engine._first_start_profile_reverified = False
        self.engine._inference_verified = False
        self.engine._fallback_reason = None
        self.engine.worker_variant = "cuda"
        self.engine.selection_mode = "automatic"
        self.engine.process = None
        self.engine._lines = queue.Queue()
        self.engine.worker_candidates = lambda: list(self.candidates)

        self.start_calls = []
        self.send_calls = []
        self.demotions = []
        self.start_failures = {}
        self.send_behaviors = {}

        self.engine.start = self._start_worker
        self.engine.stop = self._stop_worker
        self.engine._send = self._send_request
        self.engine._record_successful_inference = lambda elapsed: None
        self.engine._demote_failed_non_cpu_worker = (
            lambda reason: self.demotions.append(str(reason))
        )

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _worker(root, name):
        path = root / name
        path.write_bytes(b"executable test double")
        path.chmod(0o755)
        return path

    def _start_worker(
        self,
        *,
        candidate_path,
        ready_timeout_seconds,
        mark_failed_candidate,
    ):
        key = str(Path(candidate_path).resolve())
        self.start_calls.append(key)
        failures = self.start_failures.get(key, [])
        if failures:
            error = failures.pop(0)
            raise error
        self.engine.process = FakeProcess()
        self.engine._active_worker_path = key
        self.engine._lines = queue.Queue()

    def _stop_worker(self):
        self.engine.process = None

    @staticmethod
    def _success(request_id, text=""):
        records = []
        if text:
            records.append(
                f"SEGMENT\t{request_id}\t0\t100\t{_encode(text)}\n".encode()
            )
        records.append(f"DONE\t{request_id}\t-\n".encode())
        return records

    @staticmethod
    def _failure(request_id, code, partial=None):
        records = []
        if partial:
            records.append(
                f"SEGMENT\t{request_id}\t0\t100\t{_encode(partial)}\n".encode()
            )
        records.append(f"ERROR\t{request_id}\t{code}\n".encode())
        return records

    def _send_request(self, line, activate_request_id=None):
        request_id = line.split("\t")[1]
        key = self.engine._active_worker_path
        self.send_calls.append(key)

        if key in self.engine._cancel_requested_ids:
            pass

        behaviors = self.send_behaviors.get(key, [])
        records = behaviors.pop(0) if behaviors else self._success(request_id)
        if callable(records):
            records = records(request_id)
        for record in records:
            self.engine._lines.put(record)

    def _run(self, request_id="contract_request", timeout_seconds=5.0):
        return self.engine.transcribe(
            self.audio,
            language="en",
            timeout_seconds=timeout_seconds,
            request_id=request_id,
        )

    def test_unavailable_candidate_is_skipped_without_restart(self):
        cuda_key = str(self.cuda.resolve())
        self.start_failures[cuda_key] = [
            RuntimeError("worker_driver_unavailable")
        ]

        result = self._run()

        self.assertEqual(
            self.start_calls,
            [cuda_key, str(self.vulkan.resolve())],
        )
        self.assertEqual(self.send_calls, [str(self.vulkan.resolve())])
        self.assertEqual(result["text"], "")
        self.assertIn(cuda_key, self.engine._bad_worker_paths)

    def test_restartable_start_failure_retries_same_candidate_once_then_falls_back(self):
        cuda_key = str(self.cuda.resolve())
        self.start_failures[cuda_key] = [
            RuntimeError("persistent_worker_ready_timeout"),
            RuntimeError("persistent_worker_ready_timeout"),
        ]

        result = self._run()

        self.assertEqual(
            self.start_calls,
            [cuda_key, cuda_key, str(self.vulkan.resolve())],
        )
        self.assertEqual(self.send_calls, [str(self.vulkan.resolve())])
        self.assertEqual(result["text"], "")

    def test_inference_failure_restarts_once_and_discards_partial_transcript(self):
        cuda_key = str(self.cuda.resolve())
        self.send_behaviors[cuda_key] = [
            lambda request_id: self._failure(
                request_id, "inference_failed", partial="partial-from-failed-worker"
            ),
            lambda request_id: self._failure(
                request_id, "inference_failed", partial="second-partial"
            ),
        ]
        vulkan_key = str(self.vulkan.resolve())
        self.send_behaviors[vulkan_key] = [
            lambda request_id: self._success(request_id, "verified-fallback")
        ]

        result = self._run()

        self.assertEqual(
            self.start_calls,
            [cuda_key, cuda_key, vulkan_key],
        )
        self.assertEqual(
            result["text"],
            "verified-fallback",
        )
        self.assertNotIn("partial", result["text"])
        self.assertEqual(len(self.demotions), 1)

    def test_invalid_audio_is_not_replayed_or_failed_over(self):
        cuda_key = str(self.cuda.resolve())
        self.send_behaviors[cuda_key] = [
            lambda request_id: self._failure(request_id, "invalid_audio")
        ]

        with self.assertRaisesRegex(RuntimeError, "worker_invalid_audio"):
            self._run()

        self.assertEqual(self.start_calls, [cuda_key])
        self.assertEqual(self.send_calls, [cuda_key])
        self.assertEqual(self.demotions, [])

    def test_cancellation_is_not_replayed_or_failed_over(self):
        cuda_key = str(self.cuda.resolve())

        def cancel_during_send(request_id):
            self.engine._cancel_requested_ids.add(request_id)
            return self._failure(request_id, "inference_failed")

        self.send_behaviors[cuda_key] = [cancel_during_send]

        with self.assertRaisesRegex(RuntimeError, "worker_inference_failed"):
            self._run()

        self.assertEqual(self.start_calls, [cuda_key])
        self.assertEqual(self.send_calls, [cuda_key])
        self.assertEqual(self.demotions, [])

    def test_oom_retries_same_worker_once_but_never_falls_back_to_other_provider(self):
        cuda_key = str(self.cuda.resolve())
        self.send_behaviors[cuda_key] = [
            lambda request_id: self._failure(request_id, "out_of_memory"),
            lambda request_id: self._failure(request_id, "out_of_memory"),
        ]

        with self.assertRaisesRegex(RuntimeError, "worker_out_of_memory"):
            self._run()

        self.assertEqual(self.start_calls, [cuda_key, cuda_key])
        self.assertEqual(self.send_calls, [cuda_key, cuda_key])
        self.assertEqual(self.demotions, [])


    def test_failure_walks_cuda_vulkan_then_verified_cpu(self):
        cuda_key = str(self.cuda.resolve())
        vulkan_key = str(self.vulkan.resolve())
        cpu_key = str(self.cpu.resolve())

        self.send_behaviors[cuda_key] = [
            lambda request_id: self._failure(request_id, "inference_failed"),
            lambda request_id: self._failure(request_id, "inference_failed"),
        ]
        self.send_behaviors[vulkan_key] = [
            lambda request_id: self._failure(request_id, "inference_failed"),
            lambda request_id: self._failure(request_id, "inference_failed"),
        ]
        self.send_behaviors[cpu_key] = [
            lambda request_id: self._success(request_id, "verified-cpu-result")
        ]

        result = self._run()

        self.assertEqual(
            self.start_calls,
            [cuda_key, cuda_key, vulkan_key, vulkan_key, cpu_key],
        )
        self.assertEqual(
            self.send_calls,
            [cuda_key, cuda_key, vulkan_key, vulkan_key, cpu_key],
        )
        self.assertEqual(result["text"], "verified-cpu-result")
        self.assertEqual(len(self.demotions), 2)

    def test_trickling_ipc_cannot_extend_absolute_request_deadline(self):
        import time

        stop_stream = threading.Event()
        started_streams = []
        emitted = []

        def trickle_send(line, activate_request_id=None):
            request_id = line.split("\t")[1]
            started_streams.append(request_id)
            with self.engine._state_lock:
                self.engine._active_id = activate_request_id

            def producer():
                for index in range(100):
                    if stop_stream.is_set():
                        return
                    record = (
                        f"SEGMENT\t{request_id}\t{index}\t{index + 1}\t"
                        f"{_encode('x')}\n"
                    ).encode()
                    self.engine._lines.put(record)
                    emitted.append(index)
                    time.sleep(0.025)

            threading.Thread(target=producer, daemon=True).start()

        original_stop = self.engine.stop

        def stop_and_end_stream():
            stop_stream.set()
            original_stop()

        self.engine._send = trickle_send
        self.engine.stop = stop_and_end_stream
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(RuntimeError, "worker_transcription_timeout"):
                self._run(timeout_seconds=0.16)
        finally:
            stop_stream.set()

        elapsed = time.monotonic() - started
        self.assertEqual(len(started_streams), 1)
        self.assertGreater(len(emitted), 1)
        self.assertLess(len(emitted), 100)
        self.assertLess(elapsed, 0.75)


if __name__ == "__main__":
    unittest.main()
