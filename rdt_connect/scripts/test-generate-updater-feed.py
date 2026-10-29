#!/usr/bin/env python3
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

SCRIPT = Path(__file__).with_name("generate-updater-feed.py")
SPEC = importlib.util.spec_from_file_location("feed_generator", SCRIPT)
GEN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GEN)


class FeedTests(unittest.TestCase):
    def test_valid_platform_markers(self):
        cases = [
            ("0.2.0-dev.11.1D04", "darwin-aarch64"),
            ("0.2.0-dev.11.2A05", "windows-aarch64"),
            ("0.2.0-dev.11.3A01", "windows-x86_64"),
            ("0.2.0-dev.11.4U01", "linux-aarch64"),
            ("0.2.0-dev.11.5D01", "linux-x86_64"),
        ]
        for version, target in cases:
            with self.subTest(target=target):
                self.assertTrue(GEN.parse_version(version, target))

    def test_version_ordering_and_rollover(self):
        self.assertLess(
            GEN.parse_version("0.2.0-dev.11.2A99", "windows-aarch64"),
            GEN.parse_version("0.2.0-dev.11.2B00", "windows-aarch64"),
        )
        self.assertLess(
            GEN.parse_version("0.2.0-dev.11.2A05", "windows-aarch64"),
            GEN.parse_version("0.2.0-dev.12.2A01", "windows-aarch64"),
        )
        self.assertLess(
            GEN.parse_version("0.2.0-dev.11.2A05", "windows-aarch64"),
            GEN.parse_version("0.2.1-dev.11.2A01", "windows-aarch64"),
        )

    def test_rejects_cross_target_or_bad_version(self):
        for version, target in [
            ("0.2.0-dev.11.3A01", "windows-aarch64"),
            ("0.2.0-dev.11.2A05", "windows-x86_64"),
            ("0.2.0.2A01", "windows-aarch64"),
            ("0.2.0-dev.11.2A5", "windows-aarch64"),
            ("0.2.0-dev.11.2A05-extra", "windows-aarch64"),
        ]:
            with self.subTest(version=version, target=target):
                with self.assertRaises(ValueError):
                    GEN.parse_version(version, target)

    def test_rejects_http_or_missing_signature(self):
        for entry in [
            {"url": "http://example.com/app.exe", "signature": "sig"},
            {"url": "https://example.com/app.exe", "signature": ""},
            {"url": "https://example.com/app.exe", "signature": "line1\nline2"},
            {"url": "https://example.com/app.exe", "signature": "line1\rline2"},
        ]:
            with self.subTest(entry=entry):
                with self.assertRaises(ValueError):
                    GEN.validate_entry(entry, "windows-aarch64")

    def test_cli_create_update_and_downgrade_protection(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "latest.json"
            base = [
                sys.executable, str(SCRIPT),
                "--target", "windows-aarch64",
                "--url", "https://example.com/release.exe",
                "--signature", "test-signature",
                "--notes", "safe notes; not shell code",
                "--output", str(output),
            ]
            def run(version, update=False):
                cmd = base + ["--version", version]
                if update:
                    cmd.append("--update")
                return subprocess.run(cmd, capture_output=True, text=True)

            first = run("0.2.0-dev.11.2A05")
            self.assertEqual(first.returncode, 0, first.stderr)
            original = output.read_bytes()

            no_flag = run("0.2.0-dev.11.2A06")
            self.assertNotEqual(no_flag.returncode, 0)
            self.assertEqual(output.read_bytes(), original)

            downgrade = run("0.2.0-dev.11.2A04", update=True)
            self.assertNotEqual(downgrade.returncode, 0)
            self.assertEqual(output.read_bytes(), original)

            same = run("0.2.0-dev.11.2A05", update=True)
            self.assertEqual(same.returncode, 0, same.stderr)

            # Allow a same-version URL migration only when the signature matches.
            migrated = subprocess.run(
                base + [
                    "--version", "0.2.0-dev.11.2A05",
                    "--update",
                    "--url", "https://example.com/canonical-release.exe",
                ],
                capture_output=True, text=True,
            )
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            migrated_data = json.loads(output.read_text(encoding="utf-8"))
            migrated_entry = migrated_data["platforms"]["windows-aarch64"]
            self.assertEqual(
                migrated_entry["url"],
                "https://example.com/canonical-release.exe",
            )
            self.assertEqual(migrated_entry["signature"], "test-signature")
            repaired = output.read_bytes()

            changed_artifact = subprocess.run(
                base + [
                    "--version", "0.2.0-dev.11.2A05",
                    "--update", "--signature", "different-signature",
                ],
                capture_output=True, text=True,
            )
            self.assertNotEqual(changed_artifact.returncode, 0)
            self.assertEqual(output.read_bytes(), repaired)

            advanced = run("0.2.0-dev.11.2A06", update=True)
            self.assertEqual(advanced.returncode, 0, advanced.stderr)
            data = json.loads(output.read_text())
            self.assertEqual(data["version"], "0.2.0-dev.11.2A06")
            self.assertEqual(list(data["platforms"]), ["windows-aarch64"])
            self.assertEqual(data["notes"], "safe notes; not shell code")

    def test_migrates_legacy_fractional_timestamp_on_update(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "latest.json"
            manifest = GEN.build_manifest(
                "0.2.0-dev.11.2A05", "windows-aarch64",
                "https://example.com/release.exe", "test-signature", "old notes",
            )
            manifest["pub_date"] = "2026-10-28T06:04:27.265Z"
            output.write_text(json.dumps(manifest), encoding="utf-8")

            cmd = [
                sys.executable, str(SCRIPT),
                "--version", "0.2.0-dev.11.2A06",
                "--target", "windows-aarch64",
                "--url", "https://example.com/release.exe",
                "--signature", "test-signature",
                "--notes", "updated notes",
                "--output", str(output),
                "--update",
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

            updated = json.loads(output.read_text(encoding="utf-8"))
            self.assertRegex(
                updated["pub_date"],
                r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$",
            )
            with self.assertRaises(ValueError):
                GEN.validate_manifest(manifest, "windows-aarch64")


    def test_refuses_malformed_existing_manifest_without_modifying_it(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "latest.json"
            output.write_text('{"version":"bad","platforms":{}}')
            original = output.read_bytes()
            cmd = [
                sys.executable, str(SCRIPT),
                "--version", "0.2.0-dev.11.2A06",
                "--target", "windows-aarch64",
                "--url", "https://example.com/release.exe",
                "--signature", "sig",
                "--notes", "notes",
                "--output", str(output),
                "--update",
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(output.read_bytes(), original)

    def test_strict_prerelease_and_url_validation(self):
        for version in [
            "0.2.0-dev..11.2A05",
            "0.2.0-.2A05",
            "0.2.0-dev..2A05",
            "0.2.0-dev.011.2A05",
        ]:
            with self.subTest(version=version):
                with self.assertRaises(ValueError):
                    GEN.parse_version(version, "windows-aarch64")

        for url in [
            "https://user:pass@example.com/app.exe",
            "https://example.com/app.exe?token=x",
            "https://example.com/app.exe#fragment",
            "https:///app.exe",
        ]:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    GEN.validate_entry(
                        {"url": url, "signature": "sig"}, "windows-aarch64"
                    )

    def test_manifest_rejects_missing_fields_and_bad_version_type(self):
        valid = GEN.build_manifest(
            "0.2.0-dev.11.2A05", "windows-aarch64",
            "https://example.com/app.exe", "sig", "notes"
        )
        for mutate in (
            lambda d: d.pop("notes"),
            lambda d: d.update(version=None),
            lambda d: d.update(pub_date="not-a-date"),
            lambda d: d.update(platforms={}),
        ):
            candidate = json.loads(json.dumps(valid))
            mutate(candidate)
            with self.subTest(candidate=candidate):
                with self.assertRaises(ValueError):
                    GEN.validate_manifest(candidate, "windows-aarch64")


    def test_write_atomic_restores_previous_feed_after_final_validation_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "latest.json"
            original = b'{"previous":"feed"}\\n'
            output.write_bytes(original)
            manifest = GEN.build_manifest(
                "0.2.0-dev.11.2A06",
                "windows-aarch64",
                "https://example.com/release.exe",
                "signature",
                "notes",
            )
            with mock.patch.object(
                GEN,
                "validate_manifest",
                side_effect=[manifest, ValueError("forced final validation failure")],
            ):
                with self.assertRaisesRegex(ValueError, "forced final validation failure"):
                    GEN.write_atomic(output, manifest, "windows-aarch64")
            self.assertEqual(output.read_bytes(), original)
            self.assertEqual(
                [p.name for p in Path(temp).iterdir()],
                ["latest.json"],
                "temporary files must be cleaned up",
            )

    def test_write_atomic_removes_new_feed_after_final_validation_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "latest.json"
            manifest = GEN.build_manifest(
                "0.2.0-dev.11.2A06",
                "windows-aarch64",
                "https://example.com/release.exe",
                "signature",
                "notes",
            )
            with mock.patch.object(
                GEN,
                "validate_manifest",
                side_effect=[manifest, ValueError("forced final validation failure")],
            ):
                with self.assertRaisesRegex(ValueError, "forced final validation failure"):
                    GEN.write_atomic(output, manifest, "windows-aarch64")
            self.assertFalse(output.exists())
            self.assertEqual(list(Path(temp).iterdir()), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
