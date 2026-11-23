from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github/workflows/windows-public-release.yml"


class WindowsPublicReleaseWorkflowContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = WORKFLOW.read_text(encoding="utf-8")

    def test_draft_verification_binds_marker_and_tag_to_qualified_commit(self):
        block = self.source.split(
            "      - name: Verify all four drafts before publishing any", 1
        )[1].split(
            "      - name: Publish all four releases only when explicitly requested", 1
        )[0]
        self.assertIn('verify_release_identity "$tag"', block)
        self.assertIn(".draft == true", block)
        self.assertIn(".prerelease == true", block)
        self.assertIn("git/ref/tags/$tag", block)
        self.assertIn("git/tags/$object_sha", block)
        self.assertIn(
            '[[ "$object_type" == "commit" && "$object_sha" == "$SOURCE_SHA" ]]',
            block,
        )
        self.assertIn(
            "RDT-PROMOTION:${QUALIFICATION_RUN_ID}:${QUALIFICATION_RUN_ATTEMPT}:${SOURCE_SHA}",
            block,
        )

    def test_publication_preflight_and_rollback_are_ownership_guarded(self):
        block = self.source.split(
            "      - name: Publish all four releases only when explicitly requested", 1
        )[1].split(
            "      - name: Leave verified drafts unpublished", 1
        )[0]
        self.assertIn('verify_release_identity "$tag"', block)
        self.assertIn('verify_release_identity "$rollback_tag"', block)
        self.assertIn("refusing rollback", block)
        self.assertIn("SOURCE_SHA: ${{ steps.source.outputs.sha }}", block)

    def test_failed_draft_cleanup_proves_exact_release_and_git_tag_ownership(self):
        block = self.source.split(
            "      - name: Create four complete draft releases without rebuilding", 1
        )[1].split(
            "      - name: Verify all four drafts before publishing any", 1
        )[0]
        self.assertIn('tag_commit_sha "$created_tag"', block)
        self.assertIn(".tag_name == $tag", block)
        self.assertIn(".draft == true", block)
        self.assertIn(".prerelease == true", block)
        self.assertIn("contains($marker)", block)
        self.assertIn("git/ref/tags/$tag", block)
        self.assertIn("git/tags/$object_sha", block)
        self.assertNotIn(".target_commitish == $sha", block)

    def test_default_token_is_read_only_and_promotions_are_serialized(self):
        header = self.source.split("jobs:", 1)[0]
        self.assertRegex(
            header,
            r"permissions:\s+actions: read\s+contents: read",
        )
        self.assertNotIn("contents: write", header)
        self.assertIn(
            "group: rdt-connect-release-promotion-${{ inputs.expected_base_version }}",
            header,
        )
        self.assertIn("cancel-in-progress: false", header)

    def test_release_mutations_use_dedicated_token(self):
        self.assertIn('GH_TOKEN="$RELEASE_TOKEN" gh release create', self.source)
        self.assertIn('GH_TOKEN="$RELEASE_TOKEN" gh release delete', self.source)
        self.assertIn(
            'GH_TOKEN="$RELEASE_TOKEN" gh api --method PATCH',
            self.source,
        )

    def test_linux_artifact_layout_is_normalized_before_manifest_verification(self):
        block = self.source.split(
            "      - name: Download exact artifacts from the qualification run", 1
        )[1].split(
            "      - name: Verify all manifests, hashes, evidence and Tauri signatures", 1
        )[0]
        self.assertIn("normalize_linux_artifact()", block)
        self.assertIn('find "$base" -type d -name package -print0', block)
        self.assertIn('find "$base" -type f -name release-manifest.json -print0', block)
        self.assertIn('normalize_linux_artifact "$root/linux-x64"', block)
        self.assertIn('normalize_linux_artifact "$root/linux-ARM64"', block)
        self.assertIn("expected exactly one Linux package directory", block)
        self.assertIn("expected exactly one Linux release manifest", block)

    def test_linux_qualification_artifacts_match_promotion_contract(self):
        root = WORKFLOW.parents[2]
        linux = (root / ".github/workflows/linux-native-qualification.yml").read_text(
            encoding="utf-8"
        )
        promotion = self.source
        generator = (
            root / "rdt_connect/scripts/release_artifact_manifest.py"
        ).read_text(encoding="utf-8")

        self.assertIn(
            'artifact_name="rdt-connect-linux-${BUILD_ARCH}-qualification-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"',
            linux,
        )
        self.assertIn('--artifact-name "$artifact_name"', linux)
        self.assertIn('--qualification-artifact-name "$artifact_name"', linux)
        self.assertIn(
            '--output "$RUNNER_TEMP/rdt-linux-qualification/release-manifest.json"',
            linux,
        )
        self.assertIn(
            'find "$RUNNER_TEMP/rdt-linux-qualification/package" -maxdepth 1 -type f',
            linux,
        )
        self.assertIn(
            'name: rdt-connect-linux-${{ matrix.arch }}-qualification-${{ github.run_id }}-${{ github.run_attempt }}',
            linux,
        )

        self.assertIn(
            'appimages = [name for name in names if name.endswith(".AppImage")]',
            generator,
        )
        self.assertIn(
            'debs = [name for name in names if name.endswith(".deb")]',
            generator,
        )
        self.assertIn('appimage + ".sig"', generator)
        self.assertIn('appimage + ".sha256"', generator)
        self.assertIn('appimage + ".sig.sha256"', generator)

        self.assertIn(
            'download_one "rdt-connect-linux-x64-qualification-$run-$attempt"',
            promotion,
        )
        self.assertIn(
            'download_one "rdt-connect-linux-ARM64-qualification-$run-$attempt"',
            promotion,
        )
        self.assertIn('normalize_linux_artifact "$root/linux-x64"', promotion)
        self.assertIn('normalize_linux_artifact "$root/linux-ARM64"', promotion)
        self.assertIn(
            'verify_draft "$LINUX_X64_TAG" "$root/linux-x64/package" 6',
            promotion,
        )
        self.assertIn(
            'verify_draft "$LINUX_ARM64_TAG" "$root/linux-ARM64/package" 6',
            promotion,
        )

    def test_four_target_orchestrator_requires_all_targets_and_twelve_artifacts(self):
        root = WORKFLOW.parents[2]
        orchestrator = (
            root / ".github/workflows/windows-linux-native-qualification.yml"
        ).read_text(encoding="utf-8")

        gate = orchestrator.split("  required-targets:", 1)[1]
        dependencies = gate.split("    runs-on:", 1)[0]
        for target in ("linux-native", "windows-x64", "windows-arm64"):
            self.assertIn("- " + target, dependencies)

        self.assertIn("if: ${{ always() }}", gate)
        result_step = gate.split(
            "      - name: Report native platform results and fail closed", 1
        )[1].split(
            "      - name: Verify all required qualification artifacts exist", 1
        )[0]
        for check in (
            'test "$LINUX_RESULT" = success',
            'test "$WINDOWS_X64_RESULT" = success',
            'test "$WINDOWS_ARM64_RESULT" = success',
        ):
            self.assertIn(check, result_step)

        artifact_step = gate.split(
            "      - name: Verify all required qualification artifacts exist", 1
        )[1]
        self.assertIn("gh api --paginate", artifact_step)
        self.assertIn(
            '"repos/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID/artifacts?per_page=100"',
            artifact_step,
        )
        self.assertIn("select(.expired == false)", artifact_step)
        self.assertIn("if ! grep -Fqx --", artifact_step)
        self.assertIn("GH_TOKEN: ${{ github.token }}", gate)
        self.assertIn("echo 'PASS: all four target artifact groups are present in this workflow run'", artifact_step)

        for group in (
            "require_group 'Windows x64'",
            "require_group 'Windows ARM64'",
            "require_group 'Linux x64'",
            "require_group 'Linux ARM64'",
        ):
            self.assertIn(group, artifact_step)

        gate_permissions = gate.split("    permissions:", 1)[1].split(
            "    timeout-minutes:", 1
        )[0]
        self.assertIn("actions: read", gate_permissions)
        self.assertIn("contents: read", gate_permissions)
        for forbidden in ("actions: write", "contents: write", "packages: write"):
            self.assertNotIn(forbidden, gate_permissions)

        expected_artifacts = (
            "rdt-connect-windows-x64-release-manifest-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT",
            "RDT-Connect-Windows-x64-DISTRIBUTION-${GITHUB_RUN_ATTEMPT}",
            "RDT-Connect-Windows-x64-INSTALLATION-GUIDE-${GITHUB_RUN_ATTEMPT}",
            "RDT-Connect-Windows-x64-TEST-INSTALLER-${GITHUB_RUN_ATTEMPT}",
            "rdt-windows-x64-installed-qualification-${GITHUB_RUN_ATTEMPT}",
            "rdt-connect-windows-arm64-release-manifest-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT",
            "RDT-Connect-Windows-ARM64-DISTRIBUTION-${GITHUB_RUN_ATTEMPT}",
            "RDT-Connect-Windows-ARM64-INSTALLATION-GUIDE-${GITHUB_RUN_ATTEMPT}",
            "RDT-Connect-Windows-ARM64-TEST-INSTALLER-${GITHUB_RUN_ATTEMPT}",
            "rdt-windows-arm64-installed-qualification-${GITHUB_RUN_ATTEMPT}",
            "rdt-connect-linux-x64-qualification-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT",
            "rdt-connect-linux-ARM64-qualification-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT",
        )
        for artifact in expected_artifacts:
            self.assertIn(artifact, artifact_step, artifact)
        self.assertEqual(len(expected_artifacts), 12)

        groups = [
            line for line in artifact_step.splitlines()
            if line.strip().startswith("require_group ")
        ]
        self.assertEqual(len(groups), 4)

    def test_release_publication_remains_explicit_opt_in(self):
        dispatch = self.source.split("      publish_release:", 1)[1].split(
            "permissions:", 1
        )[0]
        self.assertIn("default: false", dispatch)
        self.assertIn("if: ${{ inputs.publish_release }}", self.source)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class WorkflowReliabilityPolicyContract(unittest.TestCase):
    """Prevent runner jobs and uploaded artifacts from losing CI safeguards."""

    @classmethod
    def setUpClass(cls):
        import re
        from pathlib import Path

        cls.re = re
        cls.root = Path(__file__).resolve().parents[3]
        workflow_dir = cls.root / ".github" / "workflows"
        cls.workflows = sorted(
            list(workflow_dir.glob("*.yml"))
            + list(workflow_dir.glob("*.yaml"))
        )
        if not cls.workflows:
            raise AssertionError("No GitHub Actions workflow files were found.")

    def test_every_runner_job_has_an_explicit_timeout(self):
        import re

        failures = []
        job_header = re.compile(r"^  ([A-Za-z0-9_.-]+):\\s*(?:#.*)?$")

        for path in self.workflows:
            lines = path.read_text(encoding="utf-8").splitlines()
            try:
                jobs_index = lines.index("jobs:")
            except ValueError:
                continue

            headers = [
                (i, match.group(1))
                for i, line in enumerate(lines[jobs_index + 1:], jobs_index + 1)
                if (match := job_header.match(line))
            ]

            for n, (job_start, job_id) in enumerate(headers):
                job_end = headers[n + 1][0] if n + 1 < len(headers) else len(lines)
                block = "\\n".join(lines[job_start:job_end])

                if not re.search(r"(?m)^    runs-on\\s*:", block):
                    continue

                values = re.findall(
                    r"(?m)^    timeout-minutes:\\s*(.*?)\\s*(?:#.*)?$",
                    block,
                )
                valid = False
                if len(values) == 1:
                    value = values[0].strip()
                    if value.isdigit():
                        valid = int(value) >= 1
                    elif value.startswith("${{") and value.endswith("}}"):
                        limits = [
                            int(number)
                            for number in re.findall(r"\\b[0-9]+\\b", value)
                        ]
                        valid = bool(limits) and all(number >= 1 for number in limits)

                if not valid:
                    failures.append(f"{path}:{job_id}")

        self.assertFalse(
            failures,
            "Runner jobs need one explicit positive timeout (literal or "
            "GitHub Actions expression):\\n" + "\\n".join(failures),
        )

    def test_every_uploaded_artifact_has_explicit_retention(self):
        failures = []
        for path in self.workflows:
            lines = path.read_text(encoding="utf-8").splitlines()
            for i, line in enumerate(lines):
                match = self.re.match(
                    r"^( *)(?:-\s*)?uses:\s*actions/upload-artifact@",
                    line,
                )
                if not match:
                    continue

                leading = len(match.group(1))
                inline_dash = bool(self.re.match(r"^ *-\s*uses:", line))
                mapping_indent = leading + (2 if inline_dash else 0)
                step_indent = mapping_indent - 2
                end = len(lines)

                for j in range(i + 1, len(lines)):
                    candidate = lines[j]
                    if not candidate.strip():
                        continue
                    indent = len(candidate) - len(candidate.lstrip(" "))
                    if indent < step_indent:
                        end = j
                        break
                    if indent == step_indent and candidate.lstrip().startswith("- "):
                        end = j
                        break

                block = "\n".join(lines[i:end])
                retention = self.re.search(
                    r"(?m)^\s+retention-days:\s*([0-9]+)\s*$",
                    block,
                )
                if not retention:
                    failures.append(f"{path}:line {i + 1}")
                elif not 1 <= int(retention.group(1)) <= 90:
                    failures.append(
                        f"{path}:line {i + 1}: retention outside 1..90 days"
                    )

        self.assertFalse(
            failures,
            "Artifact uploads need explicit retention between 1 and 90 days:\n"
            + "\n".join(failures),
        )
