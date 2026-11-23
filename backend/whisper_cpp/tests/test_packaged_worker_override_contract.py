import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import backend.whisper_cpp.engine as engine_module


class PackagedWorkerOverrideContract(unittest.TestCase):
    def test_frozen_app_rejects_worker_outside_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_dir = root / "app"
            external_dir = root / "external"
            app_dir.mkdir()
            external_dir.mkdir()
            executable = app_dir / "RDT Connect"
            worker = external_dir / "rdt_whisper_worker_cuda"

            with patch.dict(os.environ, {
                "RDT_WHISPER_WORKER_EXE": str(worker),
                "RDT_WHISPER_PROVIDER": "auto",
            }), patch.object(engine_module.sys, "frozen", True, create=True), \
                 patch.object(engine_module.sys, "executable", str(executable)):
                with self.assertRaises(RuntimeError) as caught:
                    engine_module.PersistentWhisperEngine.worker_candidates()

            self.assertEqual(
                str(caught.exception),
                "persistent_worker_override_outside_package",
            )

    def test_frozen_app_accepts_worker_beside_app_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_dir = Path(tmp)
            executable = app_dir / "RDT Connect"
            worker = app_dir / "rdt_whisper_worker_cuda"

            with patch.dict(os.environ, {
                "RDT_WHISPER_WORKER_EXE": str(worker),
                "RDT_WHISPER_PROVIDER": "auto",
            }), patch.object(engine_module.sys, "frozen", True, create=True), \
                 patch.object(engine_module.sys, "executable", str(executable)):
                result = engine_module.PersistentWhisperEngine.worker_candidates()

            self.assertEqual(result, [worker.resolve()])

    def test_frozen_app_rejects_symlink_worker_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_dir = root / "app"
            app_dir.mkdir()
            target = app_dir / "real_worker"
            target.write_text("fixture")
            link = app_dir / "rdt_whisper_worker_cuda"
            link.symlink_to(target)

            with patch.dict(os.environ, {
                "RDT_WHISPER_WORKER_EXE": str(link),
                "RDT_WHISPER_PROVIDER": "auto",
            }), patch.object(engine_module.sys, "frozen", True, create=True), \
                 patch.object(engine_module.sys, "executable", str(app_dir / "RDT Connect")):
                with self.assertRaises(RuntimeError) as caught:
                    engine_module.PersistentWhisperEngine.worker_candidates()

            self.assertEqual(
                str(caught.exception),
                "persistent_worker_override_is_symlink",
            )


if __name__ == "__main__":
    unittest.main()
