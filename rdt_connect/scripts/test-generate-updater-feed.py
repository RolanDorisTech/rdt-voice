#!/usr/bin/env python3
import importlib.util
import unittest

SPEC = importlib.util.spec_from_file_location(
    "feed_generator", "scripts/generate-updater-feed.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FeedValidationTests(unittest.TestCase):
    def test_accepts_current_platform_markers(self):
        cases = [
            ("0.2.0-dev.11.1D01", "darwin-aarch64"),
            ("0.2.0-dev.11.2A05", "windows-aarch64"),
            ("0.2.0-dev.11.3A01", "windows-x86_64"),
            ("0.2.0-dev.11.4U01", "linux-aarch64"),
            ("0.2.0-dev.11.5U01", "linux-x86_64"),
        ]
        for version, target in cases:
            with self.subTest(version=version, target=target):
                MODULE.validate(
                    version, target,
                    "https://example.com/artifact", "test-signature"
                )

    def test_rejects_cross_platform_markers(self):
        cases = [
            ("0.2.0-dev.11.2A05", "darwin-aarch64"),
            ("0.2.0-dev.11.3A01", "windows-aarch64"),
            ("0.2.0-dev.11.4U01", "windows-x86_64"),
            ("0.2.0-dev.11.5U01", "linux-aarch64"),
        ]
        for version, target in cases:
            with self.subTest(version=version, target=target):
                with self.assertRaises(ValueError):
                    MODULE.validate(
                        version, target,
                        "https://example.com/artifact", "test-signature"
                    )

    def test_rejects_bad_marker(self):
        for version in (
            "0.2.0-dev.11.0D01",
            "0.2.0-dev.11.1D1",
            "0.2.0-dev.11.1d01",
            "0.2.0-dev.11",
        ):
            with self.subTest(version=version):
                with self.assertRaises(ValueError):
                    MODULE.validate(
                        version, "darwin-aarch64",
                        "https://example.com/artifact", "test-signature"
                    )

    def test_rejects_http_and_missing_signature(self):
        with self.assertRaises(ValueError):
            MODULE.validate(
                "0.2.0-dev.11.1D01", "darwin-aarch64",
                "http://example.com/artifact", "test-signature"
            )
        with self.assertRaises(ValueError):
            MODULE.validate(
                "0.2.0-dev.11.1D01", "darwin-aarch64",
                "https://example.com/artifact", " "
            )

    def test_rejects_unknown_target(self):
        with self.assertRaises(ValueError):
            MODULE.validate(
                "0.2.0-dev.11.1D01", "darwin-x86_64",
                "https://example.com/artifact", "test-signature"
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
