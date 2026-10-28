from __future__ import annotations

import hashlib
import json
import subprocess
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





    def test_packaged_qualification_rss_note_key_is_consistent(self) -> None:
        qualifier = ROOT / ".github/scripts/qualify-packaged-linux-backend.py"
        source = qualifier.read_text(encoding="utf-8")
        self.assertIn('"sampled_peak_rss_note":', source)
        self.assertIn('report["sampled_peak_rss_note"] +=', source)
        self.assertNotIn('report["peak_rss_note"]', source)

class EarlyFailureReportTests(unittest.TestCase):
    def test_missing_packaged_backend_still_writes_failure_report(self) -> None:
        qualifier = ROOT / ".github/scripts/qualify-packaged-linux-backend.py"
        self.assertTrue(qualifier.is_file(), f"missing qualifier: {qualifier}")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            report_path = root / "evidence" / "failure.json"
            result = subprocess.run(
                [
                    sys.executable, str(qualifier),
                    "--backend", str(root / "missing-backend"),
                    "--worker", str(root / "missing-worker"),
                    "--frontend-dir", str(root / "missing-frontend"),
                    "--model-dir", str(root / "missing-model"),
                    "--fixture", str(root / "missing.wav"),
                    "--output-json", str(report_path),
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(report_path.is_file(), result.stderr[-2000:])
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report.get("outcome"), "FAIL")
            self.assertTrue(report.get("failure"))
            self.assertTrue(report.get("outer_failure_traceback"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
