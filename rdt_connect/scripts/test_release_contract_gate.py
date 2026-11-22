#!/usr/bin/env python3
"""Mandatory build, lifecycle, watchdog and candidate-startup release contracts."""

from pathlib import Path
import json
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUST_PATH = ROOT / "src-tauri" / "src" / "main.rs"
BACKEND_PATH = ROOT.parent / "backend" / "apple" / "server.py"


def source_function(source: str, start: str, end: str) -> str:
    first = source.find(start)
    if first < 0:
        raise AssertionError(f"Required source declaration not found: {start}")
    last = source.find(end, first + len(start))
    if last < 0:
        raise AssertionError(f"Required source boundary not found after: {start}")
    return source[first:last]


class ReleaseContractGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rust = RUST_PATH.read_text(encoding="utf-8")
        cls.backend = BACKEND_PATH.read_text(encoding="utf-8")
        cls.build_script = (ROOT / "scripts" / "build-macos-with-marker.sh").read_text(
            encoding="utf-8"
        )
        cls.runner = (ROOT / "scripts" / "run-release-contracts.sh").read_text(
            encoding="utf-8"
        )
        cls.package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))

    def test_canonical_app_build_runs_gate_before_preparation_and_compilation(self):
        gate = self.build_script.find('bash "$ROOT/scripts/run-release-contracts.sh"')
        self.assertGreaterEqual(gate, 0, "canonical macOS build must invoke the release gate")
        for later_step in (
            'KEY_PATH="$HOME/.tauri/rdt-connect-updater.key"',
            "npm run prepare:desktop-resources",
            "npm run tauri -- build --bundles app",
        ):
            position = self.build_script.find(later_step)
            self.assertGreater(position, gate, f"release gate must precede {later_step}")

    def test_npm_release_and_custom_dmg_commands_cannot_bypass_gate(self):
        scripts = self.package["scripts"]
        self.assertEqual(
            scripts.get("test:release-contracts"),
            "bash scripts/run-release-contracts.sh",
        )
        self.assertIn(
            "scripts/build-macos-with-marker.sh",
            scripts["build:desktop:macos"],
        )
        self.assertIn(
            "scripts/build-macos-with-marker.sh",
            scripts["build:desktop:custom-dmg:macos"],
        )
        self.assertTrue(
            scripts["build:custom-dmg"].startswith(
                "bash scripts/run-release-contracts.sh && "
            ),
            "standalone custom-DMG npm packaging must run the release gate",
        )

    def test_gate_runs_lifecycle_benchmark_packaging_and_offline_regressions(self):
        required = (
            "test_connect_single_process_runtime_contract.py",
            "test_preserve_packager_resource_tracker_contract.py",
            "test_model_download_startup_contract.py",
            "test_model_download_reliability.py",
            "test-connect-benchmark-response-contract.py",
            "test-connect-benchmark-api.py",
            "test-offline-launch-contract.py",
            "test_macos_build_pipeline_contract.py",
            "test_release_contract_gate.py",
        )
        for name in required:
            with self.subTest(test=name):
                self.assertIn(name, self.runner)

    def test_macos_uses_os_reported_backend_executable_identity(self):
        match = re.search(
            r'#\[cfg\(target_os = "macos"\)\]\s*'
            r'fn process_is_expected_backend\(.*?\n\}',
            self.rust,
            re.DOTALL,
        )
        self.assertIsNotNone(match, "macOS executable identity verifier is required")
        body = match.group(0)
        self.assertIn("proc_pidpath", body)
        self.assertIn("command_is_expected_backend(path_text, expected_exe)", body)
        self.assertIn("return false", body)

    def test_unix_orphan_termination_revalidates_the_complete_group(self):
        match = re.search(
            r'#\[cfg\(unix\)\]\s*fn terminate_verified_backend_tree\(.*?(?=\n#\[cfg|\Z)',
            self.rust,
            re.DOTALL,
        )
        self.assertIsNotNone(match, "Unix process-group termination implementation is required")
        body = match.group(0)
        for token in (
            "process_snapshot_result()",
            "validate_backend_group_snapshot",
            "before_term",
            "before_kill",
            "send_signal_checked(-(group_id as i32), SIGNAL_TERM)",
            "send_signal_checked(-(group_id as i32), SIGNAL_KILL)",
        ):
            with self.subTest(token=token):
                self.assertIn(token, body)
        self.assertNotIn(
            "group_id == pid",
            body,
            "a verified helper PID may select a group whose leader is a different PID",
        )

    def test_group_validation_rejects_unrelated_members_and_pid_reuse(self):
        body = source_function(
            self.rust,
            "fn validate_backend_group_snapshot",
            "\n#[cfg(unix)]\nfn terminate_verified_backend_tree",
        )
        for token in (
            "item.pgid == group_id",
            "!item.stat.starts_with('Z')",
            "is_expected_backend(item)",
            "previous.started != item.started",
        ):
            with self.subTest(token=token):
                self.assertIn(token, body)

    def test_manual_fixture_uses_current_transcript_field(self):
        source = (ROOT / "scripts" / "test-manual-benchmark-fixture.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('reference = fixture.get("transcript")', source)
        self.assertNotIn('fixture.get("reference")', source)
        fixture = json.loads(
            (ROOT / "src-tauri" / "resources" / "benchmark" / "fixture.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIsInstance(fixture.get("transcript"), str)
        self.assertEqual(len(fixture["transcript"].split()), 50)

    def test_orphan_stop_uses_verified_identity_and_group_cleanup(self):
        body = source_function(
            self.rust,
            "fn stop_orphaned_backend_safely",
            "\n/// Stop the backend owned by this instance",
        )
        self.assertIn("verified_backend_process_snapshot", body)
        self.assertIn("terminate_verified_backend_tree", body)
        self.assertNotIn("port.ok_or_else", body)

    def test_backend_parent_watchdog_fails_closed_on_unknown_parent_identity(self):
        body = source_function(
            self.backend,
            "def _start_rdt_connect_parent_watchdog",
            "\n\n@app.get(\"/health\")",
        )
        for token in (
            "RDT_CONNECT_PARENT_PID",
            "RDT_CONNECT_PARENT_EXE",
            "os.kill(parent_pid, 0)",
            "if not current_executable:",
            "continue",
            "os.path.realpath(current_executable) != os.path.realpath(expected_exe)",
            "os.killpg(os.getpgrp(), signal.SIGTERM)",
        ):
            with self.subTest(token=token):
                self.assertIn(token, body)

    def test_candidate_start_uses_isolated_port_and_checks_returned_endpoint(self):
        body = source_function(
            self.rust,
            "async fn start_hardware_candidate_backend",
            "\n#[tauri::command]",
        )
        for token in (
            "native_benchmark_ephemeral_loopback_port()",
            "native_benchmark_random_five_digit_loopback_port()",
            "candidate_port",
            "Some(device)",
            "status.port != Some(candidate_port)",
        ):
            with self.subTest(token=token):
                self.assertIn(token, body)

    def test_normal_backend_start_requires_selected_tls_trust_and_keeps_port(self):
        body = source_function(
            self.rust,
            "fn start_backend_blocking_with_options_once",
            "\nfn ",
        )
        for token in (
            "load_local_tls_config",
            "validate_tls_material(&tls_config)",
            "configured_certificate_trusted(&tls_config)",
            "No HTTP fallback was attempted",
            "let port = preferred_port",
        ):
            with self.subTest(token=token):
                self.assertIn(token, body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
