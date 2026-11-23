import json
import tempfile
import unittest
from pathlib import Path

from backend.whisper_cpp.hardware.circuit_breaker import (
    DAY,
    PersistentHardwareCircuitBreaker,
)


def context(identity="gpu-0", worker="a" * 64, runtime="driver-1", model="b" * 64):
    return {
        "hardware_identity": {"kind": "pci", "value": identity},
        "provider": "cuda",
        "worker_variant": "cuda",
        "worker_sha256": worker,
        "runtime": {
            "driver_version": runtime,
            "runtime_version": "CUDA-13.5",
            "runtime_fingerprint": "f" * 64,
        },
        "selector": {
            "runtime_selector_kind": "cuda-ordinal",
            "runtime_selector": "0",
        },
        "model": {"id": "approved-model", "sha256": model},
    }


class PersistentHardwareCircuitBreakerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.breaker = PersistentHardwareCircuitBreaker(
            Path(self.temp.name) / "state.json"
        )
        self.candidate = context()

    def tearDown(self):
        self.temp.cleanup()

    def test_three_incidents_within_24_hours_quarantine_candidate(self):
        self.breaker.record_failure(self.candidate, now=1000)
        self.breaker.record_failure(self.candidate, now=1100)
        self.assertFalse(self.breaker.is_quarantined(self.candidate, now=1200))
        result = self.breaker.record_failure(self.candidate, now=1200)
        self.assertEqual(result["incident_count"], 3)
        self.assertTrue(self.breaker.is_quarantined(self.candidate, now=1201))
        self.assertEqual(result["quarantined_until"], 1200 + DAY)

    def test_incidents_outside_rolling_window_do_not_accumulate(self):
        self.breaker.record_failure(self.candidate, now=1000)
        self.breaker.record_failure(self.candidate, now=1000 + DAY + 1)
        result = self.breaker.record_failure(self.candidate, now=1000 + DAY + 2)
        self.assertEqual(result["incident_count"], 2)
        self.assertFalse(self.breaker.is_quarantined(self.candidate, now=1000 + DAY + 3))

    def test_identity_worker_runtime_and_model_changes_are_separate_keys(self):
        for timestamp in (10, 20, 30):
            self.breaker.record_failure(self.candidate, now=timestamp)
        changed = context(identity="gpu-1")
        self.assertFalse(self.breaker.is_quarantined(changed, now=40))
        changed_worker = context(worker="c" * 64)
        self.assertFalse(self.breaker.is_quarantined(changed_worker, now=40))
        changed_runtime = context(runtime="driver-2")
        self.assertFalse(self.breaker.is_quarantined(changed_runtime, now=40))
        changed_model = context(model="d" * 64)
        self.assertFalse(self.breaker.is_quarantined(changed_model, now=40))

    def test_expired_quarantine_still_requires_passing_recovery_probe(self):
        for timestamp in (100, 200, 300):
            self.breaker.record_failure(self.candidate, now=timestamp)
        self.assertTrue(self.breaker.is_quarantined(self.candidate, now=300 + DAY + 1))
        self.assertEqual(len(self.breaker.recovery_probes_due(now=300 + DAY + 1)), 1)
        self.assertFalse(self.breaker.clear_after_probe(self.candidate, False, now=300 + DAY + 2))
        self.assertTrue(self.breaker.is_quarantined(self.candidate, now=300 + DAY + 3))
        self.assertTrue(self.breaker.clear_after_probe(self.candidate, True, now=300 + DAY + 4))
        self.assertFalse(self.breaker.is_quarantined(self.candidate, now=300 + DAY + 5))

    def test_corrupt_state_is_non_blocking_and_replaced_atomically(self):
        self.breaker.path.parent.mkdir(parents=True, exist_ok=True)
        self.breaker.path.write_text("{broken", encoding="utf-8")
        self.assertFalse(self.breaker.is_quarantined(self.candidate, now=10))
        self.breaker.record_failure(self.candidate, now=11)
        data = json.loads(self.breaker.path.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 1)
        self.assertEqual(len(data["entries"]), 1)

    def test_incomplete_identity_is_not_persisted(self):
        invalid = context(identity="-")
        with self.assertRaises(ValueError):
            self.breaker.record_failure(invalid, now=10)


if __name__ == "__main__":
    unittest.main()
