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
        self.assertIn("RDT Connect · version/build information loading…", html)
        self.assertNotIn("Version 0.2.0-dev.10 (0.2.0-dev.10)", html)

    def test_original_icon_and_about_navigation(self):
        rust = (ROOT / "src-tauri/src/main.rs").read_text(encoding="utf-8")
        self.assertIn('icons/rdt-menu-bar-icon.png', rust)
        self.assertIn('.icon_as_template(false)', rust)
        self.assertNotIn('rdt-menu-bar-template-0D45.png', rust)
        self.assertIn('"app-menu-about-rdt-connect" => {', rust)
        self.assertIn('let _ = app.emit("rdt-connect-open-about", ());', rust)
        self.assertNotIn('show_about_window(app)', rust)
        self.assertIn('"app-menu-check-updates" => {', rust)
        self.assertIn('if let Err(error) = show_update_window(app)', rust)
        start = rust.index('let app_submenu = Submenu::with_items(')
        end = rust.index(';', start) + 1
        submenu = rust[start:end]
        self.assertNotRegex(submenu, r'(?i)github|youtube')

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

    def test_update_window_auto_dismisses_only_passive_results(self):
        update = (ROOT / "src/update-window.ts").read_text(encoding="utf-8")
        self.assertIn('getCurrentWindow().close()', update)
        self.assertIn('state.key === "updater.upToDate"', update)
        self.assertIn('state.key === "updater.checkFailed"', update)
        self.assertIn('currentAction === null', update)
        self.assertIn('currentAction === "reopen"', update)

    def test_reopen_action_and_update_reliability_remain(self):
        main = (ROOT / "src/main.ts").read_text(encoding="utf-8")
        self.assertIn('await update.install({ restartAfterInstall: false })', main)
        self.assertIn('setApplicationUpdateAction("reopen")', main)
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
