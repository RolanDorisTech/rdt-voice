import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.whisper_cpp.engine import (
    PersistentWhisperEngine,
    _WorkerTranscriptionError,
    _first_start_file_sha256,
)


class RecordingBreaker:
    def __init__(self):
        self.failures = []

    def record_failure(self, context):
        self.failures.append(context)


class HardwareBreakerEngineIntegrationTests(unittest.TestCase):
    def test_context_binds_discovered_driver_runtime_and_selector(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = root / "rdt_whisper_worker_cuda"
            worker.write_bytes(b"fixture worker")
            model = root / "model.bin"
            model.write_bytes(b"fixture model")
            worker_hash = _first_start_file_sha256(worker)
            inventory = {
                "runtime_fingerprint": "f" * 64,
                "worker_build": "worker-build",
                "candidates": [{
                    "provider": "cuda",
                    "identity_kind": "pci-bdf",
                    "identity_value": "0000:01:00.0",
                    "worker_sha256": worker_hash,
                    "driver": "NVIDIA",
                    "driver_version": "555.42",
                    "runtime_version": "CUDA-13.5",
                    "hardware_signature": "gpu-signature",
                    "runtime_identity_kind": "cuda-runtime",
                    "runtime_identity_value": "13.5",
                    "runtime_selector_kind": "cuda-uuid",
                    "runtime_selector": "GPU-12345678-1234-1234-1234-123456789abc",
                }],
            }
            instance = object.__new__(PersistentWhisperEngine)
            instance._hardware_breaker = object()
            instance.model_path = model
            instance.model_id = "approved-model"
            ready = {
                "provider": "cuda",
                "identity_kind": "pci-bdf",
                "identity_value": "0000:01:00.0",
            }
            with patch(
                "backend.whisper_cpp.engine._current_first_start_runtime_identity",
                return_value=inventory,
            ):
                context = instance._build_hardware_breaker_context(worker, ready, None)

            self.assertEqual(context["runtime"]["driver_version"], "555.42")
            self.assertEqual(context["runtime"]["runtime_version"], "CUDA-13.5")
            self.assertEqual(context["runtime"]["runtime_fingerprint"], "f" * 64)
            self.assertEqual(context["selector"]["runtime_selector_kind"], "cuda-uuid")
            self.assertEqual(context["selector"]["runtime_selector"], inventory["candidates"][0]["runtime_selector"])

    def test_only_classified_accelerator_inference_fault_counts(self):
        context = {"hardware_identity": {"kind": "pci", "value": "gpu-0"}}

        def make_engine():
            instance = object.__new__(PersistentWhisperEngine)
            instance.selection_mode = "automatic"
            instance.worker_variant = "cuda"
            instance._state_lock = threading.Lock()
            instance._active_id = None
            instance._cancel_requested_ids = set()
            instance._active_worker_path = "/tmp/rdt_whisper_worker_cuda"
            instance._bad_worker_paths = set()
            instance._fallback_reason = None
            instance._hardware_breaker = RecordingBreaker()
            instance._active_hardware_breaker_context = context
            instance.stop = lambda: None
            return instance

        excluded = (
            RuntimeError("worker_out_of_memory"),
            RuntimeError("worker_transcription_timeout"),
            RuntimeError("request_cancelled"),
            _WorkerTranscriptionError("worker_invalid_audio"),
            RuntimeError("worker_auth_failed"),
            RuntimeError("unrelated_service_error"),
        )
        for reason in excluded:
            with self.subTest(reason=str(reason)):
                instance = make_engine()
                instance._demote_failed_non_cpu_worker(reason)
                self.assertEqual(instance._hardware_breaker.failures, [])

        instance = make_engine()
        instance._demote_failed_non_cpu_worker(
            _WorkerTranscriptionError("worker_inference_failed")
        )
        self.assertEqual(instance._hardware_breaker.failures, [context])


    def test_arm_dotprod_context_binds_worker_and_cpu_hardware_signature(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = root / "rdt_whisper_worker_arm_dotprod"
            worker.write_bytes(b"fixture ARM dotprod worker")
            worker.chmod(0o755)
            model = root / "model.bin"
            model.write_bytes(b"fixture model")
            worker_hash = _first_start_file_sha256(worker)
            hardware_signature = "arm64-v8.2-a-dotprod-fixture"
            identity_kind = "cpu-hardware-signature"
            identity_value = "arm64:fixture-cpu"
            inventory = {
                "runtime_fingerprint": "f" * 64,
                "worker_build": "worker-build",
                "candidates": [{
                    "provider": "cpu",
                    "identity_kind": identity_kind,
                    "identity_value": identity_value,
                    "worker_sha256": worker_hash,
                    "worker_variant": "arm-dotprod",
                    "hardware_signature": hardware_signature,
                }],
            }
            instance = object.__new__(PersistentWhisperEngine)
            instance._hardware_breaker = object()
            instance.model_path = model
            instance.model_id = "approved-model"
            ready = {
                "provider": "cpu",
                "identity_kind": identity_kind,
                "identity_value": identity_value,
            }
            with patch(
                "backend.whisper_cpp.engine._current_first_start_runtime_identity",
                return_value=inventory,
            ):
                context = instance._build_hardware_breaker_context(worker, ready, inventory["candidates"][0])

            self.assertEqual(context["provider"], "cpu")
            self.assertEqual(context["worker_variant"], "arm-dotprod")
            self.assertEqual(context["worker_sha256"], worker_hash)
            self.assertEqual(context["runtime"]["hardware_signature"], hardware_signature)
            self.assertEqual(context["runtime"]["runtime_fingerprint"], "f" * 64)
            from backend.whisper_cpp.hardware.circuit_breaker import _context_key
            self.assertEqual(len(_context_key(context)), 64)


if __name__ == "__main__":
    unittest.main()
