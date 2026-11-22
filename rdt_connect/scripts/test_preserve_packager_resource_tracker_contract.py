#!/usr/bin/env python3
"""Contract: both macOS packagers must install the Connect resource-tracker hook."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOKEN = '--runtime-hook "$SCRIPT_DIR/pyi_rth_rdt_resource_tracker.py"'

class ResourceTrackerPackagingContract(unittest.TestCase):
    def test_both_packagers_include_exactly_one_hook(self):
        for name in (
            "prepare-macos-bundle.sh",
            "prepare-macos-bundle-preserve.sh",
        ):
            with self.subTest(packager=name):
                source = (ROOT / "scripts" / name).read_text()
                self.assertEqual(source.count(TOKEN), 1)
                self.assertIn('"$PY" -m PyInstaller "${ARGS[@]}"', source)

    def test_hook_is_scoped_to_frozen_backend_and_tracker_helpers(self):
        source = (ROOT / "scripts" / "pyi_rth_rdt_resource_tracker.py").read_text()
        self.assertIn('if getattr(_sys, "frozen", False):', source)
        self.assertIn("from multiprocessing.resource_tracker import main", source)
        self.assertIn("raise SystemExit(0)", source)
        self.assertIn("_rdt_tracker.ensure_running = _rdt_disable_resource_tracking", source)


    def test_both_packagers_normalize_mlx_shader_aliases(self):
        root = __import__("pathlib").Path(__file__).resolve().parents[1]
        standard = (root / "scripts/prepare-macos-bundle.sh").read_text(
            encoding="utf-8"
        )
        preserve = (root / "scripts/prepare-macos-bundle-preserve.sh").read_text(
            encoding="utf-8"
        )
        invocation = (
            'python3 "$SCRIPT_DIR/dedupe-macos-mlx-metallib.py" '
            '--internal-dir "$MLX_INTERNAL"'
        )

        for name, contents in (("standard", standard), ("preserve", preserve)):
            self.assertEqual(contents.count(invocation), 1, name)
            self.assertEqual(contents.count("if ! " + invocation), 1, name)

        alias_setup = (
            'ln -sfn "mlx/mlx.metallib" "$MLX_INTERNAL/mlx.metallib"'
        )
        self.assertLess(
            preserve.index(alias_setup),
            preserve.index(invocation),
            "preserve mode must create its top-level lookup alias before normalization",
        )

        normalizer = (
            root / "scripts/dedupe-macos-mlx-metallib.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'Path("mlx/lib/mlx.metallib"): "../mlx.metallib"',
            normalizer,
        )
        self.assertIn(
            'Path("mlx.metallib"): "mlx/mlx.metallib"',
            normalizer,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
