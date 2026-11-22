#!/usr/bin/env python3
"""Regression contracts for Connect's single-process packaged backend."""
from pathlib import Path
import ast
import importlib.util
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCHER_PATH = ROOT / "scripts" / "patch_desktop_backend.py"
SPEC = importlib.util.spec_from_file_location("rdt_connect_patcher", PATCHER_PATH)
PATCHER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PATCHER)

class ConnectSingleProcessContracts(unittest.TestCase):
    def test_injected_guard_binds_standard_sys_and_private_alias(self):
        tree = ast.parse(PATCHER_PATH.read_text(encoding="utf-8"))
        assignment = next(
            node for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "_CONNECT_SINGLE_PROCESS_GUARD"
                for target in node.targets
            )
        )
        guard = ast.literal_eval(assignment.value)
        self.assertIn("import sys as _rdt_sys", guard)
        self.assertRegex(guard, r"(?m)^import sys$")

    def test_guard_is_inserted_before_heavy_runtime_imports(self):
        fixture = "import threading\nimport os\nimport numpy as np\n"
        result = PATCHER._apply_connect_single_process_guard(fixture)
        self.assertLess(result.index("import multiprocessing.resource_tracker"),
                        result.index("import os"))
        self.assertLess(result.index("import multiprocessing.resource_tracker"),
                        result.index("import numpy"))
        self.assertIn("_rdt_std_tqdm.set_lock(threading.RLock())", result)
        self.assertEqual(
            PATCHER._apply_connect_single_process_guard(result), result
        )

    def test_tracker_registration_is_a_noop_without_starting_a_helper(self):
        prelude = PATCHER._apply_connect_single_process_guard("import threading\n")
        code = (
            "import multiprocessing.resource_tracker as rt\n"
            "exec(compile(" + repr(prelude) + ", '<guard>', 'exec'), "
            "{'__name__':'rdt_probe'})\n"
            "assert rt.register('/rdt-contract-probe', 'semaphore') is None\n"
            "assert rt.unregister('/rdt-contract-probe', 'semaphore') is None\n"
            "assert rt._resource_tracker._pid is None\n"
            "assert rt._resource_tracker._fd is None\n"
        )
        result = subprocess.run([sys.executable, "-c", code], text=True,
                                capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_tracker_and_forkserver_frozen_helper_dispatch_exits_cleanly(self):
        prelude = PATCHER._apply_connect_single_process_guard("import threading\n")
        for command in (
            "from multiprocessing.resource_tracker import main;main(6)",
            "from multiprocessing.forkserver import main;main(6, 7)",
            "import sys; from multiprocessing.forkserver import main;main(6, 7)",
        ):
            code = (
                "import sys\n"
                "sys.argv=['rdt_voice_backend','-B','-S','-I','-c'," +
                repr(command) + "]\n"
                "try:\n"
                " exec(compile(" + repr(prelude) +
                ", '<guard>', 'exec'), {'__name__':'__main__'})\n"
                "except SystemExit as exc:\n"
                " assert exc.code == 0, exc.code\n"
                "else:\n"
                " raise AssertionError('helper dispatch was not stopped')\n"
            )
            result = subprocess.run([sys.executable, "-c", code], text=True,
                                    capture_output=True)
            self.assertEqual(result.returncode, 0,
                             result.stdout + result.stderr)

    def test_unknown_frozen_python_c_invocation_fails_closed(self):
        prelude = PATCHER._apply_connect_single_process_guard("import threading\n")
        code = (
            "import sys\n"
            "sys.argv=['rdt_voice_backend','-c','print(1)']\n"
            "try:\n"
            " exec(compile(" + repr(prelude) +
            ", '<guard>', 'exec'), {'__name__':'__main__'})\n"
            "except SystemExit as exc:\n"
            " assert exc.code == 64, exc.code\n"
            "else:\n"
            " raise AssertionError('unexpected -c invocation was accepted')\n"
        )
        result = subprocess.run([sys.executable, "-c", code], text=True,
                                capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_synthetic_startup_warmup_is_removed_but_real_inference_remains(self):
        fixture = (
            "import threading\n"
            "# Warmup\n"
            "_warm_path = 'synthetic.wav'\n"
            "mlx_whisper.transcribe(_warm_path)\n"
            "def clean_transcript_enhanced(text):\n"
            "    return text\n"
            "def trans():\n"
            "    return mlx_whisper.transcribe(w2)\n"
        )
        result = PATCHER._remove_connect_startup_warmup(fixture)
        self.assertNotIn("_warm_path", result)
        self.assertNotIn("# Warmup", result)
        self.assertIn("mlx_whisper.transcribe(w2)", result)
        self.assertEqual(PATCHER._remove_connect_startup_warmup(result), result)

    def test_patch_pipeline_applies_both_connect_only_adaptations(self):
        source = PATCHER_PATH.read_text()
        self.assertIn("text = _apply_connect_single_process_guard(text)", source)
        self.assertIn("text = _remove_connect_startup_warmup(text)", source)


    def test_current_canonical_voice_source_transforms_safely(self):
        connect_source = ROOT.parent / "backend" / "apple" / "server.py"
        self.assertTrue(connect_source.is_file(), str(connect_source))
        staged = PATCHER.patch(connect_source)
        ast.parse(staged)
        self.assertIn(
            "_rdt_tracker.ensure_running = _rdt_disable_resource_tracking",
            staged,
        )
        self.assertIn("_rdt_std_tqdm.set_lock(threading.RLock())", staged)
        self.assertNotIn("# Warmup\n", staged)
        self.assertNotIn("_warm_path", staged)
        self.assertIn("mlx_whisper.transcribe(", staged)

if __name__ == "__main__":
    unittest.main(verbosity=2)
