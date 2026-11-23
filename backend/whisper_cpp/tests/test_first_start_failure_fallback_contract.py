import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


class FirstStartFailureFallbackContractTests(unittest.TestCase):
    def test_failed_benchmark_uses_partial_selection_path(self):
        source = (ROOT / "backend/whisper_cpp/hardware/first_start.py").read_text()
        self.assertIn(
            'is_incomplete = report.get("status") in {"incomplete", "failed"}',
            source,
        )
        self.assertIn("_best_partial_qualified_candidate(", source)

    def test_partial_result_is_available_without_claiming_full_completion(self):
        source = (ROOT / "backend/whisper_cpp/hardware/first_start.py").read_text()
        self.assertIn('report["partial_engine_profile"] =', source)
        self.assertIn('report["engine_profile"] =', source)
        self.assertIn('"completed": not is_incomplete,', source)
        self.assertIn('if not is_incomplete:', source)

    def test_engine_loader_uses_partial_or_verified_cpu_fallback(self):
        source = (ROOT / "backend/whisper_cpp/engine.py").read_text()
        self.assertIn('partial.get("selection_kind")', source)
        self.assertIn('"partial-provisional"', source)
        self.assertIn('"cpu-readiness-fallback"', source)
        self.assertIn('report["engine_profile"] = partial', source)
        self.assertIn('report["completed"] = True', source)


if __name__ == "__main__":
    unittest.main()
