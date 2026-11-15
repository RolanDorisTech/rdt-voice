import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from macos_release_identity import derive_release_identity


class ReleaseIdentityTests(unittest.TestCase):
    def test_resolves_existing_1d34(self):
        identity = derive_release_identity("0.2.0-dev.12.1D34", "1D34")
        self.assertEqual(identity.tag, "rdt-connect-v0.2.0-dev.12-1d34")
        self.assertEqual(identity.stage_directory, "build/release-assets-1D34")
        self.assertEqual(
            identity.dmg_name, "RDT_Connect_0.2.0-dev.12.1D34_aarch64.dmg"
        )

    def test_resolves_current_1d35(self):
        identity = derive_release_identity("0.2.0-dev.12.1D35", "1D35")
        self.assertEqual(identity.tag, "rdt-connect-v0.2.0-dev.12-1d35")
        self.assertEqual(identity.stage_directory, "build/release-assets-1D35")
        self.assertIn(identity.tag, identity.archive_url)

    def test_resolves_future_1d36_without_source_edit(self):
        identity = derive_release_identity("0.2.0-dev.12.1D36", "1D36")
        self.assertEqual(identity.tag, "rdt-connect-v0.2.0-dev.12-1d36")
        self.assertEqual(identity.stage_directory, "build/release-assets-1D36")

    def test_rejects_version_marker_mismatch(self):
        with self.assertRaisesRegex(ValueError, "marker mismatch"):
            derive_release_identity("0.2.0-dev.12.1D35", "1D34")

    def test_rejects_non_macos_platform_marker(self):
        with self.assertRaisesRegex(ValueError, "macOS Apple Silicon"):
            derive_release_identity("0.2.0-dev.12.2A01", "2A01")

    def test_rejects_malformed_version(self):
        with self.assertRaises(ValueError):
            derive_release_identity("0.2.0-dev.12", "1D35")


if __name__ == "__main__":
    unittest.main(verbosity=2)
