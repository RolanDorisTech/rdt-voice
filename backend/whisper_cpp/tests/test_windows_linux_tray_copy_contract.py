import json
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
LOCALES = ROOT / "rdt_connect/src/locales"
MAIN = ROOT / "rdt_connect/src/main.ts"
HTML = ROOT / "rdt_connect/index.html"


class WindowsLinuxTrayCopyContractTests(unittest.TestCase):
    def test_all_26_locales_have_windows_and_linux_tray_copy(self):
        files = sorted(LOCALES.glob("*.json"))
        self.assertEqual(len(files), 26)
        for path in files:
            with self.subTest(locale=path.stem):
                messages = json.loads(path.read_text(encoding="utf-8"))["messages"]
                for key in (
                    "msg.show_in_notification_area.1d47",
                    "msg.keep_running_in_notification_area_on_close.1d47",
                    "msg.show_in_system_tray.1d47",
                    "msg.keep_running_in_system_tray_on_close.1d47",
                ):
                    self.assertTrue(messages.get(key), f"{path.name}: missing {key}")

    def test_windows_notification_area_and_linux_system_tray_are_separate(self):
        source = MAIN.read_text(encoding="utf-8")
        self.assertIn("if (isWindowsRuntime())", source)
        self.assertIn("else if (isLinuxRuntime())", source)
        self.assertIn("msg.show_in_notification_area.1d47", source)
        self.assertIn("msg.keep_running_in_notification_area_on_close.1d47", source)
        self.assertIn("msg.show_in_system_tray.1d47", source)
        self.assertIn("msg.keep_running_in_system_tray_on_close.1d47", source)
        self.assertNotIn("notification center", source.lower())

    def test_mlx_only_controls_are_hidden_on_windows_and_linux(self):
        source = MAIN.read_text(encoding="utf-8")
        self.assertIn('querySelector<HTMLElement>(".inference-device-card")', source)
        self.assertIn('querySelector<HTMLElement>(".dev-benchmark-controls > p:first-child")', source)
        self.assertIn("if (isWindowsOrLinuxRuntime())", source)
        self.assertIn('inferenceDeviceCard?.classList.add("hidden")', source)
        self.assertIn('mlxBenchmarkDescription?.classList.add("hidden")', source)
        html = (ROOT / "rdt_connect/index.html").read_text(encoding="utf-8")
        self.assertIn('id="open-native-benchmark-button"', html)

    def test_macos_menu_bar_text_remains_in_i18n_pipeline(self):
        html = HTML.read_text(encoding="utf-8")
        self.assertIn('data-l10n-key="msg.show_in_menu_bar.d24a581a"', html)
        self.assertIn('data-l10n-key="msg.', html)

    def test_linux_tray_builder_failure_is_nonfatal_and_disables_close_to_tray(self):
        source = (ROOT / "rdt_connect/src-tauri/src/main.rs").read_text(encoding="utf-8")
        self.assertIn('let tray_build_result = TrayIconBuilder::with_id("rdt-connect-menu-bar")', source)
        linux_start = source.index('#[cfg(target_os = "linux")]\n            {', source.index("tray_build_result"))
        other_start = source.index('#[cfg(not(target_os = "linux"))]', linux_start)
        linux_branch = source[linux_start:other_start]
        self.assertIn("Err(error) =>", linux_branch)
        self.assertIn("Continuing without tray integration.", linux_branch)
        self.assertIn("state.keep_in_menu_bar.store(false, Ordering::SeqCst);", linux_branch)
        self.assertIn("state.set_keep_in_menu_bar(false)", linux_branch)
        self.assertIn("tray_build_result?;", source[other_start:])

    def test_linux_deb_and_ci_require_appindicator_runtime(self):
        config = json.loads((ROOT / "rdt_connect/src-tauri/tauri.linux.conf.json").read_text(encoding="utf-8"))
        depends = config["bundle"]["linux"]["deb"]["depends"]
        self.assertIn("libvulkan1", depends)
        self.assertIn("libayatana-appindicator3-1", depends)
        workflow = (ROOT / ".github/workflows/linux-native-qualification.yml").read_text(encoding="utf-8")
        self.assertIn('assert "libayatana-appindicator3-1" in depends', workflow)
        self.assertIn("libayatana-appindicator3-1", workflow)

    def test_linux_watcher_guards_close_to_tray(self):
        rust = (ROOT / "rdt_connect/src-tauri/src/main.rs").read_text(encoding="utf-8")
        self.assertIn("fn linux_status_notifier_watcher_available()", rust)
        self.assertIn('"org.kde.StatusNotifierWatcher"', rust)
        self.assertIn("NameHasOwner", rust)
        self.assertIn("state.keep_in_menu_bar() && linux_status_notifier_watcher_available()", rust)
        self.assertIn("enabled && !linux_status_notifier_watcher_available()", rust)
        self.assertIn("let keep = keep && linux_status_notifier_watcher_available();", rust)

    def test_linux_appimage_explicitly_bundles_and_checks_tray_library(self):
        workflow = (ROOT / ".github/workflows/linux-native-qualification.yml").read_text(encoding="utf-8")
        self.assertIn("TRAY_LIBRARY_PATH=", workflow)
        self.assertIn("libayatana-appindicator3.so.1", workflow)
        self.assertIn("unsquashfs -ll -offset", workflow)

    def test_dbus_watcher_dependency_is_linux_only(self):
        manifest = tomllib.loads((ROOT / "rdt_connect/src-tauri/Cargo.toml").read_text(encoding="utf-8"))
        linux = manifest["target"]['cfg(target_os = "linux")']["dependencies"]
        self.assertIn("dbus", linux)
