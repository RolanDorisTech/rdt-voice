import base64
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_artifact_manifest import build_manifest
from verify_release_artifact_manifest import verify


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.package = self.root / "package"
        self.evidence = self.root / "evidence"
        self.package.mkdir()
        self.evidence.mkdir()
        self.config = self.root / "tauri.conf.json"
        self.config.write_text(json.dumps({
            "version": "0.2.0-dev.12.5L001",
            "plugins": {"updater": {
                "pubkey": base64.b64encode(b"test public key").decode()
            }},
        }), encoding="utf-8")
        self.manifest_path = self.root / "release-manifest.json"
        self.minisign = self.root / "minisign-test"
        self.minisign.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.minisign.chmod(0o755)

        app = self.add_pair("RDT-Connect-x64.AppImage", b"appimage-payload")
        sig = self.add_pair(app.name + ".sig", base64.b64encode(b"test-signature"))
        deb = self.package / "rdt-connect-x64.deb"
        deb.write_bytes(b"debian-package")
        (self.evidence / "inference.json").write_text(
            '{"result":"fixture-only"}\n', encoding="utf-8"
        )

        artifact = "rdt-connect-linux-x64-qualification-12345-2"
        build_args = SimpleNamespace(
            platform="linux", architecture="x64", marker="5L001",
            source_sha="a" * 40,
            repository="RolanDorisTech/rdt-voice",
            run_id="12345", run_attempt="2",
            source_ref="refs/heads/test-branch",
            workflow_name="Windows and Linux four-target native qualification",
            tauri_config=self.config,
            asset=[
                str(app), str(app.with_name(app.name + ".sha256")),
                str(sig), str(sig.with_name(sig.name + ".sha256")), str(deb)
            ],
            artifact_name=[artifact],
            qualification_artifact_name=[artifact],
            evidence=[self.evidence],
            output=self.manifest_path,
        )
        manifest = build_manifest(build_args)
        self.manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8"
        )
        self.args = SimpleNamespace(
            manifest=self.manifest_path,
            asset_root=[self.package],
            evidence_root=[self.evidence],
            platform="linux", architecture="x64", marker="5L001",
            repository="RolanDorisTech/rdt-voice",
            source_sha="a" * 40, source_ref="refs/heads/test-branch",
            run_id="12345", run_attempt="2",
            workflow_name="Windows and Linux four-target native qualification",
            tauri_config=self.config, minisign=str(self.minisign),
        )

    def tearDown(self):
        self.temp.cleanup()

    def add_pair(self, name, payload):
        path = self.package / name
        path.write_bytes(payload)
        checksum = hashlib.sha256(payload).hexdigest()
        (self.package / (name + ".sha256")).write_text(
            checksum + "  " + name + "\n", encoding="ascii"
        )
        return path

    def test_valid_exact_run_and_signature(self):
        verify(self.args)

    def test_wrong_commit_rejected(self):
        self.args.source_sha = "b" * 40
        with self.assertRaisesRegex(ValueError, "source commit mismatch"):
            verify(self.args)

    def test_tampered_evidence_rejected(self):
        (self.evidence / "inference.json").write_text("changed\n")
        with self.assertRaisesRegex(ValueError, "evidence inventory mismatch"):
            verify(self.args)

    def test_empty_qualification_evidence_rejected(self):
        data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        data["evidence"] = {}
        self.manifest_path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "manifest contains no qualification evidence"):
            verify(self.args)

    def test_missing_evidence_root_rejected(self):
        self.args.evidence_root = [self.root / "missing-evidence"]
        with self.assertRaisesRegex(ValueError, "qualification evidence directory missing"):
            verify(self.args)

    def test_wrong_run_attempt_rejected(self):
        self.args.run_attempt = "3"
        with self.assertRaisesRegex(ValueError, "run attempt mismatch"):
            verify(self.args)

    def test_wrong_platform_marker_rejected(self):
        self.args.marker = "3A01"
        with self.assertRaisesRegex(ValueError, "marker/platform mismatch"):
            verify(self.args)


if __name__ == "__main__":
    unittest.main(verbosity=2)
