import copy
import unittest

from backend.whisper_cpp.engine import _validate_first_start_profile_runtime


class FirstStartProfileFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.candidate = {
            "candidate_id": "cuda:pci:0000:01:00.0",
            "provider": "cuda",
            "identity_kind": "pci-bdf",
            "identity_value": "0000:01:00.0",
            "driver": "nvidia",
            "driver_version": "555.42",
            "runtime_version": "12.4",
            "hardware_signature": "gpu-signature",
            "runtime_identity_kind": "pci-bdf",
            "runtime_identity_value": "0000:01:00.0",
            "runtime_selector_kind": "cuda-ordinal",
            "runtime_selector": "0",
            "device": "Test GPU",
            "worker_variant": "cuda",
            "worker_path": "/app/bin/rdt_whisper_worker_cuda",
            "worker_sha256": "a" * 64,
        }
        self.current = {
            "runtime_fingerprint": "b" * 64,
            "worker_build": "c" * 64,
            "candidates": [copy.deepcopy(self.candidate)],
        }
        self.profile = {
            "runtime_fingerprint": self.current["runtime_fingerprint"],
            "worker_build": self.current["worker_build"],
            "selected_runtime": copy.deepcopy(self.candidate),
        }

    def test_accepts_exact_current_candidate(self):
        _validate_first_start_profile_runtime(self.profile, self.current)

    def test_rejects_changed_global_fingerprints(self):
        for key, value in (
            ("runtime_fingerprint", "d" * 64),
            ("worker_build", "e" * 64),
        ):
            current = copy.deepcopy(self.current)
            current[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                _validate_first_start_profile_runtime(self.profile, current)

    def test_rejects_driver_runtime_identity_and_selector_changes(self):
        for key, value in (
            ("driver_version", "different-driver"),
            ("runtime_version", "different-runtime"),
            ("identity_value", "0000:02:00.0"),
            ("hardware_signature", "different-gpu"),
            ("runtime_selector", "1"),
            ("runtime_selector_kind", "different"),
        ):
            current = copy.deepcopy(self.current)
            current["candidates"][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                _validate_first_start_profile_runtime(self.profile, current)

    def test_rejects_missing_or_duplicate_candidate(self):
        for candidates in ([], [self.candidate, copy.deepcopy(self.candidate)]):
            current = copy.deepcopy(self.current)
            current["candidates"] = candidates
            with self.assertRaises(ValueError):
                _validate_first_start_profile_runtime(self.profile, current)

    def test_rejects_worker_path_hash_and_variant_changes(self):
        for key, value in (
            ("worker_path", "/app/bin/rdt_whisper_worker_vulkan"),
            ("worker_sha256", "f" * 64),
            ("worker_variant", "vulkan"),
        ):
            current = copy.deepcopy(self.current)
            current["candidates"][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                _validate_first_start_profile_runtime(self.profile, current)


if __name__ == "__main__":
    unittest.main()
