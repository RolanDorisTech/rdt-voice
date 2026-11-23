import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


class LinuxTlsTauriResourceContractTests(unittest.TestCase):
    def test_linux_override_maps_the_notice_directory(self):
        config = json.loads(
            (ROOT / "rdt_connect/src-tauri/tauri.linux.conf.json").read_text(
                encoding="utf-8"
            )
        )
        resources = config["bundle"]["resources"]
        self.assertEqual(
            resources["resources/linux-third-party-notices"],
            "third_party_notices",
        )
        self.assertIsNone(resources["resources/third_party_notices"])

    def test_rust_test_step_creates_and_cleans_both_temporary_resources(self):
        workflow = (
            ROOT / ".github/workflows/linux-native-qualification.yml"
        ).read_text(encoding="utf-8")
        start = workflow.index(
            "      - name: Run shared Rust Linux TLS tests"
        )
        end = workflow.index(
            "      - name: Fetch and validate pinned whisper.cpp source",
            start,
        )
        step = workflow[start:end]

        self.assertIn(
            'TEMP_LINUX_NOTICES="$RESOURCE_ROOT/linux-third-party-notices"',
            step,
        )
        self.assertIn(
            'trap \'rm -rf -- "$TEMP_BACKEND" "$TEMP_LINUX_NOTICES"\' EXIT',
            step,
        )
        self.assertIn(
            'mkdir -p "$TEMP_BACKEND" "$TEMP_LINUX_NOTICES"',
            step,
        )
        self.assertIn(
            '> "$TEMP_LINUX_NOTICES/.qualification-placeholder"',
            step,
        )
        self.assertIn(
            "python3 backend/linux/tests/test_linux_tls_resource_contract.py -v",
            step,
        )


if __name__ == "__main__":
    unittest.main()
