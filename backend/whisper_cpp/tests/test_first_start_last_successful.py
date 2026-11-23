import json
import tempfile
import unittest
from pathlib import Path

from backend.whisper_cpp.hardware.first_start import (
    _best_partial_qualified_candidate,
    _persist_last_successful_engine_profile,
)


class FirstStartSelectionPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "first-start-model.json"
        self.good = {
            "completed": True,
            "completion_schema": 1,
            "status": "measured",
            "engine_profile": {
                "schema": 1, "completed": True, "selection_kind": "measured",
                "model_sha256": "a" * 64, "runtime_fingerprint": "b" * 64,
                "worker_build": "c" * 64, "fixture_sha256": "d" * 64,
            },
            "selected_runtime": {
                "provider": "cuda", "identity_kind": "pci",
                "identity_value": "0000:01:00.0",
                "worker_path": "/app/rdt_whisper_worker_cuda",
                "worker_sha256": "e" * 64,
            },
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_successful_selection_is_saved_separately(self):
        self.assertTrue(_persist_last_successful_engine_profile(self.path, self.good))
        saved = self.path.with_name("first-start-model-last-successful.json")
        self.assertEqual(json.loads(saved.read_text()), self.good)

    def test_failed_attempt_cannot_replace_previous_success(self):
        self.assertTrue(_persist_last_successful_engine_profile(self.path, self.good))
        saved = self.path.with_name("first-start-model-last-successful.json")
        original = saved.read_bytes()
        failed = dict(self.good, completed=False, status="failed", engine_profile=None)
        self.assertFalse(_persist_last_successful_engine_profile(self.path, failed))
        self.assertEqual(saved.read_bytes(), original)

    def test_partial_run_uses_fastest_individually_qualified_candidate(self):
        candidates = [
            {"candidate_id": "gpu-a", "worker_sha256": "1" * 64,
             "identity_kind": "pci", "identity_value": "A"},
            {"candidate_id": "gpu-b", "worker_sha256": "2" * 64,
             "identity_kind": "pci", "identity_value": "B"},
        ]
        results = [
            {"candidate_id": "gpu-a", "status": "qualified", "transcript_valid": True,
             "segment_count": 2, "repetitions": 3, "median_inference_ms": 90},
            {"candidate_id": "gpu-b", "status": "qualified", "transcript_valid": True,
             "segment_count": 2, "repetitions": 3, "median_inference_ms": 50},
            {"candidate_id": "gpu-b", "status": "failed", "transcript_valid": False,
             "segment_count": 0, "repetitions": 1, "median_inference_ms": 1},
        ]
        selected = _best_partial_qualified_candidate(candidates, results)
        self.assertEqual(selected["candidate_id"], "gpu-b")

    def test_invalid_partial_measurement_is_not_selected(self):
        candidates = [{"candidate_id": "gpu-a", "worker_sha256": "1" * 64,
                       "identity_kind": "pci", "identity_value": "A"}]
        results = [{"candidate_id": "gpu-a", "status": "qualified",
                    "transcript_valid": False, "segment_count": 0,
                    "repetitions": 3, "median_inference_ms": 1}]
        self.assertIsNone(_best_partial_qualified_candidate(candidates, results))


if __name__ == "__main__":
    unittest.main()
