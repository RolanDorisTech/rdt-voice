"""Cross-check the Linux Tauri catalog against the actual GGML download registry.

These are source-contract guards. The authoritative behavioral checks for full-byte
hashing, path validation and deletion are the Rust unit tests in linux_ggml_models.rs.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

import model_registry

ROOT = Path(__file__).resolve().parents[3]
RUST_MODELS = ROOT / "rdt_connect" / "src-tauri" / "src" / "linux_ggml_models.rs"
RUST_MAIN = ROOT / "rdt_connect" / "src-tauri" / "src" / "main.rs"
RUST_PATHS = ROOT / "rdt_connect" / "src-tauri" / "src" / "linux_paths.rs"
TS_MAIN = ROOT / "rdt_connect" / "src" / "main.ts"


def desktop_catalog() -> dict[str, dict[str, str]]:
    source = RUST_MODELS.read_text(encoding="utf-8")
    pattern = re.compile(
        r'ModelSpec\s*\{\s*id:\s*"([^\"]+)"\s*,\s*name:\s*"([^\"]+)"\s*,'
        r'\s*filename:\s*"([^\"]+)"\s*,\s*quantization:\s*"([^\"]+)"\s*,'
        r'\s*sha256:\s*"([0-9a-f]{64})"',
        re.MULTILINE,
    )
    return {
        match.group(1): {
            "name": match.group(2),
            "file": match.group(3),
            "quantization": match.group(4),
            "sha256": match.group(5),
        }
        for match in pattern.finditer(source)
    }


class LinuxDesktopCatalogContractTests(unittest.TestCase):
    def test_rust_catalog_matches_every_approved_registry_identity(self) -> None:
        rust = desktop_catalog()
        self.assertEqual(set(rust), set(model_registry.MODELS))
        for model_id, registry in model_registry.MODELS.items():
            with self.subTest(model=model_id):
                self.assertEqual(rust[model_id]["file"], registry["file"])
                self.assertEqual(rust[model_id]["quantization"], registry["quantization"])
                self.assertEqual(rust[model_id]["sha256"], registry["sha256"])
        self.assertIn(model_registry.REVISION, RUST_MODELS.read_text(encoding="utf-8"))

    def test_readiness_requires_full_file_hash_and_exact_sidecar_identity(self) -> None:
        source = RUST_MODELS.read_text(encoding="utf-8")
        for field in ("schema_version", "model_id", "repo", "revision", "url", "filename", "quantization", "sha256", "size_bytes"):
            with self.subTest(field=field):
                self.assertIn(f'get("{field}")', source)
        self.assertIn("super::sha256_file_hex(&canonical_artifact)", source)
        self.assertIn("actual_sha != expected_sha", source)
        self.assertIn("pub(crate) fn model_cache_has_weights", source)
        self.assertIn("verify_model(root, model_id).is_ok()", source)

    def test_verification_cache_is_identity_bound_and_explicitly_invalidated(self) -> None:
        source = RUST_MODELS.read_text(encoding="utf-8")
        self.assertIn("struct VerificationKey", source)
        self.assertIn("canonical_path: PathBuf", source)
        self.assertIn("device: u64", source)
        self.assertIn("inode: u64", source)
        self.assertIn("changed_nanoseconds", source)
        self.assertIn("sidecar: FileIdentity", source)
        self.assertIn("pub(crate) fn invalidate_model_cache", source)
        self.assertIn("unchanged_verified_model_reuses_full_hash_result", source)
        self.assertIn("verification_cache_invalidates_explicitly_and_on_replacement", source)

    def test_xdg_destinations_have_symlink_and_type_guard_tests(self) -> None:
        source = RUST_PATHS.read_text(encoding="utf-8")
        self.assertIn("pub(crate) fn validate_directory_target", source)
        self.assertIn("existing_symlinked_xdg_application_data_destination_is_rejected", source)
        self.assertIn("symlinked_xdg_state_root_is_rejected_before_log_creation", source)
        self.assertIn('validate_directory_target(&selected, "application-data")?', source)
        self.assertIn('validate_directory_target(&selected, "application-log")?', source)

    @unittest.skipUnless(TS_MAIN.is_file(), "Tauri frontend sources are absent from this backend-only test archive")
    def test_frontend_deletion_copy_is_platform_neutral(self) -> None:
        source = TS_MAIN.read_text(encoding="utf-8")
        self.assertNotIn("RDT Connect MLX model caches", source)
        self.assertNotIn("RDT Connect MLX Whisper cache entries", source)
        self.assertIn("Permanently delete only these selected RDT Connect Whisper model caches?", source)

    @unittest.skipUnless(RUST_MAIN.is_file(), "Tauri desktop sources are absent from this backend-only test archive")
    def test_download_and_inference_export_the_same_model_root(self) -> None:
        source = RUST_MAIN.read_text(encoding="utf-8")
        self.assertIn('.env("RDT_GGML_MODEL_DIR", &root)', source)
        self.assertIn('.env("RDT_GGML_MODEL_DIR", &model_storage_root)', source)
        self.assertIn("invalidate_model_cache(&root, &model)", source)
        self.assertIn('"large-v3-turbo"', source)
        self.assertIn('fn get_platform_model_catalog()', source)

    def test_linux_path_selection_keeps_legacy_https_state_without_migration(self) -> None:
        source = RUST_PATHS.read_text(encoding="utf-8")
        self.assertRegex(source, r'home\s*\.join\("Library"\)\s*\.join\("Application Support"\)\s*\.join\(app_name\)')
        self.assertIn('home.join("Library").join("Logs").join(app_name)', source)
        self.assertIn('"XDG_DATA_HOME"', source)
        self.assertIn('"XDG_STATE_HOME"', source)
        self.assertIn("legacy_directory_exists", source)


if __name__ == "__main__":
    unittest.main()
