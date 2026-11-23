#!/usr/bin/env python3
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

class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = self.root / "tauri.conf.json"
        self.write_config("0.2.0-dev.12.5L001")

    def tearDown(self):
        self.tmp.cleanup()

    def write_config(self, version):
        self.config.write_text(json.dumps({
            "version": version,
            "plugins": {"updater": {"pubkey": base64.b64encode(b"test key").decode()}},
        }), encoding="utf-8")

    def add(self, name, data):
        path = self.root / name
        path.write_bytes(data)
        return path

    def pair(self, name, data):
        path = self.add(name, data)
        self.add(name + ".sha256",
                 (hashlib.sha256(data).hexdigest() + "  " + name + "\n").encode())
        return path

    def args(self, platform, arch, marker, assets):
        return SimpleNamespace(
            platform=platform, architecture=arch, marker=marker,
            source_sha="a" * 40, repository="RolanDorisTech/rdt-voice",
            run_id="12345", run_attempt="2", source_ref="refs/heads/test",
            workflow_name="Windows and Linux four-target native qualification",
            tauri_config=self.config, asset=[str(p) for p in assets],
            artifact_name=["release-artifact-12345-2"],
            qualification_artifact_name=["qualification-artifact-12345-2"],
            evidence=[],
            output=self.root / "release-manifest.json",
        )

    def test_linux_x64_manifest_and_sidecars(self):
        app = self.pair("RDT-Connect-x64.AppImage", b"appimage")
        sig = self.pair(app.name + ".sig", base64.b64encode(b"signature"))
        deb = self.add("rdt-connect.deb", b"deb")
        args = self.args("linux", "x64", "5L001", [
            app, app.with_name(app.name + ".sha256"),
            sig, sig.with_name(sig.name + ".sha256"), deb,
        ])
        manifest = build_manifest(args)
        self.assertEqual(manifest["target"]["build_marker"], "5L001")
        self.assertEqual(len(manifest["assets"]), 5)

    def test_linux_arm64_marker(self):
        self.write_config("0.2.0-dev.12.4L001")
        app = self.pair("RDT-Connect-arm64.AppImage", b"appimage")
        sig = self.pair(app.name + ".sig", base64.b64encode(b"signature"))
        deb = self.add("rdt-connect-arm64.deb", b"deb")
        args = self.args("linux", "ARM64", "4L001", [
            app, app.with_name(app.name + ".sha256"),
            sig, sig.with_name(sig.name + ".sha256"), deb,
        ])
        self.assertEqual(build_manifest(args)["target"]["architecture"], "ARM64")

    def test_windows_x64_manifest(self):
        self.write_config("0.2.0-dev.12.3A01")
        dist = self.pair("RDT-Connect-Windows-x64-DISTRIBUTION.zip", b"dist")
        guide = self.pair("RDT-Connect-Windows-Installation-Guide.zip", b"guide")
        installer = self.pair("RDT-Connect-0.2.0-dev.12.3A01-x64-setup.exe", b"exe")
        sig = self.pair(installer.name + ".sig", base64.b64encode(b"signature"))
        paths = [
            dist, dist.with_name(dist.name + ".sha256"),
            guide, guide.with_name(guide.name + ".sha256"),
            installer, installer.with_name(installer.name + ".sha256"),
            sig, sig.with_name(sig.name + ".sha256"),
        ]
        manifest = build_manifest(self.args("windows", "x64", "3A01", paths))
        self.assertEqual(manifest["target"]["architecture"], "x64")
        self.assertEqual(len(manifest["assets"]), 8)

    def test_windows_arm64_manifest(self):
        self.write_config("0.2.0-dev.12.2A05")
        dist = self.pair("RDT-Connect-Windows-ARM64-DISTRIBUTION.zip", b"arm64-dist")
        guide = self.pair("RDT-Connect-Windows-Installation-Guide.zip", b"guide")
        installer = self.pair("RDT-Connect-0.2.0-dev.12.2A05-arm64-setup.exe", b"arm64-exe")
        sig = self.pair(installer.name + ".sig", base64.b64encode(b"signature"))
        paths = [
            dist, dist.with_name(dist.name + ".sha256"),
            guide, guide.with_name(guide.name + ".sha256"),
            installer, installer.with_name(installer.name + ".sha256"),
            sig, sig.with_name(sig.name + ".sha256"),
        ]
        manifest = build_manifest(self.args("windows", "ARM64", "2A05", paths))
        self.assertEqual(manifest["target"]["architecture"], "ARM64")
        self.assertEqual(manifest["target"]["build_marker"], "2A05")
        self.assertEqual(len(manifest["assets"]), 8)

    def test_rejects_reserved_windows_l_marker(self):
        self.write_config("0.2.0-dev.12.3L001")
        with self.assertRaisesRegex(ValueError, "marker mismatch"):
            build_manifest(self.args("windows", "x64", "3L001", []))

    def test_rejects_checksum_mismatch(self):
        app = self.pair("RDT-Connect-x64.AppImage", b"correct")
        sig = self.pair(app.name + ".sig", base64.b64encode(b"signature"))
        deb = self.add("rdt-connect.deb", b"deb")
        app.write_bytes(b"tampered")
        paths = [
            app, app.with_name(app.name + ".sha256"),
            sig, sig.with_name(sig.name + ".sha256"), deb,
        ]
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            build_manifest(self.args("linux", "x64", "5L001", paths))

    def test_evidence_inventory_uses_relative_paths(self):
        from release_artifact_manifest import collect_evidence
        folder = self.root / "evidence"
        (folder / "logs").mkdir(parents=True)
        (folder / "logs" / "worker.txt").write_text("evidence", encoding="utf-8")
        inventory = collect_evidence([folder], folder / "release-manifest.json")
        self.assertIn("logs/worker.txt", inventory)
        self.assertNotIn(str(folder), " ".join(inventory))

    def test_missing_evidence_root_rejected(self):
        from release_artifact_manifest import collect_evidence
        missing = self.root / "missing-evidence"
        with self.assertRaisesRegex(ValueError, "evidence path missing or invalid"):
            collect_evidence([missing], self.root / "release-manifest.json")

    def test_symlink_evidence_root_rejected(self):
        from release_artifact_manifest import collect_evidence
        real = self.root / "real-evidence"
        real.mkdir()
        alias = self.root / "evidence-alias"
        alias.symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "evidence path must not be a symlink"):
            collect_evidence([alias], self.root / "release-manifest.json")


if __name__ == "__main__":
    unittest.main(verbosity=2)
