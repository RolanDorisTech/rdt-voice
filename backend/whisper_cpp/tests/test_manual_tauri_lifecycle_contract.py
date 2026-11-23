import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


class ManualTauriLifecycleContractTests(unittest.TestCase):
    def test_runner_tree_is_checked_before_report_and_restore(self):
        source = (ROOT / "rdt_connect/src-tauri/src/main.rs").read_text()
        start = source.index("fn run_manual_hardware_benchmark_blocking(")
        end = source.index("\n#[tauri::command]", start + 1)
        body = source[start:end]
        pid = body.index("let runner_pid = child.id();")
        wait = body.index("let exit_status = loop")
        cleanup = body.index("if process_group_alive(runner_pid)", wait)
        report = body.index("let report_bytes", cleanup)
        restore = body.index("let restore_result", report)
        self.assertLess(pid, wait)
        self.assertLess(wait, cleanup)
        self.assertLess(cleanup, report)
        self.assertLess(report, restore)
        self.assertIn("terminate_process_group(runner_pid, Some(&mut child))", body)

    def test_raii_guard_clears_running_and_cancel_but_preserves_frontend_starting(self):
        source = (ROOT / "rdt_connect/src-tauri/src/main.rs").read_text()
        start = source.index("impl Drop for NativeBenchmarkRunGuard")
        end = source.index("\n}", start) + 2
        body = source[start:end]
        self.assertNotIn("NATIVE_BENCHMARK_STARTING.store(false", body)
        self.assertIn("NATIVE_BENCHMARK_RUNNING.store(false", body)
        self.assertIn("NATIVE_BENCHMARK_CANCEL_REQUESTED.store(false", body)

    def test_manual_frontend_finally_releases_starting_reservation(self):
        source = (ROOT / "rdt_connect/src/main.ts").read_text()
        start = source.index("let startingGuard = false;")
        found = False
        for match in __import__("re").finditer(r"finally\s*\{", source[start:]):
            pos = start + match.start()
            opening = source.index("{", pos)
            depth = 0
            closing = None
            for i in range(opening, len(source)):
                if source[i] == "{":
                    depth += 1
                elif source[i] == "}":
                    depth -= 1
                    if depth == 0:
                        closing = i
                        break
            if closing is None:
                continue
            block = source[opening + 1:closing]
            if "progressUnlisten" in block and "benchmarkRunInProgress" in block:
                self.assertIn("if (startingGuard)", block)
                self.assertIn("set_native_benchmark_starting", block)
                self.assertIn("startingGuard = false;", block)
                found = True
        self.assertTrue(found, "Manual benchmark finalizer does not release STARTING")


if __name__ == "__main__":
    unittest.main()
