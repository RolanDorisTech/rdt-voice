#!/usr/bin/env python3
from pathlib import Path
import json
import unittest
ROOT = Path(__file__).resolve().parents[1]

class AboutMenuUpdateContractTests(unittest.TestCase):
    def test_about_version_retains_runtime_build_id(self):
        main = (ROOT / "src/main.ts").read_text(encoding="utf-8")
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn('msg.rdt_connect_app_version_label_build_build_id.62522b9b', main)
        self.assertIn('BUILD_ID: info.buildId', main)
        self.assertNotIn("aboutVersionObserver", main)
        self.assertIn('id="about-version-label"', html)
        build_id = (ROOT / "scripts/build-id-sequence.txt").read_text(encoding="utf-8").strip()
        self.assertIn(f'id="header-build-id">{build_id}</span>', html)
        self.assertNotIn("Version 0.2.0-dev.10 (0.2.0-dev.10)", html)

    def test_original_icon_and_about_navigation(self):
        rust = (ROOT / "src-tauri/src/main.rs").read_text(encoding="utf-8")
        self.assertIn('tauri::include_image!("icons/rdt-menu-bar-template.png")', rust)
        self.assertIn('tauri::include_image!("icons/rdt-menu-bar-icon.png")', rust)
        self.assertIn('.icon_as_template(tray_icon_is_template)', rust)
        self.assertIn('#[cfg(target_os = "macos")]', rust)
        self.assertIn('#[cfg(not(target_os = "macos"))]', rust)
        self.assertNotIn('rdt-menu-bar-template-0D45.png', rust)

        lines = rust.splitlines()

        def menu_arm(item_id):
            target = f'"{item_id}" => {{'
            starts = [i for i, line in enumerate(lines) if line.strip() == target]
            self.assertEqual(len(starts), 1, f"Expected one {item_id} menu arm")
            body = [lines[starts[0]]]
            for line in lines[starts[0] + 1:]:
                stripped = line.strip()
                if (stripped.startswith('"') and '=>' in stripped) or stripped.startswith('_ =>'):
                    break
                body.append(line)
            return "\\n".join(body)

        for item_id in ("about-app", "tray-about-app"):
            action = menu_arm(item_id)
            self.assertIn('app.run_on_main_thread(|| show_about_panel_on_top())', action)
            self.assertNotIn('show_main_window(app)', action)
            self.assertNotIn('rdt-connect-open-about', action)

        panel_start = rust.index('fn show_about_panel_on_top()')
        panel_end = rust.index('fn restore_dock_icon()', panel_start)
        panel = rust[panel_start:panel_end]
        self.assertIn('application.activate();', panel)
        self.assertIn('NSModalPanelWindowLevel', panel)
        self.assertIn('window.orderFrontRegardless();', panel)
        self.assertNotIn('show_main_window(app)', panel)
        self.assertNotIn('show_about_window(app)', rust)
        self.assertIn('"app-menu-check-updates" => {', rust)
        self.assertIn('if let Err(error) = show_update_window(app, true)', rust)
        start = rust.index('let app_submenu = Submenu::with_items(')
        end = rust.index(';', start) + 1
        self.assertNotRegex(rust[start:end], r'(?i)github|youtube')





    def test_native_about_credits_show_build_and_exact_links(self):
        credits = (ROOT / "src-tauri/resources/Credits.html").read_text(encoding="utf-8")
        build_id = (ROOT / "scripts/build-id-sequence.txt").read_text(encoding="utf-8").strip()
        self.assertIn(f'<p class="build">Build {build_id}</p>', credits)
        self.assertIn(
            '<a href="https://github.com/RolanDorisTech/rdt-voice">RDT Voice on GitHub</a>',
            credits,
        )
        self.assertIn(
            '<a href="https://youtube.com/@RolanDorisTech">Support us on YouTube</a>',
            credits,
        )
        self.assertEqual(credits.count("<a href="), 2)
        config = (ROOT / "src-tauri/tauri.conf.json").read_text(encoding="utf-8")
        self.assertIn('"resources/Credits.html": "Credits.html"', config)

    def test_original_about_card_and_exact_destinations(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="brand-home-link" href="https://rolandoristech.github.io/rdt-voice/"', html)
        self.assertIn('id="about-company-link" class="about-company-link" href="https://github.com/RolanDorisTech/rdt-voice"', html)
        self.assertIn('id="about-youtube-button" class="button secondary"', html)
        self.assertIn('>Support us on YouTube ↗</button>', html)
        self.assertIn('© 2026 Rolan &amp; Doris Tech', html)
        for target in (
            'href="https://github.com/RolanDorisTech/rdt-voice/blob/main/LICENSE"',
            'href="https://github.com/RolanDorisTech/rdt-voice/blob/main/COMMERCIAL_LICENSE.md"',
            'href="https://github.com/RolanDorisTech/rdt-voice/blob/main/LICENSE_HISTORY.md"',
            'href="https://github.com/RolanDorisTech/rdt-voice/blob/main/README.md#acknowledgements-and-third-party-licenses"',
        ):
            self.assertIn(target, html)
        main = (ROOT / "src/main.ts").read_text(encoding="utf-8")
        self.assertIn('await invoke("open_youtube_channel")', main)
        self.assertIn('aboutYoutubeButton.addEventListener("click"', main)

def test_update_window_keeps_passive_results_visible_and_retryable(self):
    update = (ROOT / "src" / "update-window.ts").read_text(encoding="utf-8")
    self.assertIn('state.key === "updater.upToDate"', update)
    self.assertIn('state.key === "updater.checkFailed"', update)
    self.assertIn("currentAction = state.action ?? null;", update)
    self.assertIn(
        'checkButton.hidden = currentAction === "install" || currentAction === "installing" || currentAction === "reopen";',
        update,
    )
    self.assertIn('checkButton.addEventListener("click"', update)
    self.assertIn('void emit("rdt-connect-update-check-request", {})', update)
    self.assertNotIn("getCurrentWindow().close()", update)
    def test_reopen_action_and_update_reliability_remain(self):
        main = (ROOT / "src/main.ts").read_text(encoding="utf-8")
        self.assertIn('await update.install({ restartAfterInstall: false })', main)
        self.assertIn('if (status === "restart-required") return "reopen";', main)
        self.assertIn('rdt-connect-update-relaunch-request', main)
        css = (ROOT / "src/style.css").read_text(encoding="utf-8")
        self.assertIn('rdtDownloadSpotlightCue0D47', css)
        self.assertIn('prefers-reduced-motion: reduce', css)
        lock = json.loads((ROOT.parent / "backend/rdt_model_lock.json").read_text(encoding="utf-8"))
        self.assertEqual(lock.get("official_endpoint"), "https://huggingface.co")
        self.assertEqual(len(lock.get("models", {})), 5)
        for repo, record in lock["models"].items():
            self.assertRegex(record.get("revision", ""), r"^[0-9a-f]{40}$", repo)
            self.assertRegex(record.get("sha256", ""), r"^[0-9a-f]{64}$", repo)

if __name__ == "__main__":
    unittest.main(verbosity=2)
