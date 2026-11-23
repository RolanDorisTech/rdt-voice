import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


class WindowsLinuxPlatformUiContractTests(unittest.TestCase):
    def test_apple_model_fallbacks_are_removed_only_on_windows_and_linux(self):
        source = (ROOT / "rdt_connect/src/main.ts").read_text(encoding="utf-8")
        html = (ROOT / "rdt_connect/index.html").read_text(encoding="utf-8")

        self.assertIn(
            "function hideAppleModelFallbackChoicesOnWindowsLinux()", source
        )
        self.assertIn("if (!isWindowsOrLinuxRuntime()) return;", source)
        self.assertIn(
            'option.value.toLowerCase().startsWith("mlx-community/")', source
        )
        self.assertIn('querySelector<HTMLSelectElement>("#model-select")', source)
        self.assertIn('querySelector<HTMLElement>("#model-id-detail")', source)

        # Keep the original Apple MLX entries in shared HTML for macOS.
        self.assertIn(
            'value="mlx-community/whisper-tiny-mlx"', html
        )

    def test_platform_copy_survives_locale_translation_refresh(self):
        source = (ROOT / "rdt_connect/src/main.ts").read_text(encoding="utf-8")

        self.assertIn(
            'translateKey("msg.show_in_notification_area.1d47"', source
        )
        self.assertIn(
            'translateKey("msg.show_in_system_tray.1d47"', source
        )
        self.assertIn(
            'new MutationObserver(() => {\n    syncKeepInMenuBarPlatformCopy();',
            source,
        )
        self.assertIn(
            "heading.textContent !== headingText", source
        )
        self.assertIn(
            "description.textContent !== descriptionText", source
        )


    def test_remote_access_copy_is_platform_neutral(self):
        html = (ROOT / "rdt_connect/index.html").read_text(encoding="utf-8")
        css = (ROOT / "rdt_connect/src/style.css").read_text(encoding="utf-8")
        start = html.index('<section id="view-remote"')
        end = html.index('id="settings-security"', start)
        remote = html[start:end]

        for phrase in ("your Mac", "this Mac", "Transcribe on Mac",
                       "for macOS", "install/mac"):
            self.assertNotIn(phrase.casefold(), remote.casefold())
        self.assertIn("Transcribe on this computer", remote)
        self.assertIn("Install Tailscale on this computer", remote)
        self.assertIn('href="https://tailscale.com/docs/install"', remote)
        self.assertIn("remote-flow-desktop", html)
        self.assertIn(".remote-flow-desktop", css)

    def test_certificate_copy_uses_the_actual_platform_trust_store(self):
        source = (ROOT / "rdt_connect/src/main.ts").read_text(encoding="utf-8")
        self.assertIn("function platformLocalTrustStoreTooltip()", source)
        self.assertIn("msg.add_the_rdt_connect_local_root_ca_to_the_windows_current_user_certificate_trust_store", source)
        self.assertIn("msg.add_the_rdt_connect_local_root_ca_to_this_computer_s_system_trust_store", source)
        self.assertIn("this Mac's login Keychain", source)
        self.assertIn("syncPlatformSecurityCopy();", source)
        self.assertIn('window.addEventListener("rdt-connect-language-changed"', source)
        self.assertIn("trust_local_https_ca", source)

    def test_platform_trust_translations_exist_in_all_locales(self):
        import json

        locale_dir = ROOT / "rdt_connect/src/locales"
        files = sorted(locale_dir.glob("*.json"))
        self.assertEqual(len(files), 26)

        keys = (
            "msg.add_the_rdt_connect_local_root_ca_to_the_windows_current_user_certificate_trust_store_rdt_connect_records_its_exact_fingerprint_so_it_can_remove_only_this_entry_later.15d3811e",
            "msg.add_the_rdt_connect_local_root_ca_to_this_computer_s_system_trust_store_your_system_will_ask_for_administrator_authorization_the_ca_is_restricted_to_localhost_127_0_0_1_and_1_and_rdt_connect_records_its_exact_fingerprint_so_it_can_remove_only_this_entry_later_firefox_and_chrome_chromium_may_keep_separate_certificate_stores_and_are_not_changed.50cb7d5d",
        )

        def has_key(value, key):
            if isinstance(value, dict):
                return key in value or any(has_key(v, key) for v in value.values())
            if isinstance(value, list):
                return any(has_key(v, key) for v in value)
            return False

        missing = []
        for path in files:
            data = json.loads(path.read_text(encoding="utf-8"))
            for key in keys:
                if not has_key(data, key):
                    missing.append(f"{path.name}:{key[-12:]}")
        self.assertFalse(missing, "Missing trust translations: " + ", ".join(missing))


if __name__ == "__main__":
    unittest.main()
