import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


class OOMNeverDemotesContractTests(unittest.TestCase):
    def test_oom_is_excluded_before_hardware_demotion_state_changes(self):
        source = (ROOT / "backend/whisper_cpp/engine.py").read_text()
        start = source.index("    def _demote_failed_non_cpu_worker(")
        end = source.find("\n    def ", start + 5)
        body = source[start:end if end >= 0 else len(source)]
        lock_position = body.index("with self._state_lock:")
        guard = body[:lock_position]
        for marker in (
            '"out_of_memory"',
            '"out of memory"',
            '"worker_oom"',
            "return",
        ):
            self.assertIn(marker, guard)


if __name__ == "__main__":
    unittest.main()
