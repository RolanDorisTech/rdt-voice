import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.whisper_cpp.hardware.manual import (
    execute_manual_hardware_benchmark,
    validate_manual_benchmark_request,
)


def _candidate(candidate_id, provider, identity_kind, identity_value, worker):
    return {
        "candidate_id": candidate_id,
        "provider": provider,
        "device": candidate_id,
        "worker_path": str(worker.resolve()),
        "identity_kind": identity_kind,
        "identity_value": identity_value,
        "runtime_identity_kind": identity_kind,
        "runtime_identity_value": identity_value,
        "runtime_selector": "0" if provider != "cpu" else None,
        "runtime_selector_kind": (
            "cuda-ordinal" if provider == "cuda" else "cpu"
        ),
        "eligible": True,
        "dedicated_memory_bytes": 1024 if provider != "cpu" else 0,
    }


class ManualHardwareBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.model = self.root / "model.ggml"
        self.fixture = self.root / "fixture.wav"
        self.model.write_bytes(b"test-model")
        self.fixture.write_bytes(b"owner-fixture-test-double")

        self.gpu_worker = self.root / "rdt_whisper_worker_cuda"
        self.cpu_worker = self.root / "rdt_whisper_worker"
        for worker in (self.gpu_worker, self.cpu_worker):
            worker.write_bytes(b"worker-test-double")
            worker.chmod(0o700)

        self.gpu = _candidate(
            "rdt_whisper_worker_cuda:cuda:0000:01:00.0",
            "cuda", "pci-bdf", "0000:01:00.0", self.gpu_worker,
        )
        self.cpu = _candidate(
            "rdt_whisper_worker:cpu:host",
            "cpu", "cpu", "host", self.cpu_worker,
        )
        self.inventory = [self.gpu, self.cpu]
        self.request = {
            "schema": 1,
            "run_id": "manual-test-run",
            "candidate_ids": [self.gpu["candidate_id"], self.cpu["candidate_id"]],
            "order": "gpu",
            "fixture": "12s",
            "time_limit_enabled": True,
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_discovery_selector_threshold_and_worker_path_privacy(self):
        from backend.whisper_cpp.hardware.manual import discover_manual_benchmark_candidates

        second_gpu = _candidate(
            "rdt_whisper_worker_vulkan:0000:02:00.0",
            "vulkan", "pci-bdf", "0000:02:00.0", self.gpu_worker,
        )
        inventory = [self.gpu, second_gpu, self.cpu]
        with (
            patch(
                "backend.whisper_cpp.hardware.manual._worker_inventory_paths",
                return_value=[self.gpu_worker.resolve(strict=True), self.cpu_worker.resolve(strict=True)],
            ),
            patch(
                "backend.whisper_cpp.hardware.manual.discover_hardware_candidates",
                return_value=inventory,
            ),
        ):
            result = discover_manual_benchmark_candidates()

        self.assertEqual(result["physical_choice_count"], 3)
        self.assertTrue(result["show_selector"])
        self.assertEqual(len(result["candidates"]), 3)
        self.assertTrue(all("worker_path" not in item for item in result["candidates"]))

        # The selector is omitted when fewer than three physical choices exist.
        with (
            patch(
                "backend.whisper_cpp.hardware.manual._worker_inventory_paths",
                return_value=[
                    self.gpu_worker.resolve(strict=True),
                    self.cpu_worker.resolve(strict=True),
                ],
            ),
            patch(
                "backend.whisper_cpp.hardware.manual.discover_hardware_candidates",
                return_value=[dict(self.cpu)],
            ),
        ):
            single_choice = discover_manual_benchmark_candidates()

        self.assertEqual(single_choice["physical_choice_count"], 1)
        self.assertFalse(single_choice["show_selector"])
        self.assertEqual(len(single_choice["candidates"]), 1)
        self.assertTrue(single_choice["candidates"][0]["eligible"])

    def test_request_rejects_empty_duplicate_and_stale_selection(self):
        workers = [self.gpu_worker, self.cpu_worker]

        for request in (
            {**self.request, "candidate_ids": []},
            {**self.request, "candidate_ids": [self.gpu["candidate_id"]] * 2},
            {**self.request, "candidate_ids": ["stale-candidate"]},
        ):
            with self.subTest(request=request):
                with self.assertRaises(ValueError):
                    validate_manual_benchmark_request(
                        request, self.inventory, workers
                    )

    def test_request_rejects_unverified_identity_and_unapproved_worker_path(self):
        invalid_identity = dict(self.gpu, identity_kind="unavailable", identity_value="-")
        with self.assertRaisesRegex(ValueError, "candidate_identity_invalid"):
            validate_manual_benchmark_request(
                {**self.request, "candidate_ids": [self.gpu["candidate_id"]]},
                [invalid_identity],
                [self.gpu_worker],
            )

        outside_worker = self.root / "outside-worker"
        outside_worker.write_bytes(b"not-in-inventory")
        outside_worker.chmod(0o700)
        forged = dict(self.gpu, worker_path=str(outside_worker))
        with self.assertRaisesRegex(ValueError, "worker_not_packaged"):
            validate_manual_benchmark_request(
                {**self.request, "candidate_ids": [self.gpu["candidate_id"]]},
                [forged],
                [self.gpu_worker],
            )

    def test_executor_uses_separate_report_and_profile_and_no_session_deadline(self):
        observed_contexts = []

        def fake_runner(candidate, context, model_path, fixture_path):
            observed_contexts.append(dict(context))
            speed = 80.0 if candidate["provider"] == "cuda" else 250.0
            return {
                "actual_provider": candidate["provider"],
                "identity_kind": candidate["identity_kind"],
                "identity_value": candidate["identity_value"],
                "model_sha256": context["model_sha256"],
                "fixture_sha256": context["fixture_sha256"],
                "transcript": "same deterministic transcript",
                "segments": [{"start": 0.0, "end": 1.0, "text": "sample"}],
                "inference_times_ms": [speed, speed + 2.0, speed - 2.0],
                "audio_duration_ms": 12029.0,
                "load_ms": 100.0,
                "warmup_ms": 90.0,
            }

        report_path = self.root / "manual" / "last-report.json"
        profile_path = self.root / "manual" / "profile.json"
        progress_path = self.root / "manual" / "progress.json"

        with (
            patch(
                "backend.whisper_cpp.hardware.manual.discover_hardware_candidates",
                return_value=self.inventory,
            ),
            patch(
                "backend.whisper_cpp.hardware.manual.locate_manual_fixture",
                return_value=self.fixture,
            ),
            patch(
                "backend.whisper_cpp.hardware.manual.run_candidate_benchmark",
                side_effect=fake_runner,
            ),
        ):
            report = execute_manual_hardware_benchmark(
                self.model,
                "tiny",
                [self.gpu_worker, self.cpu_worker],
                self.request,
                profile_path,
                report_path,
                progress_path,
            )

        self.assertTrue(report["completed"])
        self.assertEqual(report["mode"], "manual")
        self.assertEqual(report["status"], "measured")
        self.assertEqual(report["selected_candidate_id"], self.gpu["candidate_id"])
        self.assertEqual(
            report["manual_settings"]["candidate_timeout_seconds"], 60.0
        )
        self.assertIsNone(report["manual_settings"]["aggregate_deadline_seconds"])
        self.assertEqual(
            report["manual_settings"]["selected_candidate_ids"],
            self.request["candidate_ids"],
        )
        self.assertEqual(len(observed_contexts), 2)
        for context in observed_contexts:
            self.assertNotIn("_benchmark_deadline_monotonic", context)
            self.assertIn("_candidate_deadline_monotonic", context)

        saved = json.loads(report_path.read_text(encoding="utf-8"))
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["run_id"], "manual-test-run")
        self.assertEqual(progress["run_id"], "manual-test-run")
        self.assertEqual(progress["phase"], "complete")
        self.assertTrue(profile_path.exists())
        self.assertFalse(profile_path.with_name(profile_path.name + ".running").exists())

    def test_21_second_fixture_uses_120_seconds_per_candidate(self):
        self.request["fixture"] = "21s"
        seen = []

        def fake_runner(candidate, context, model_path, fixture_path):
            seen.append(dict(context))
            speed = 100.0 if candidate["provider"] == "cuda" else 240.0
            return {
                "actual_provider": candidate["provider"],
                "identity_kind": candidate["identity_kind"],
                "identity_value": candidate["identity_value"],
                "model_sha256": context["model_sha256"],
                "fixture_sha256": context["fixture_sha256"],
                "transcript": "same long-fixture transcript",
                "segments": [{"start": 0.0, "end": 1.0, "text": "sample"}],
                "inference_times_ms": [speed, speed + 1.0, speed - 1.0],
                "audio_duration_ms": 20726.0,
                "load_ms": 10.0,
                "warmup_ms": 10.0,
            }

        with (
            patch(
                "backend.whisper_cpp.hardware.manual.discover_hardware_candidates",
                return_value=self.inventory,
            ),
            patch(
                "backend.whisper_cpp.hardware.manual.locate_manual_fixture",
                return_value=self.fixture,
            ),
            patch(
                "backend.whisper_cpp.hardware.manual.run_candidate_benchmark",
                side_effect=fake_runner,
            ),
        ):
            report = execute_manual_hardware_benchmark(
                self.model,
                "tiny",
                [self.gpu_worker, self.cpu_worker],
                self.request,
                self.root / "manual-21s" / "profile.json",
                self.root / "manual-21s" / "report.json",
                self.root / "manual-21s" / "progress.json",
            )

        self.assertTrue(report["completed"])
        self.assertEqual(report["mode"], "manual")
        self.assertEqual(
            report["manual_settings"]["candidate_timeout_seconds"], 120.0
        )
        self.assertIsNone(report["manual_settings"]["aggregate_deadline_seconds"])
        self.assertEqual(len(seen), 2)
        for context in seen:
            self.assertNotIn("_benchmark_deadline_monotonic", context)
            remaining = (
                context["_candidate_deadline_monotonic"]
                - __import__("time").monotonic()
            )
            self.assertGreater(remaining, 0)
            self.assertLessEqual(remaining, 120.0)

    def test_unchecked_time_limit_has_no_candidate_deadline(self):
        self.request["time_limit_enabled"] = False
        seen = []

        def fake_runner(candidate, context, model_path, fixture_path):
            seen.append(dict(context))
            speed = 90.0 if candidate["provider"] == "cuda" else 220.0
            return {
                "actual_provider": candidate["provider"],
                "identity_kind": candidate["identity_kind"],
                "identity_value": candidate["identity_value"],
                "model_sha256": context["model_sha256"],
                "fixture_sha256": context["fixture_sha256"],
                "transcript": "same transcript",
                "segments": [{"start": 0.0, "end": 1.0, "text": "sample"}],
                "inference_times_ms": [speed, speed + 1.0, speed - 1.0],
                "audio_duration_ms": 12029.0,
                "load_ms": 10.0,
                "warmup_ms": 10.0,
            }

        with (
            patch(
                "backend.whisper_cpp.hardware.manual.discover_hardware_candidates",
                return_value=self.inventory,
            ),
            patch(
                "backend.whisper_cpp.hardware.manual.locate_manual_fixture",
                return_value=self.fixture,
            ),
            patch(
                "backend.whisper_cpp.hardware.manual.run_candidate_benchmark",
                side_effect=fake_runner,
            ),
        ):
            report = execute_manual_hardware_benchmark(
                self.model,
                "tiny",
                [self.gpu_worker, self.cpu_worker],
                self.request,
                self.root / "manual-unlimited" / "profile.json",
                self.root / "manual-unlimited" / "report.json",
                self.root / "manual-unlimited" / "progress.json",
            )

        self.assertTrue(report["completed"])
        self.assertIsNone(report["manual_settings"]["candidate_timeout_seconds"])
        for context in seen:
            self.assertNotIn("_benchmark_deadline_monotonic", context)
            self.assertNotIn("_candidate_deadline_monotonic", context)


if __name__ == "__main__":
    unittest.main()
