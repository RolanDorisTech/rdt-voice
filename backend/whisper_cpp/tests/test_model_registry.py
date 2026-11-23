from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from whisper_cpp import model_registry as registry


class SharedModelRegistryContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "ggml"
        self.root.mkdir()
        self.original_spec = dict(registry.MODELS["tiny"])
        self.payload = b"test fixture model bytes; not a real Whisper artifact"
        registry.MODELS["tiny"]["sha256"] = hashlib.sha256(self.payload).hexdigest()
        registry.MODELS["tiny"]["file"] = "test-tiny.bin"

    def tearDown(self) -> None:
        registry.MODELS["tiny"].clear()
        registry.MODELS["tiny"].update(self.original_spec)
        self.tmp.cleanup()

    def _write_verified(self) -> Path:
        spec = registry.MODELS["tiny"]
        path = self.root / spec["file"]
        path.write_bytes(self.payload)
        metadata = {
            "schema_version": 1,
            "model_id": "tiny",
            "repo": registry.SOURCE_REPO,
            "revision": registry.REVISION,
            "url": f"https://huggingface.co/{registry.SOURCE_REPO}/resolve/{registry.REVISION}/{spec['file']}",
            "filename": spec["file"],
            "quantization": spec["quantization"],
            "size_bytes": len(self.payload),
            "sha256": spec["sha256"],
        }
        path.with_name(path.name + ".rdt-verified.json").write_text(json.dumps(metadata), encoding="utf-8")
        return path

    def test_verified_sidecar_and_full_bytes_are_accepted(self) -> None:
        path = self._write_verified()
        with patch.object(registry, "model_root", return_value=self.root):
            self.assertEqual(registry.verify_cached_model("tiny", path)["size_bytes"], len(self.payload))

    def test_changed_bytes_are_rejected_even_when_sidecar_is_retained(self) -> None:
        path = self._write_verified()
        path.write_bytes(self.payload + b"tampered")
        with patch.object(registry, "model_root", return_value=self.root):
            with self.assertRaisesRegex(RuntimeError, "fully verified"):
                registry.verify_cached_model("tiny", path)

    def test_wrong_revision_is_rejected(self) -> None:
        path = self._write_verified()
        sidecar = path.with_name(path.name + ".rdt-verified.json")
        metadata = json.loads(sidecar.read_text(encoding="utf-8"))
        metadata["revision"] = "0" * 40
        sidecar.write_text(json.dumps(metadata), encoding="utf-8")
        with patch.object(registry, "model_root", return_value=self.root):
            with self.assertRaisesRegex(RuntimeError, "fully verified"):
                registry.verify_cached_model("tiny", path)


if __name__ == "__main__":
    unittest.main()
