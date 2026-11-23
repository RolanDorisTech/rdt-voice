from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.linux.scripts.validate_linux_requirements_lock import (
    load_installed_graph,
    parse_direct_requirements,
    parse_lock,
    validate_dependency_lock,
)

HASH_A = "a" * 64
HASH_B = "b" * 64


class LinuxLockValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.direct_path = self.root / "requirements.in"
        self.lock_path = self.root / "requirements.lock"
        self.direct_path.write_text(
            "# direct inputs\nfastapi==1.2.3\nuvicorn==4.5.6\n",
            encoding="utf-8",
        )
        self.lock_path.write_text(
            f"fastapi==1.2.3 \\\\\n    --hash=sha256:{HASH_A} \\\\\n"
            f"    --hash=sha256:{HASH_B}\n"
            f"uvicorn==4.5.6 \\\\\n    --hash=sha256:{HASH_A}\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_matching_direct_pins_and_hashed_lock_pass(self) -> None:
        direct = parse_direct_requirements(self.direct_path)
        locked = parse_lock(self.lock_path)
        result = validate_dependency_lock(direct, locked)
        self.assertTrue(result["direct_pins_match_lock"])
        self.assertTrue(result["lock_entries_have_sha256_hashes"])
        self.assertEqual(result["lock_package_count"], 2)

    def test_missing_artifact_hash_fails_closed(self) -> None:
        self.lock_path.write_text(
            "fastapi==1.2.3\nuvicorn==4.5.6 --hash=sha256:" + HASH_A + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "no SHA-256"):
            parse_lock(self.lock_path)

    def test_direct_version_mismatch_is_rejected(self) -> None:
        self.lock_path.write_text(
            "fastapi==9.9.9 --hash=sha256:" + HASH_A + "\n"
            "uvicorn==4.5.6 --hash=sha256:" + HASH_B + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "Direct/lock mismatch"):
            validate_dependency_lock(
                parse_direct_requirements(self.direct_path),
                parse_lock(self.lock_path),
            )

    def test_installed_graph_must_match_exact_lock(self) -> None:
        direct = parse_direct_requirements(self.direct_path)
        locked = parse_lock(self.lock_path)
        installed = {"fastapi": "1.2.3", "uvicorn": "4.5.6"}
        self.assertTrue(
            validate_dependency_lock(direct, locked, installed)
            ["installed_graph"]["matches_lock"]
        )
        with self.assertRaisesRegex(ValueError, "Installed graph mismatch"):
            validate_dependency_lock(
                direct, locked, {"fastapi": "1.2.3", "uvicorn": "0.0.1"}
            )

    def test_malformed_installed_graph_is_rejected(self) -> None:
        path = self.root / "installed.json"
        path.write_text(json.dumps([{"name": "fastapi"}]), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Missing installed version"):
            load_installed_graph(path)

    def test_invalid_utf8_direct_input_fails_closed(self) -> None:
        self.direct_path.write_bytes(b"fastapi==1.2.3\n\xff\n")
        with self.assertRaises(UnicodeDecodeError):
            parse_direct_requirements(self.direct_path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
