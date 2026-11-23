import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / ".github/scripts/validate-tauri-updater-config.py"
spec = importlib.util.spec_from_file_location("tauri_updater_validator", SCRIPT)
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


class LinuxTauriUpdaterConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = json.loads(
            (ROOT / "rdt_connect/src-tauri/tauri.conf.json").read_text()
        )
        cls.overlay = json.loads(
            (ROOT / "rdt_connect/src-tauri/tauri.linux.conf.json").read_text()
        )
        cls.workflow = (
            ROOT / ".github/workflows/linux-native-qualification.yml"
        ).read_text()

    def test_effective_linux_config_inherits_signed_updater_policy(self):
        effective = validator.validate_linux(self.base, self.overlay, self.workflow)
        updater = effective["plugins"]["updater"]
        self.assertTrue(updater["pubkey"])
        self.assertTrue(updater["requireSignedVersion"])
        self.assertTrue(effective["bundle"]["createUpdaterArtifacts"])

    def test_linux_overlay_does_not_need_to_duplicate_base_public_key(self):
        self.assertNotIn("pubkey", self.overlay.get("plugins", {}).get("updater", {}))
        effective = validator.merge_patch(self.base, self.overlay)
        self.assertIn("pubkey", effective["plugins"]["updater"])

    def test_merge_patch_null_removes_inherited_resource_mapping(self):
        merged = validator.merge_patch(
            {"bundle": {"resources": {"resources/third_party_notices": "third_party_notices"}}},
            {"bundle": {"resources": {"resources/third_party_notices": None}}},
        )
        self.assertNotIn("resources/third_party_notices", merged["bundle"]["resources"])

    def test_http_updater_endpoint_is_rejected(self):
        overlay = json.loads(json.dumps(self.overlay))
        overlay["plugins"]["updater"]["endpoints"] = [
            endpoint.replace("https://", "http://")
            for endpoint in overlay["plugins"]["updater"]["endpoints"]
        ]
        with self.assertRaises(AssertionError):
            validator.validate_linux(self.base, overlay, self.workflow)

    def test_unsigned_updater_policy_is_rejected(self):
        base = json.loads(json.dumps(self.base))
        base["plugins"]["updater"]["requireSignedVersion"] = False
        with self.assertRaises(AssertionError):
            validator.validate_linux(base, self.overlay, self.workflow)


if __name__ == "__main__":
    unittest.main()


class LinuxAppImageSignatureVerificationTests(unittest.TestCase):
    def test_appimage_signature_is_cryptographically_verified(self):
        workflow = Path(".github/workflows/linux-native-qualification.yml").read_text(encoding="utf-8").lower()
        self.assertIn('base64 --decode "$appimage.sig"', workflow)
        self.assertIn('minisign -v -m "$appimage"', workflow)
        self.assertIn("tauri-updater.pub", workflow)
