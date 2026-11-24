from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[3]
MAIN_RS = REPO_ROOT / "rdt_connect" / "src-tauri" / "src" / "main.rs"


class LinuxPlatformCommandContractTests(unittest.TestCase):
    def test_youtube_channel_uses_linux_desktop_opener(self):
        source = MAIN_RS.read_text(encoding="utf-8")
        start = source.index(
            "#[tauri::command]\nfn open_youtube_channel() -> Result<(), String> {"
        )
        end = source.index("\nfn native_benchmark_edit_distance", start)
        function = source[start:end]

        self.assertIn('#[cfg(target_os = "linux")]', function)
        self.assertIn('Command::new("xdg-open")', function)
        self.assertIn('Command::new("/usr/bin/open")', function)
        self.assertIn('open_url_windows(URL, Some("default"))', function)
        self.assertIn(".arg(URL)", function)
        self.assertIn(".spawn()", function)
        self.assertIn(".map_err(|error|", function)


if __name__ == "__main__":
    unittest.main()
