from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[3]


class PlatformValidationWorkflowContractTests(unittest.TestCase):
    def test_local_validation_environment_is_keyed_to_requirements(self):
        script = (ROOT / ".github/scripts/validate-platform-contracts.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("backend/linux/requirements-linux.txt", script)
        self.assertIn("sha256sum backend/linux/requirements-linux.txt", script)
        self.assertIn("shasum -a 256 backend/linux/requirements-linux.txt", script)
        self.assertIn('VENV="$CACHE/linux-py${PY312_VERSION}-${REQ_HASH}"', script)
        self.assertIn('.requirements-${REQ_HASH}', script)
        self.assertIn('"$VENV/bin/python" -m pip install', script)

    def test_all_python_suites_use_the_same_cached_environment(self):
        script = (ROOT / ".github/scripts/validate-platform-contracts.sh").read_text(
            encoding="utf-8"
        )
        for suite in (
            "backend/whisper_cpp/tests",
            "backend/linux/tests",
            "backend/windows/tests",
        ):
            self.assertIn(f'"$VENV/bin/python" -m unittest discover -s {suite}', script)
        self.assertIn('exec "$1" -m unittest discover -s scripts', script)
        self.assertIn('"$VENV/bin/python"', script)

    def test_github_workflow_runs_saved_validation_on_source_changes(self):
        workflow = (ROOT / ".github/workflows/platform-contracts.yml").read_text(
            encoding="utf-8"
        )
        for trigger in ("push:", "pull_request:", "workflow_dispatch:"):
            self.assertIn(trigger, workflow)
        for path in (
            ".github/workflows/**",
            ".github/actions/**",
            ".github/dependabot.yml",
            "backend/**",
            "rdt_connect/**",
        ):
            self.assertIn(path, workflow)
        for action in (
            "actions/checkout@v7.0.1",
            "actions/setup-python@v7.1.0",
            "actions/setup-node@v7.1.0",
        ):
            self.assertIn(action, workflow)
        self.assertIn("cache: pip", workflow)
        self.assertIn(
            "cache-dependency-path: backend/linux/requirements-linux.txt",
            workflow,
        )
        self.assertIn("timeout-minutes: 60", workflow)
        self.assertIn("contents: read", workflow)
        self.assertIn(".github/scripts/validate-platform-contracts.sh", workflow)
        self.assertIn("ffmpeg", workflow)


    def test_hosted_ci_installs_and_requires_pinned_workflow_tools(self):
        workflow = (ROOT / ".github/workflows/platform-contracts.yml").read_text(
            encoding="utf-8"
        )
        script = (ROOT / ".github/scripts/validate-platform-contracts.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("actionlint_1.7.12_linux_amd64.tar.gz", workflow)
        self.assertIn(
            "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8",
            workflow,
        )
        self.assertIn("sha256sum --check --status", workflow)
        self.assertIn(
            "gh extension install github/gh-actions-lock --pin v0.5.1",
            workflow,
        )
        self.assertIn('RDT_REQUIRE_ACTIONLINT: "1"', workflow)
        self.assertIn('RDT_REQUIRE_ACTIONS_LOCK: "1"', workflow)
        self.assertIn('RDT_REQUIRE_ACTIONLINT:-0', script)
        self.assertIn('RDT_REQUIRE_ACTIONS_LOCK:-0', script)
        self.assertIn("CI requires actionlint", script)
        self.assertIn("CI requires gh-actions-lock", script)

    def test_native_orchestrator_requires_platform_contracts_first(self):
        workflow = (ROOT / ".github/workflows/platform-contracts.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("on:", workflow.splitlines())
        self.assertIn("  workflow_call:", workflow.splitlines())

        orchestrator = (
            ROOT / ".github/workflows/windows-linux-native-qualification.yml"
        ).read_text(encoding="utf-8")
        lines = orchestrator.splitlines()

        def block(name):
            start = lines.index(f"  {name}:")
            end = next(
                (
                    i for i in range(start + 1, len(lines))
                    if lines[i].startswith("  ")
                    and not lines[i].startswith("   ")
                    and lines[i].endswith(":")
                ),
                len(lines),
            )
            return lines[start:end]

        self.assertIn(
            "    uses: ./.github/workflows/platform-contracts.yml",
            block("platform-contracts"),
        )
        for name in ("linux-native", "windows-x64", "windows-arm64"):
            self.assertIn("    needs: platform-contracts", block(name))

        gate = block("required-targets")
        self.assertIn("      - platform-contracts", gate)
        self.assertIn(
            "          CONTRACTS_RESULT: ${{ needs.platform-contracts.result }}",
            gate,
        )
        self.assertIn('          test "$CONTRACTS_RESULT" = success', gate)


if __name__ == "__main__":
    unittest.main()
