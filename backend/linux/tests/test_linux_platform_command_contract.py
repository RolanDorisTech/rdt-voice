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


    def test_cpp_cpu_only_device_listing_declares_gpu_variables_in_scope(self):
        worker = (
            REPO_ROOT / "backend" / "whisper_cpp" / "worker"
            / "rdt_whisper_worker.cpp"
        ).read_text(encoding="utf-8")
        start = worker.index("static int list_provider_devices() {")
        end = worker.find("int main(", start)
        if end < 0:
            end = len(worker)
        function = worker[start:end]

        cuda_guard = function.index("#if defined(RDT_ENABLE_CUDA)")
        registry_decl = function.index("const char *registry_name = nullptr;")
        provider_decl = function.index("const char *provider_name = nullptr;")
        self.assertLess(registry_decl, cuda_guard)
        self.assertLess(provider_decl, cuda_guard)

        self.assertIn('registry_name = "CUDA";', function)
        self.assertIn('provider_name = "cuda";', function)
        self.assertIn('registry_name = "Vulkan";', function)
        self.assertIn('provider_name = "vulkan";', function)

        cpu_device_line = next(
            (
                line for line in function.splitlines()
                if '"DEVICE' in line and "CPU" in line and "host" in line
            ),
            None,
        )
        self.assertIsNotNone(cpu_device_line)
        self.assertRegex(cpu_device_line, r'"DEVICE.*cpu.*CPU.*cpu.*host')
        self.assertIn("return 0;", function)


if __name__ == "__main__":
    unittest.main()
