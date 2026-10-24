from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.whisper_cpp import model_registry
from backend.whisper_cpp.qualification_support import verify_model_in_directory


class PackagedQualificationModelRootTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.cache = base / "explicit-model-cache"
        self.cache.mkdir()
        self.wrong_cache = base / "wrong-cache"
        self.wrong_cache.mkdir()

        self.original_spec = dict(model_registry.MODELS["tiny"])
        self.payload = b"qualification fixture; not a real Whisper model"
        model_registry.MODELS["tiny"]["file"] = "qualification-tiny.bin"
        model_registry.MODELS["tiny"]["sha256"] = hashlib.sha256(
            self.payload
        ).hexdigest()

        self.model_path = self.cache / "qualification-tiny.bin"
        self.model_path.write_bytes(self.payload)
        sidecar = {
            "schema_version": 1,
            "model_id": "tiny",
            "repo": model_registry.SOURCE_REPO,
            "revision": model_registry.REVISION,
            "url": "https://huggingface.co/qualification-fixture",
            "filename": self.model_path.name,
            "quantization": model_registry.MODELS["tiny"]["quantization"],
            "size_bytes": len(self.payload),
            "sha256": model_registry.MODELS["tiny"]["sha256"],
        }
        self.model_path.with_name(
            self.model_path.name + ".rdt-verified.json"
        ).write_text(json.dumps(sidecar), encoding="utf-8")

    def tearDown(self) -> None:
        model_registry.MODELS["tiny"].clear()
        model_registry.MODELS["tiny"].update(self.original_spec)
        self.temp.cleanup()

    def test_explicit_model_dir_is_used_as_verification_root(self) -> None:
        with patch.dict(
            os.environ, {"RDT_GGML_MODEL_DIR": str(self.wrong_cache)}
        ):
            spec, model_path, verified_sidecar = verify_model_in_directory(
                "tiny", self.cache
            )

            self.assertEqual(model_path.resolve(strict=True), self.model_path.resolve(strict=True))
            self.assertEqual(spec["file"], self.model_path.name)
            self.assertEqual(
                Path(os.environ["RDT_GGML_MODEL_DIR"]),
                self.cache.resolve(),
            )
            self.assertEqual(
                verified_sidecar["sha256"],
                model_registry.MODELS["tiny"]["sha256"],
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
