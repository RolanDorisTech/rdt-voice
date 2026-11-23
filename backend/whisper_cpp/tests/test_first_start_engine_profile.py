import hashlib
import json
import os
import platform
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.whisper_cpp import engine as engine_module


class FirstStartEngineProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.worker_root = self.root / "workers"
        self.worker_root.mkdir()
        self.model_path = self.root / "model.bin"
        self.model_path.write_bytes(b"verified-model-fixture")
        self.worker_path = self.worker_root / "rdt_whisper_worker_cuda"
        self.worker_path.write_bytes(b"test-cuda-worker-binary")
        self.worker_path.chmod(0o755)
        self.report_path = self.root / "first-start-report.json"
        self.model_id = "test-model-id"

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _sha256(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _write_report(self, **selection_overrides):
        selected = {
            "candidate_id": "cuda:pci:0000:01:00.0",
            "provider": "cuda",
            "identity_kind": "pci_bdf",
            "identity_value": "0000:01:00.0",
            "worker_path": str(self.worker_path),
            "worker_sha256": self._sha256(self.worker_path),
            "worker_variant": "cuda",
            "runtime_selector": "0",
            "runtime_selector_kind": "ordinal",
        }
        selected.update(selection_overrides)
        profile = {
            "schema": 1,
            "completed": True,
            "selection_kind": "measured",
            "model_id": self.model_id,
            "model_sha256": self._sha256(self.model_path),
            "os": "windows" if os.name == "nt" else "linux",
            "architecture": platform.machine().lower(),
            "runtime_fingerprint": "a" * 64,
            "worker_build": "b" * 64,
            "fixture_sha256": "c" * 64,
            "selected_runtime": selected,
        }
        self.report_path.write_text(
            json.dumps({"completed": True, "engine_profile": profile}),
            encoding="utf-8",
        )

    def _current_runtime_identity(self):
        document = json.loads(self.report_path.read_text(encoding="utf-8"))
        saved = document["engine_profile"]["selected_runtime"]
        candidate = dict(saved)
        worker_path = Path(str(saved.get("worker_path") or ""))
        if worker_path.is_file():
            candidate["worker_path"] = str(worker_path.resolve())
            candidate["worker_sha256"] = self._sha256(worker_path)
        return {
            "runtime_fingerprint": document["engine_profile"]["runtime_fingerprint"],
            "worker_build": document["engine_profile"]["worker_build"],
            "candidates": [candidate],
        }

    def _load(self):
        with (
            patch.dict(
                os.environ,
                {"RDT_WHISPER_FIRST_START_REPORT_PATH": str(self.report_path)},
            ),
            patch.object(
                engine_module,
                "_packaged_worker_root",
                return_value=self.worker_root,
            ),
            patch.object(
                engine_module,
                "_current_first_start_runtime_identity",
                side_effect=self._current_runtime_identity,
            ),
        ):
            return engine_module._load_first_start_engine_profile(
                self.model_path, self.model_id
            )


    def test_accepts_exact_packaged_worker_and_model(self):
        self._write_report()
        selected, reason = self._load()
        self.assertIsNone(reason)
        self.assertIsNotNone(selected)
        self.assertEqual(selected["worker_path"], str(self.worker_path.resolve()))
        self.assertEqual(selected["provider"], "cuda")
        self.assertEqual(selected["identity_value"], "0000:01:00.0")

    def test_rejects_worker_hash_changed_after_benchmark(self):
        self._write_report()
        self.worker_path.write_bytes(b"replaced-worker-binary")
        selected, reason = self._load()
        self.assertIsNone(selected)
        self.assertEqual(reason, "first_start_profile_rejected")

    def test_rejects_model_hash_mismatch(self):
        self._write_report()
        self.model_path.write_bytes(b"different-model")
        selected, reason = self._load()
        self.assertIsNone(selected)
        self.assertEqual(reason, "first_start_profile_rejected")

    def test_rejects_provider_worker_mismatch(self):
        self._write_report(provider="vulkan")
        selected, reason = self._load()
        self.assertIsNone(selected)
        self.assertEqual(reason, "first_start_profile_rejected")

    def test_rejects_identity_missing_from_profile(self):
        self._write_report(identity_value="")
        selected, reason = self._load()
        self.assertIsNone(selected)
        self.assertEqual(reason, "first_start_profile_rejected")

    def test_rejects_worker_outside_packaged_directory(self):
        outside = self.root / "outside-worker"
        outside.write_bytes(b"not-packaged")
        outside.chmod(0o755)
        self._write_report(
            worker_path=str(outside),
            worker_sha256=self._sha256(outside),
        )
        selected, reason = self._load()
        self.assertIsNone(selected)
        self.assertEqual(reason, "first_start_profile_rejected")

    def test_missing_report_means_benchmark_not_yet_run(self):
        with (
            patch.dict(
                os.environ,
                {"RDT_WHISPER_FIRST_START_REPORT_PATH": str(self.report_path)},
            ),
            patch.object(
                engine_module,
                "_packaged_worker_root",
                return_value=self.worker_root,
            ),
        ):
            selected, reason = engine_module._load_first_start_engine_profile(
                self.model_path, self.model_id
            )
        self.assertIsNone(selected)
        self.assertIsNone(reason)

    def test_ready_handshake_must_match_provider_and_physical_identity(self):
        selection = {
            "provider": "cuda",
            "identity_kind": "pci_bdf",
            "identity_value": "0000:01:00.0",
        }
        ready = {
            "provider": "cuda",
            "identity_kind": "pci_bdf",
            "identity_value": "0000:01:00.0",
        }
        engine_module._validate_first_start_ready(ready, selection)

        wrong_device = dict(ready, identity_value="0000:02:00.0")
        with self.assertRaisesRegex(
            RuntimeError, "first_start_profile_ready_identity_mismatch"
        ):
            engine_module._validate_first_start_ready(
                wrong_device, selection
            )


if __name__ == "__main__":
    unittest.main()
