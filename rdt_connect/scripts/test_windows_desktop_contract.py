#!/usr/bin/env python3
"""Static regression guards for Windows-only native behavior.

These tests do not replace a Windows build or VM test. They prevent accidental
reintroduction of source-known blockers in browser dispatch and TLS trust scope.
"""
from __future__ import annotations

from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "rdt_connect" / "src-tauri" / "src" / "main.rs"


def block_between(source: str, start: str, end: str) -> str:
    begin = source.index(start)
    finish = source.index(end, begin + len(start))
    return source[begin:finish]


class WindowsBrowserContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = MAIN.read_text(encoding="utf-8")

    def test_windows_url_paths_never_use_explorer_as_browser(self) -> None:
        self.assertNotIn("explorer.exe", self.source.lower())
        self.assertIn("fn open_url_windows", self.source)
        self.assertIn("ShellExecuteW(", self.source)

    def test_windows_browser_discovery_checks_registry_and_existing_executable(self) -> None:
        block = block_between(self.source, "fn windows_browser_executable", "#[cfg(windows)]\nfn browser_app_is_installed")
        self.assertIn("let exe_names", block)
        self.assertIn("HKEY_CURRENT_USER", block)
        self.assertIn("HKEY_LOCAL_MACHINE", block)
        self.assertIn("App Paths", block)
        self.assertIn("candidate.is_file()", block)
        self.assertIn("windows_browser_executable(selected)", self.source)
        self.assertIn("fall through to the registered system default", self.source)

    def test_explicit_browser_selection_is_not_discarded(self) -> None:
        block = block_between(self.source, "fn open_voice_window(", "fn request_builder(")
        self.assertIn("open_url_windows(launch_url.as_str(), browser_id.as_deref())", block)
        model_manager = block_between(self.source, "fn open_easywhisper_model_manager", "struct ModelStorageConfig")
        self.assertIn("open_url_windows(url, browser_id.as_deref())", model_manager)

    def test_official_link_destinations_are_pinned(self) -> None:
        self.assertIn('const GITHUB: &str = "https://github.com/RolanDorisTech/rdt-voice";', self.source)
        self.assertIn('const WEBSITE: &str = "https://rolandoristech.github.io/rdt-voice/";', self.source)
        windows_open = block_between(self.source, "fn open_url_windows", "#[cfg(target_os = \"macos\")]\nfn open_url_external")
        self.assertIn('Some("rolandoristech.github.io" | "github.com" | "youtube.com" | "www.youtube.com")', windows_open)
        self.assertIn('"mailto" => parsed.path() == "woknewsbusiness@gmail.com"', windows_open)



    def test_arm64_vulkan_sdk_urls_are_plain_url_strings(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "windows-arm64-installer.yml").read_text(encoding="utf-8")
        download_line = next(line for line in workflow.splitlines() if "$download =" in line)
        checksum_line = next(line for line in workflow.splitlines() if "$checksumUri =" in line)

        self.assertEqual(download_line.strip(), '$download = "https://sdk.lunarg.com/sdk/download/$version/warm/vulkan_sdk.exe"')
        self.assertEqual(checksum_line.strip(), '$checksumUri = "https://sdk.lunarg.com/sdk/sha/$version/warm/vulkan_sdk.exe.json"')
        self.assertNotIn("[", download_line)
        self.assertNotIn("](", download_line)
        self.assertNotIn("[", checksum_line)
        self.assertNotIn("](", checksum_line)

        self.assertIn("$checksum = Invoke-RestMethod -Uri $checksumUri", workflow)
        self.assertIn('$expectedFile = "vulkansdk-windows-ARM64-$version.exe"', workflow)
        self.assertIn("$checksum.version -ne $version", workflow)
        self.assertIn("$checksum.platform -ne 'warm'", workflow)
        self.assertIn("$checksum.file -ne $expectedFile", workflow)
        self.assertIn("([string]$checksum.sha).ToLowerInvariant()", workflow)
        self.assertIn("^[0-9a-f]{64}$", workflow)
    def test_arm64_ffmpeg_preflight_checks_both_executables(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "windows-arm64-installer.yml").read_text(encoding="utf-8")
        start = workflow.index("      - name: Prepare Windows ARM64 FFmpeg")
        end = workflow.index("\n      - name:", start + 1)
        step = workflow[start:end]
        self.assertIn("$ffmpegVersion = & (Join-Path $destination 'ffmpeg.exe') -version", step)
        self.assertIn("$ffprobeVersion = & (Join-Path $destination 'ffprobe.exe') -version", step)
        self.assertGreaterEqual(step.count("if ($LASTEXITCODE -ne 0)"), 2)


class WindowsTlsContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = MAIN.read_text(encoding="utf-8")

    def test_windows_certificate_key_usage_uses_supported_powershell_parameters(self) -> None:
        generator = block_between(
            self.source,
            "fn windows_generate_tls_files",
            "fn validate_windows_tls_material",
        )
        self.assertIn("-KeyUsage CertSign, CRLSign", generator)
        self.assertIn("-KeyUsage DigitalSignature, KeyEncipherment", generator)
        self.assertNotIn("2.5.29.15={critical}{text}keyCertSign,cRLSign", generator)
        self.assertNotIn("2.5.29.15={critical}{text}digitalSignature,keyEncipherment", generator)

    def test_windows_default_is_https_and_has_no_automatic_fallback(self) -> None:
        config = self.source[self.source.index("impl Default for LocalTlsConfig"):self.source.index("fn load_local_tls_config")]
        self.assertIn('"https"', config)
        self.assertIn("RDT Connect will not silently fall back to HTTP", self.source)
        self.assertIn("validate_tls_material", self.source)
        self.assertIn("configured_certificate_trusted", self.source)

    def test_windows_roots_are_current_user_only(self) -> None:
        start = self.source.index("#[cfg(windows)]\nfn windows_root_store_contains")
        end = self.source.index("#[cfg(all(not(target_os = \"macos\"), not(windows)))]\nfn certificate_trusted", start)
        windows_tls = self.source[start:end]
        self.assertIn("StoreLocation]::CurrentUser", windows_tls)
        self.assertNotIn("StoreLocation]::LocalMachine", windows_tls)
        self.assertNotIn("certutil -addstore", windows_tls.lower())

    def test_generated_certificate_requires_loopback_san_and_server_auth(self) -> None:
        validator = block_between(self.source, "fn windows_validate_generated_tls_files", "fn windows_generate_tls_files")
        generator = block_between(self.source, "fn windows_generate_tls_files", "fn validate_windows_tls_material")
        self.assertIn("127\\.0\\.0\\.1", validator)
        self.assertIn("1.3.6.1.5.5.7.3.1", validator)
        self.assertIn("IPAddress=127.0.0.1", generator)
        self.assertIn("DNS=localhost", generator)
        self.assertIn("windows_private_key_sha256", self.source)

    def test_windows_tls_setup_reports_trusted_state_and_removes_only_tracked_fingerprints(self) -> None:
        setup = block_between(self.source, "fn setup_local_https_certificate_windows", "#[tauri::command]\nasync fn setup_local_https_certificate")
        removal = block_between(self.source, "fn remove_rdt_https_trust", "fn set_local_transport")
        self.assertIn("status.configured", setup)
        self.assertIn("status.trusted", setup)
        self.assertIn("managed_trust_fingerprints", removal)
        self.assertIn("windows_remove_root_from_current_user_store", removal)

    def test_windows_powershell_is_encoded_and_suppresses_console_windows(self) -> None:
        block = block_between(self.source, "fn run_powershell_script", "fn powershell_read_certificate")
        self.assertIn("-EncodedCommand", block)
        self.assertIn("creation_flags(0x08000000)", block)



    def test_save_local_tls_config_propagates_acl_failure(self) -> None:
        source = (ROOT / "rdt_connect" / "src-tauri" / "src" / "main.rs").read_text(encoding="utf-8")
        routine = source.split("fn save_local_tls_config(", 1)[1].split("\nfn sha256_file_hex", 1)[0]
        self.assertIn("set_private_permissions(&state.tls_config_path, 0o600)", routine)
        self.assertIn(".map_err(|error| format!(", routine)
        self.assertNotIn("let _ = set_private_permissions", routine)


class WindowsTlsUiContractTests(unittest.TestCase):
    def test_native_ggml_ui_reaches_trusted_https_setup_command(self) -> None:
        ui_source = (ROOT / "rdt_connect" / "src" / "main.ts").read_text(encoding="utf-8")
        start = ui_source.index("async function performLocalHttpsSetup(")
        end = ui_source.index("setupLocalHttpsButton.addEventListener", start)
        block = ui_source[start:end]
        self.assertNotIn("if (isNativeGgmlCatalog())", block)
        self.assertNotIn("Trusted local HTTPS certificate setup is not implemented", block)
        self.assertIn('invoke<LocalHttpsStatus>("setup_local_https_certificate")', block)


class WindowsPackagingContractTests(unittest.TestCase):
    def test_ffmpeg_input_is_immutable_and_checksum_is_enforced(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "windows-arm64-installer.yml").read_text(encoding="utf-8")
        self.assertIn("autobuild-2026-10-19-13-06/ffmpeg-N-127696-g8b3525e718-winarm64-lgpl.zip", workflow)
        self.assertNotIn("releases/download/latest/", workflow)
        self.assertIn("$expectedSha256 = 'd4fd29fa9128fc1ba623e23b2ea1f13f283c646eb108c8583854371b2ba3d9d4'", workflow)
        self.assertIn("if ($actualSha256 -ne $expectedSha256)", workflow)
        self.assertIn('throw \"Pinned FFmpeg archive SHA-256 mismatch', workflow)



class WindowsVersionStampContractTests(unittest.TestCase):
    def test_platform_marker_files_use_reserved_namespaces(self) -> None:
        connect = ROOT / "rdt_connect" / "scripts"
        self.assertEqual((connect / "build-id-sequence-windows.txt").read_text(encoding="utf-8").strip(), "2A05")
        self.assertEqual((connect / "build-id-sequence-linux.txt").read_text(encoding="utf-8").strip(), "4L001")
        self.assertEqual((connect / "build-id-sequence.txt").read_text(encoding="utf-8").strip(), "1D01")

    def test_windows_workflows_stamp_before_native_build(self) -> None:
        workflows = [
            ROOT / ".github" / "workflows" / "windows-arm64-installer.yml",
            ROOT / ".github" / "workflows" / "windows-port-check.yml",
        ]
        for path in workflows:
            source = path.read_text(encoding="utf-8")
            self.assertIn("node scripts/stamp-platform-build.cjs windows", source, path.name)
            self.assertIn("test-platform-build-stamping.cjs", source, path.name)
        build_script = (ROOT / "rdt_connect" / "src-tauri" / "build.rs").read_text(encoding="utf-8")
        self.assertIn("build-id-sequence-windows.txt", build_script)
        self.assertIn("build-id-sequence-linux.txt", build_script)
        self.assertIn("verify_package_version_marker", build_script)


if __name__ == "__main__":
    unittest.main(verbosity=2)
