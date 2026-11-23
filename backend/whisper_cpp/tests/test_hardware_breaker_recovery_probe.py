import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.whisper_cpp.engine import PersistentWhisperEngine, _first_start_file_sha256


class FakeBreaker:
    def __init__(self, due):
        self.due = due
        self.cleared = []

    def recovery_probes_due(self):
        return list(self.due)

    @staticmethod
    def same_context(left, right):
        return left == right

    def clear_after_probe(self, context, passed):
        self.cleared.append((context, passed))
        return passed


class HardwareRecoveryProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.worker = root / "rdt_whisper_worker_cuda"
        self.worker.write_bytes(b"test packaged worker")
        self.worker.chmod(0o755)
        self.model = root / "model.bin"
        self.model.write_bytes(b"test model")
        self.fixture = root / "audio.wav"
        self.fixture.write_bytes(b"pinned-fixture-test-double")

        self.context = {
            "hardware_identity": {"kind": "pci-bdf", "value": "0000:01:00.0"},
            "provider": "cuda",
            "worker_variant": "cuda",
            "worker_sha256": _first_start_file_sha256(self.worker),
            "runtime": {
                "driver_version": "test-driver",
                "runtime_version": "CUDA-test",
                "runtime_fingerprint": "f" * 64,
            },
            "selector": {
                "runtime_selector_kind": "cuda-ordinal",
                "runtime_selector": "0",
            },
            "model": {"id": "approved-model", "sha256": _first_start_file_sha256(self.model)},
        }
        self.candidate = {
            "candidate_id": "gpu-candidate-1",
            "provider": "cuda",
            "identity_kind": "pci-bdf",
            "identity_value": "0000:01:00.0",
            "worker_path": str(self.worker),
            "worker_sha256": _first_start_file_sha256(self.worker),
            "runtime_selector_kind": "cuda-ordinal",
            "runtime_selector": "0",
            "driver_version": "test-driver",
            "runtime_version": "CUDA-test",
            "eligible": True,
        }
        self.inventory = {
            "candidates": [self.candidate],
            "runtime_fingerprint": "runtime-fingerprint",
            "worker_build": "worker-build",
        }
        self.engine = object.__new__(PersistentWhisperEngine)
        self.engine._hardware_breaker = FakeBreaker([self.context])
        self.engine._recovery_probe_context = None
        self.engine.model_path = self.model
        self.engine.model_id = "approved-model"

    def tearDown(self):
        self.temp.cleanup()

    def _run_probe(self, runner_result=None, runner_error=None, inventory_sequence=None):
        result = runner_result or {
            "candidate_id": self.candidate["candidate_id"],
            "actual_provider": self.candidate["provider"],
            "identity_kind": self.candidate["identity_kind"],
            "identity_value": self.candidate["identity_value"],
            "model_sha256": _first_start_file_sha256(self.model),
            "fixture_sha256": _first_start_file_sha256(self.fixture),
            "inference_times_ms": [100.0, 101.0],
        }
        with (
            patch(
                "backend.whisper_cpp.engine._current_first_start_runtime_identity",
                side_effect=inventory_sequence if inventory_sequence is not None else None,
                return_value=self.inventory,
            ),
            patch("backend.whisper_cpp.engine.PersistentWhisperEngine._build_hardware_breaker_context", return_value=self.context),
            patch("backend.whisper_cpp.hardware.first_start.locate_manual_fixture", return_value=self.fixture),
            patch("backend.whisper_cpp.hardware.runner.run_candidate_benchmark", side_effect=runner_error or None, return_value=result) as runner,
        ):
            self.engine._run_due_hardware_recovery_probes()
        return runner

    def test_matching_due_candidate_runs_pinned_fixture_and_clears(self):
        runner = self._run_probe()
        self.assertEqual(len(runner.call_args_list), 1)
        self.assertEqual(runner.call_args.kwargs["recovery_probe_context"], self.context)
        self.assertEqual(runner.call_args.args[3], self.fixture)
        self.assertEqual(self.engine._hardware_breaker.cleared, [(self.context, True)])

    def test_failed_inference_keeps_candidate_quarantined(self):
        runner = self._run_probe(runner_error=RuntimeError("probe failed"))
        self.assertEqual(len(runner.call_args_list), 1)
        self.assertEqual(self.engine._hardware_breaker.cleared, [(self.context, False)])

    def test_no_due_quarantine_does_not_run_probe(self):
        self.engine._hardware_breaker.due = []
        with patch("backend.whisper_cpp.hardware.runner.run_candidate_benchmark") as runner:
            self.engine._run_due_hardware_recovery_probes()
        runner.assert_not_called()
        self.assertEqual(self.engine._hardware_breaker.cleared, [])

    def test_ambiguous_identity_does_not_probe_or_clear(self):
        self.inventory["candidates"].append(dict(self.candidate))
        with patch("backend.whisper_cpp.engine._current_first_start_runtime_identity", return_value=self.inventory), patch("backend.whisper_cpp.hardware.first_start.locate_manual_fixture", return_value=self.fixture), patch("backend.whisper_cpp.hardware.runner.run_candidate_benchmark") as runner:
            with patch.object(self.engine, "_build_hardware_breaker_context", return_value=self.context):
                self.engine._run_due_hardware_recovery_probes()
        runner.assert_not_called()
        self.assertEqual(self.engine._hardware_breaker.cleared, [])



    def test_runtime_change_during_probe_prevents_quarantine_clear(self):
        changed = {
            "candidates": [dict(self.candidate, driver_version="new-driver")],
            "runtime_fingerprint": "new-runtime-fingerprint",
            "worker_build": "worker-build",
        }
        self._run_probe(inventory_sequence=[self.inventory, changed])
        self.assertEqual(self.engine._hardware_breaker.cleared, [(self.context, False)])

    def test_missing_runtime_fingerprint_is_not_a_breaker_identity(self):
        from backend.whisper_cpp.hardware.circuit_breaker import _context_key
        incomplete = dict(self.context)
        incomplete["runtime"] = {
            "driver_version": "test-driver",
            "runtime_version": "CUDA-test",
        }
        with self.assertRaises(ValueError):
            _context_key(incomplete)

    def _configure_arm_dotprod(self):
        self.worker = Path(self.temp.name) / "rdt_whisper_worker_arm_dotprod"
        self.worker.write_bytes(b"arm dotprod fixture worker")
        self.worker.chmod(0o755)
        worker_hash = _first_start_file_sha256(self.worker)
        identity_kind = "cpu-hardware-signature"
        identity_value = "arm64:fixture-cpu"
        hardware_signature = "arm64-v8.2-a-dotprod-fixture"
        self.context = {
            "hardware_identity": {"kind": identity_kind, "value": identity_value},
            "provider": "cpu",
            "worker_variant": "arm-dotprod",
            "worker_sha256": worker_hash,
            "runtime": {
                "hardware_signature": hardware_signature,
                "runtime_fingerprint": "f" * 64,
            },
            "selector": {},
            "model": {
                "id": "approved-model",
                "sha256": _first_start_file_sha256(self.model),
            },
        }
        self.candidate = {
            "candidate_id": "arm-dotprod-candidate-1",
            "provider": "cpu",
            "worker_variant": "arm-dotprod",
            "identity_kind": identity_kind,
            "identity_value": identity_value,
            "hardware_signature": hardware_signature,
            "worker_path": str(self.worker),
            "worker_sha256": worker_hash,
            "eligible": True,
        }
        self.inventory = {
            "candidates": [self.candidate],
            "runtime_fingerprint": "f" * 64,
            "worker_build": "worker-build",
        }
        self.engine._hardware_breaker = FakeBreaker([self.context])

    def test_arm_dotprod_recovery_probe_passes_and_clears_exact_candidate(self):
        self._configure_arm_dotprod()
        runner = self._run_probe()
        self.assertEqual(runner.call_count, 1)
        candidate, context, *_ = runner.call_args.args
        self.assertEqual(candidate["provider"], "cpu")
        self.assertEqual(candidate["worker_variant"], "arm-dotprod")
        self.assertEqual(candidate["worker_sha256"], self.context["worker_sha256"])
        self.assertEqual(context["model_id"], "approved-model")
        self.assertEqual(
            self.engine._hardware_breaker.cleared,
            [(self.context, True)],
        )

    def test_failed_arm_dotprod_probe_keeps_candidate_quarantined(self):
        self._configure_arm_dotprod()
        self._run_probe(runner_error=RuntimeError("dotprod probe failed"))
        self.assertEqual(
            self.engine._hardware_breaker.cleared,
            [(self.context, False)],
        )

    def test_portable_cpu_worker_is_excluded_from_recovery_probe(self):
        portable = Path(self.temp.name) / "rdt_whisper_worker_cpu"
        portable.write_bytes(b"portable cpu fixture worker")
        portable.chmod(0o755)
        self.candidate = {
            "candidate_id": "portable-cpu-1",
            "provider": "cpu",
            "worker_variant": "cpu",
            "identity_kind": "cpu-hardware-signature",
            "identity_value": "portable-cpu",
            "worker_path": str(portable),
            "worker_sha256": _first_start_file_sha256(portable),
            "eligible": True,
        }
        self.inventory["candidates"] = [self.candidate]
        runner = self._run_probe()
        self.assertEqual(runner.call_count, 0)
        self.assertEqual(self.engine._hardware_breaker.cleared, [])

    def test_dotprod_label_cannot_make_portable_cpu_worker_recoverable(self):
        portable = Path(self.temp.name) / "rdt_whisper_worker_cpu"
        portable.write_bytes(b"portable cpu fixture worker")
        portable.chmod(0o755)
        self.candidate = {
            "candidate_id": "mislabelled-dotprod-1",
            "provider": "cpu",
            "worker_variant": "arm-dotprod",
            "identity_kind": "cpu-hardware-signature",
            "identity_value": "portable-cpu",
            "worker_path": str(portable),
            "worker_sha256": _first_start_file_sha256(portable),
            "eligible": True,
        }
        self.inventory["candidates"] = [self.candidate]
        runner = self._run_probe()
        self.assertEqual(runner.call_count, 0)
        self.assertEqual(self.engine._hardware_breaker.cleared, [])


if __name__ == "__main__":
    unittest.main()
