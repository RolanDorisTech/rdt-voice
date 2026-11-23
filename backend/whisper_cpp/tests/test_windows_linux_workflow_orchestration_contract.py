import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"


class WindowsLinuxWorkflowOrchestrationContract(unittest.TestCase):
    def test_native_workflows_are_reusable_and_keep_existing_triggers(self):
        names = (
            "linux-native-qualification.yml",
            "windows-x64-installer.yml",
            "windows-arm64-installer.yml",
        )
        for name in names:
            text = (WORKFLOWS / name).read_text(encoding="utf-8")
            self.assertRegex(text, r"(?m)^  workflow_call\s*:")
            self.assertRegex(text, r"(?m)^  workflow_dispatch\s*:")
            self.assertRegex(text, r"(?m)^  push\s*:")

    def test_orchestrator_calls_all_three_native_workflows(self):
        text = (WORKFLOWS / "windows-linux-native-qualification.yml").read_text(
            encoding="utf-8"
        )
        for name in (
            "linux-native-qualification.yml",
            "windows-x64-installer.yml",
            "windows-arm64-installer.yml",
        ):
            self.assertIn(f"uses: ./.github/workflows/{name}", text)
        self.assertEqual(text.count("secrets: inherit"), 3)

    def test_orchestrator_grants_minimum_permissions_required_by_children(self):
        parent = (WORKFLOWS / "windows-linux-native-qualification.yml").read_text(
            encoding="utf-8"
        )

        def assert_contents_read(workflow_text):
            lines = workflow_text.splitlines()
            permission_blocks = [
                lines[index:index + 2]
                for index, line in enumerate(lines[:-1])
                if line == "permissions:"
            ]
            self.assertIn(["permissions:", "  contents: read"], permission_blocks)

        assert_contents_read(parent)
        self.assertNotIn("  actions: read\n", parent.split("jobs:", 1)[0])

        required_job = parent.split("  required-targets:\n", 1)[1]
        self.assertIn(
            "\n    permissions:\n"
            "      actions: read\n"
            "      contents: read\n",
            required_job,
        )
        for name in (
            "linux-native-qualification.yml",
            "windows-x64-installer.yml",
            "windows-arm64-installer.yml",
        ):
            child = (WORKFLOWS / name).read_text(encoding="utf-8")
            assert_contents_read(child)

    def test_final_gate_requires_all_three_workflow_results(self):
        text = (WORKFLOWS / "windows-linux-native-qualification.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("if: ${{ always() }}", text)
        self.assertIn("needs.linux-native.result", text)
        self.assertIn("needs.windows-x64.result", text)
        self.assertIn("needs.windows-arm64.result", text)
        self.assertIn('test "$LINUX_RESULT" = success', text)
        self.assertIn('test "$WINDOWS_X64_RESULT" = success', text)
        self.assertIn('test "$WINDOWS_ARM64_RESULT" = success', text)

    def test_native_runner_architectures_and_required_gpu_paths_remain(self):
        linux = (WORKFLOWS / "linux-native-qualification.yml").read_text(
            encoding="utf-8"
        )
        win_x64 = (WORKFLOWS / "windows-x64-installer.yml").read_text(
            encoding="utf-8"
        )
        win_arm = (WORKFLOWS / "windows-arm64-installer.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("ubuntu-24.04", linux)
        self.assertIn("ubuntu-24.04-arm", linux)
        self.assertIn("RDT_REQUIRE_CUDA=ON", linux)
        self.assertIn("RDT_REQUIRE_VULKAN=ON", linux)
        self.assertIn("windows-latest", win_x64)
        self.assertIn("windows-11-arm", win_arm)
        self.assertIn("-RequireGpuWorker", win_x64)
        self.assertIn("-RequireGpuWorker", win_arm)
        self.assertIn("rdt_whisper_worker_dotprod", linux)
        # The workflow delegates ARM64 worker construction to the bundle script.
        # Protect the full delegated contract rather than expecting the worker
        # executable name to appear literally in the workflow YAML.
        bundle_script = (
            Path(__file__).resolve().parents[3]
            / "rdt_connect"
            / "scripts"
            / "prepare-windows-bundle.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("-TargetArch ARM64", win_arm)
        self.assertIn("Build-WorkerVariant -Variant 'arm-dotprod'", bundle_script)
        self.assertIn("-ArmDotprod 'ON'", bundle_script)
        self.assertIn("Name = 'rdt_whisper_worker_dotprod.exe'", bundle_script)
        self.assertIn("Required ARM64 dot-product worker is missing.", bundle_script)


if __name__ == "__main__":
    unittest.main()
