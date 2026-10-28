from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.linux.python_qualification import (
    canonical_direct_requirements_bytes,
    requirements_hashes,
)


class CanonicalRequirementsHashTests(unittest.TestCase):
    def test_lf_crlf_and_cr_have_identical_canonical_hashes(self) -> None:
        logical_text = "fastapi==0.162.0\nuvicorn==0.54.0\n"
        representations = (
            logical_text.encode("utf-8"),
            logical_text.replace("\n", "\r\n").encode("utf-8"),
            logical_text.replace("\n", "\r").encode("utf-8"),
        )

        with tempfile.TemporaryDirectory() as td:
            results = []
            for index, raw in enumerate(representations):
                path = Path(td) / f"requirements-{index}.txt"
                path.write_bytes(raw)
                results.append(requirements_hashes(path))

        canonical = hashlib.sha256(logical_text.encode("utf-8")).hexdigest()
        self.assertEqual(
            [r["DIRECT_REQUIREMENTS_SHA256"] for r in results],
            [canonical, canonical, canonical],
        )
        self.assertEqual(
            [r["DIRECT_REQUIREMENTS_RAW_SHA256"] for r in results],
            [hashlib.sha256(raw).hexdigest() for raw in representations],
        )
        self.assertEqual(
            len({r["DIRECT_REQUIREMENTS_RAW_SHA256"] for r in results}), 3
        )

    def test_invalid_utf8_fails_closed(self) -> None:
        with self.assertRaises(UnicodeDecodeError):
            canonical_direct_requirements_bytes(b"fastapi==0.162.0\n\xff\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
