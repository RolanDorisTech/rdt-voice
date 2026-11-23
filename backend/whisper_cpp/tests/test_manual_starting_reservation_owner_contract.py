from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[3]
RUST = (ROOT / "rdt_connect/src-tauri/src/main.rs").read_text(encoding="utf-8")
FRONTEND = (ROOT / "rdt_connect/src/main.ts").read_text(encoding="utf-8")


class ManualStartingReservationOwnerContract(unittest.TestCase):
    def test_native_guard_does_not_release_frontend_owned_starting_flag(self):
        start = RUST.index("impl Drop for NativeBenchmarkRunGuard")
        end = RUST.index("\n}\n", start)
        body = RUST[start:end]
        self.assertIn("NATIVE_BENCHMARK_CANCEL_REQUESTED.store(false", body)
        self.assertIn("NATIVE_BENCHMARK_RUNNING.store(false", body)
        self.assertNotIn("NATIVE_BENCHMARK_STARTING.store(false", body)

    def test_native_restoration_precedes_final_guard_drop(self):
        start = RUST.index("fn run_manual_hardware_benchmark_blocking")
        runner = RUST[start:]
        drop_at = runner.rfind("drop(run_guard);")
        restore_at = runner.rfind("let restore_result", 0, drop_at)
        self.assertGreaterEqual(restore_at, 0)
        self.assertLess(restore_at, drop_at)

    def test_frontend_outer_finally_releases_and_resets_starting(self):
        start = FRONTEND.rfind("if (startingGuard)")
        self.assertGreaterEqual(start, 0)
        end = FRONTEND.find("await restoreOriginalBackendIfNeeded();", start)
        self.assertGreater(end, start)
        block = FRONTEND[start:end]
        self.assertIn('set_native_benchmark_starting", { enabled: false }', block)
        self.assertIn(".finally(() => {", block)
        self.assertIn("startingGuard = false;", block)


if __name__ == "__main__":
    unittest.main()
