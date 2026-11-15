from pathlib import Path
import json
import unittest
import importlib.util
import tempfile

ROOT = Path(__file__).resolve().parents[1]


class MacOSBuildPipelineContractTests(unittest.TestCase):
    def test_release_pipeline_uses_custom_dmg_after_shader_deduplication(self):
        script = (ROOT / "scripts/build-macos-with-marker.sh").read_text(encoding="utf-8")
        builder = (ROOT / "scripts/build-custom-dmg.py").read_text(encoding="utf-8")
        app_build = script.index('npm run tauri -- build --bundles app')
        dedupe = script.index('dedupe-macos-mlx-metallib.py" "$APP_BUNDLE"', app_build)
        verify_app = script.index('"$APP_BUNDLE" --verify-only', dedupe)
        app_only_exit = script.index('if [[ "$APP_ONLY" == "1" ]]')
        custom_bundle = script.index('python3 "$ROOT/scripts/build-custom-dmg.py"')
        self.assertLess(app_build, dedupe)
        self.assertLess(dedupe, verify_app)
        self.assertLess(verify_app, custom_bundle)
        self.assertLess(app_only_exit, custom_bundle)
        self.assertNotIn('bundle --bundles dmg', script)
        self.assertIn(
            'Path("mlx.metallib"): "mlx/mlx.metallib"',
            (ROOT / "scripts/dedupe-macos-mlx-metallib.py").read_text(encoding="utf-8"),
        )
        self.assertIn(
            'Path("mlx/lib/mlx.metallib"): "../mlx.metallib"',
            (ROOT / "scripts/dedupe-macos-mlx-metallib.py").read_text(encoding="utf-8"),
        )
        self.assertIn('dedupe-macos-mlx-metallib.py', builder)
        self.assertIn('"--verify-only"', builder)
        self.assertIn('"--max-app-mib"', builder)

    def test_candidate_build_outputs_are_redirectable(self):
        script = (ROOT / "scripts/build-macos-with-marker.sh").read_text(encoding="utf-8")
        prepare = (ROOT / "scripts/prepare-macos-bundle.sh").read_text(encoding="utf-8")
        self.assertIn('RDT_CARGO_TARGET_DIR:-$ROOT/build/rdt-connect-${MARKER}-cargo-target', script)
        self.assertIn('refusing to use the protected historical src-tauri/target directory', script)
        self.assertIn('export CARGO_TARGET_DIR RDT_MACOS_BUILD_STAGING_ROOT', script)
        self.assertIn('RDT_MACOS_BUILD_STAGING_ROOT:-$PROJECT_DIR/build', prepare)
        self.assertIn('RESOURCES="$PROJECT_DIR/src-tauri/resources"', prepare)

    def test_custom_visual_inspection_build_skips_updater_signing(self):
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        command = package["scripts"]["build:desktop:custom-dmg:macos"]
        self.assertIn('RDT_SKIP_UPDATER_ARTIFACTS=1', command)
        self.assertIn('RDT_BUILD_APP_ONLY=1', command)

    def test_candidate_copy_preserves_source_and_excludes_existing_build_artifacts(self):
        script_path = ROOT / "scripts/create-macos-candidate-copy.py"
        spec = importlib.util.spec_from_file_location("candidate_copy", script_path)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            destination = base / "candidate"
            (source / "rdt_connect").mkdir(parents=True)
            (source / "backend/apple").mkdir(parents=True)
            (source / "rdt_connect/package.json").write_text("{}")
            (source / "backend/apple/server.py").write_text("# active backend")
            (source / "rdt_connect/build/old-target").mkdir(parents=True)
            (source / "rdt_connect/build/old-target/output").write_text("preserve outside")
            (source / "rdt_connect/node_modules").mkdir(parents=True)
            (source / "rdt_connect/node_modules/typescript").mkdir()
            module.create_candidate(source, destination)
            self.assertTrue((destination / "backend/apple/server.py").is_file())
            self.assertFalse((destination / "rdt_connect/build").exists())
            self.assertTrue((destination / "rdt_connect/node_modules").is_symlink())
            self.assertTrue((source / "rdt_connect/build/old-target/output").is_file())
            with self.assertRaises(RuntimeError):
                module.create_candidate(source, destination)


if __name__ == "__main__":
    unittest.main(verbosity=2)
