import unittest

from backend.whisper_cpp.hardware.benchmark import (
    _cache_key,
    _cached_candidate_matches_current,
)


class FirstStartCacheIdentityTests(unittest.TestCase):
    def setUp(self):
        self.context = {
            "os": "windows",
            "architecture": "amd64",
            "runtime_fingerprint": "a" * 64,
            "worker_build": "b" * 64,
            "model_id": "verified-model",
            "model_sha256": "c" * 64,
            "fixture_sha256": "d" * 64,
        }
        self.candidate = {
            "candidate_id": "gpu-0-cuda",
            "provider": "cuda",
            "identity_kind": "pci",
            "identity_value": "0000:01:00.0",
            "worker_path": "/app/rdt_whisper_worker_cuda",
            "worker_sha256": "e" * 64,
            "worker_variant": "cuda",
            "driver": "nvidia",
            "driver_version": "580.00",
            "runtime_version": "13.5",
            "runtime_selector_kind": "cuda-device",
            "runtime_selector": "0",
            "device": "GPU 0",
            "dxgi_adapter_luid": "",
            "eligible": True,
        }

    def test_context_changes_invalidate_cache_key(self):
        baseline = _cache_key(self.context, [self.candidate])
        for field in (
            "runtime_fingerprint", "worker_build",
            "model_sha256", "fixture_sha256",
        ):
            changed = dict(self.context)
            changed[field] = "f" * 64
            with self.subTest(field=field):
                self.assertNotEqual(baseline, _cache_key(changed, [self.candidate]))

    def test_worker_and_device_changes_invalidate_cache_key(self):
        baseline = _cache_key(self.context, [self.candidate])
        fields = (
            "candidate_id", "provider", "identity_kind", "identity_value",
            "worker_path", "worker_sha256", "worker_variant", "driver",
            "driver_version", "runtime_version", "runtime_selector_kind",
            "runtime_selector", "device", "dxgi_adapter_luid",
        )
        for field in fields:
            changed = dict(self.candidate)
            changed[field] = "changed-" + field
            with self.subTest(field=field):
                self.assertNotEqual(baseline, _cache_key(self.context, [changed]))

    def test_cached_winner_requires_exact_current_identity(self):
        self.assertTrue(
            _cached_candidate_matches_current(self.candidate, self.candidate)
        )
        fields = (
            "candidate_id", "identity_kind", "identity_value", "worker_path",
            "worker_sha256", "worker_variant", "driver", "driver_version",
            "runtime_version", "runtime_selector_kind", "runtime_selector",
            "device",
        )
        for field in fields:
            changed = dict(self.candidate)
            changed[field] = "changed-" + field
            with self.subTest(field=field):
                self.assertFalse(
                    _cached_candidate_matches_current(changed, self.candidate)
                )


if __name__ == "__main__":
    unittest.main()
