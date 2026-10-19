#!/usr/bin/env python3
"""Offline, no-download tests for the pinned MLX model cache and native lifecycle contract.

This loads only the small validator helpers from backend/apple/server.py via AST. It does not
import FastAPI/MLX, contact Hugging Face, or download real model files.
Run from the repository root:
    python3 rdt_connect/scripts/test_model_download_reliability.py
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import pathlib
import re
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVER_PY = ROOT / "backend" / "apple" / "server.py"
RUST_RS = ROOT / "rdt_connect" / "src-tauri" / "src" / "main.rs"
MODEL = "mlx-community/whisper-tiny-mlx"
REVISION = "a" * 40
WEIGHTS = b"locked test MLX weights bytes"
WEIGHT_SHA = hashlib.sha256(WEIGHTS).hexdigest()
LOCK = {
    "schema_version": 1,
    "mlx_whisper_version": "0.4.3",
    "official_endpoint": "https://huggingface.co",
    "models": {
        MODEL: {
            "revision": REVISION,
            "weight_file": "weights.npz",
            "sha256": WEIGHT_SHA,
            "size_estimate_bytes": len(WEIGHTS),
        }
    },
}


def load_python_validator():
    source = SERVER_PY.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(SERVER_PY))
    names = {
        "_rdt_load_model_lock",
        "_rdt_model_spec",
        "_rdt_marker_path",
        "_rdt_marker_is_current",
        "_rdt_write_verified_marker",
        "_rdt_model_snapshot_is_complete",
    }
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    found = {n.name for n in nodes}
    if found != names:
        raise AssertionError(f"Validator dependency functions missing: {sorted(names - found)}")
    ns = {
        "os": os,
        "sys": sys,
        "json": json,
        "re": re,
        "uuid": uuid,
        "hashlib": hashlib,
        "pathlib": pathlib,
        "_RDT_MODEL_LOCK_CACHE": LOCK,
        "_RDT_MODEL_LOCK_PATH": None,
        "__file__": str(SERVER_PY),
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SERVER_PY), "exec"), ns)
    # Ensure fixture spec is used even though the production lock is on disk.
    ns["_RDT_MODEL_LOCK_CACHE"] = LOCK
    return ns["_rdt_model_snapshot_is_complete"]


validate_snapshot = load_python_validator()
REPO_KEY = "models--mlx-community--whisper-tiny-mlx"


class CacheFixture:
    def __init__(self, temp: tempfile.TemporaryDirectory[str]):
        self.base = Path(temp.name)
        self.cache = self.base / "hub"
        self.repo = self.cache / REPO_KEY
        self.snapshot = self.repo / "snapshots" / REVISION
        self.blobs = self.repo / "blobs"
        self.snapshot.mkdir(parents=True)
        self.blobs.mkdir(parents=True)
        (self.snapshot / "config.json").write_text(
            '{"model_type":"whisper","n_mels":80}', encoding="utf-8"
        )

    def add_blob_link(self, name: str, data: bytes) -> None:
        target = self.blobs / name
        target.write_bytes(data)
        (self.snapshot / name).symlink_to(target)

    def validate_and_mark(self) -> bool:
        return bool(validate_snapshot(self.snapshot, str(self.cache), MODEL, verify_digest=True))

    def valid(self) -> bool:
        return bool(validate_snapshot(self.snapshot, str(self.cache), MODEL, verify_digest=False))

    def mark_valid_data(self) -> None:
        # Used to prove the cheap, post-download UI check respects a previously verified
        # marker, without having to re-hash production-size files on every status refresh.
        self.add_blob_link("weights.npz", WEIGHTS)
        if not self.validate_and_mark():
            raise AssertionError("test fixture should verify against its local synthetic lock")

    @property
    def marker(self):
        return self.base / "rdt-model-verification" / f"{REPO_KEY}.json"


class ModelCacheValidationTests(unittest.TestCase):
    def fixture(self):
        temp = tempfile.TemporaryDirectory(prefix="rdt-model-cache-test-")
        self.addCleanup(temp.cleanup)
        return CacheFixture(temp)

    def test_accepts_exact_pinned_mlx_npz_and_writes_marker(self):
        f = self.fixture()
        f.add_blob_link("weights.npz", WEIGHTS)
        self.assertTrue(f.validate_and_mark())
        self.assertTrue(f.marker.is_file())
        data = json.loads(f.marker.read_text(encoding="utf-8"))
        self.assertEqual(data["repo_id"], MODEL)
        self.assertEqual(data["revision"], REVISION)
        self.assertEqual(data["sha256"], WEIGHT_SHA)
        self.assertEqual(data["size_bytes"], len(WEIGHTS))

    def test_fast_cache_check_accepts_unchanged_verified_marker(self):
        f = self.fixture()
        f.mark_valid_data()
        self.assertTrue(f.valid())

    def test_rejects_wrong_sha_even_with_nonempty_weights(self):
        f = self.fixture()
        f.add_blob_link("weights.npz", b"corrupt weights")
        self.assertFalse(f.validate_and_mark())
        self.assertFalse(f.marker.exists())

    def test_rejects_pytorch_only_weights(self):
        f = self.fixture()
        f.add_blob_link("pytorch_model.bin", WEIGHTS)
        self.assertFalse(f.validate_and_mark())

    def test_rejects_safetensors_only_snapshot(self):
        f = self.fixture()
        f.add_blob_link("model.safetensors", WEIGHTS)
        self.assertFalse(f.validate_and_mark())

    def test_rejects_empty_config_json(self):
        f = self.fixture()
        (f.snapshot / "config.json").write_text("{}", encoding="utf-8")
        f.add_blob_link("weights.npz", WEIGHTS)
        self.assertFalse(f.validate_and_mark())

    def test_rejects_wrong_config_model_type(self):
        f = self.fixture()
        (f.snapshot / "config.json").write_text('{"model_type":"bert"}', encoding="utf-8")
        f.add_blob_link("weights.npz", WEIGHTS)
        self.assertFalse(f.validate_and_mark())

    def test_rejects_missing_config(self):
        f = self.fixture()
        (f.snapshot / "config.json").unlink()
        f.add_blob_link("weights.npz", WEIGHTS)
        self.assertFalse(f.validate_and_mark())

    def test_rejects_zero_byte_weight(self):
        f = self.fixture()
        f.add_blob_link("weights.npz", b"")
        self.assertFalse(f.validate_and_mark())

    def test_rejects_dangling_weight_symlink(self):
        f = self.fixture()
        (f.snapshot / "weights.npz").symlink_to(f.blobs / "missing-blob")
        self.assertFalse(f.validate_and_mark())

    def test_rejects_weight_symlink_outside_repository(self):
        f = self.fixture()
        outside = f.base / "outside.npz"
        outside.write_bytes(WEIGHTS)
        (f.snapshot / "weights.npz").symlink_to(outside)
        self.assertFalse(f.validate_and_mark())

    def test_rejects_repository_symlink_outside_configured_cache(self):
        f = self.fixture()
        outside_repo = f.base / "outside-repo"
        (outside_repo / "snapshots" / REVISION).mkdir(parents=True)
        (outside_repo / "blobs").mkdir()
        f.repo.rename(f.base / "original-repo")
        f.repo.symlink_to(outside_repo, target_is_directory=True)
        snapshot = outside_repo / "snapshots" / REVISION
        (snapshot / "config.json").write_text('{"model_type":"whisper"}', encoding="utf-8")
        (snapshot / "weights.npz").write_bytes(WEIGHTS)
        self.assertFalse(f.validate_and_mark())

    def test_rejects_unpinned_snapshot_directory(self):
        f = self.fixture()
        f.snapshot.rename(f.snapshot.parent / ("b" * 40))
        f.snapshot = f.snapshot.parent / ("b" * 40)
        f.add_blob_link("weights.npz", WEIGHTS)
        self.assertFalse(f.validate_and_mark())

    def test_rejects_model_not_in_lock(self):
        f = self.fixture()
        f.add_blob_link("weights.npz", WEIGHTS)
        self.assertFalse(validate_snapshot(f.snapshot, str(f.cache), "mlx-community/whisper-tiny", verify_digest=True))

    def test_rejects_stale_marker_after_weight_changes(self):
        f = self.fixture()
        f.mark_valid_data()
        # Replacing the blob changes at least its inode/mtime/size; cached state must clear.
        real = f.blobs / "weights.npz"
        temp = f.blobs / "replacement.npz"
        temp.write_bytes(b"other bytes")
        temp.replace(real)
        self.assertFalse(f.valid())

    def test_rejects_marker_symlink(self):
        f = self.fixture()
        f.mark_valid_data()
        outside = f.base / "fake-marker.json"
        outside.write_text(f.marker.read_text(encoding="utf-8"), encoding="utf-8")
        f.marker.unlink()
        f.marker.symlink_to(outside)
        self.assertFalse(f.valid())

    def test_lock_manifest_has_five_immutable_commit_and_digest_records(self):
        lock = json.loads((ROOT / "backend" / "rdt_model_lock.json").read_text(encoding="utf-8"))
        self.assertEqual(lock["mlx_whisper_version"], "0.4.3")
        self.assertEqual(len(lock["models"]), 5)
        for model, spec in lock["models"].items():
            self.assertRegex(spec["revision"], r"^[0-9a-f]{40}$")
            self.assertRegex(spec["sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(spec["weight_file"], "weights.npz")
            self.assertTrue(model.startswith("mlx-community/whisper-"))
        self.assertIn("mlx-community/whisper-tiny-mlx", lock["models"])
        self.assertNotIn("mlx-community/whisper-tiny", lock["models"])

    def test_corrupt_cached_snapshot_uses_pinned_force_download_repair(self):
        source = SERVER_PY.read_text(encoding="utf-8")
        self.assertIn("force_download=True", source)
        self.assertIn('revision=model_spec["revision"]', source)
        self.assertIn("tqdm_class=_RdtProgressTqdm", source)
        self.assertIn("cache_repair_failed_verification", source)
        # The recovery is limited to the approved repo_id and immutable revision;
        # it must not recursively remove the HF cache or other model repositories.
        self.assertNotIn("shutil.rmtree(cache_dir", source)

    def test_frozen_model_lock_has_executable_directory_fallback(self):
        source = SERVER_PY.read_text(encoding="utf-8")
        self.assertIn('pathlib.Path(sys.executable).resolve().parent / "rdt_model_lock.json"', source)
        self.assertIn('pathlib.Path(frozen_root) / "rdt_model_lock.json"', source)

    def test_backend_download_and_inference_are_revision_pinned(self):
        source = SERVER_PY.read_text(encoding="utf-8")
        self.assertGreaterEqual(source.count('revision=model_spec["revision"]'), 2)
        self.assertIn("_rdt_resolve_model_snapshot(MODEL_ID)", source)
        self.assertIn("local_files_only=managed", source)
        self.assertIn('digest.hexdigest() != spec["sha256"]', source)
        self.assertIn("_rdt_write_verified_marker", source)
        self.assertNotIn('path_or_hf_repo=MODEL_ID', source)

    def test_rust_supervisor_uses_nonblocking_log_and_no_pipe_joins(self):
        source = RUST_RS.read_text(encoding="utf-8")
        self.assertIn(".stdout(Stdio::from(log_writer))", source)
        self.assertIn(".stderr(Stdio::null())", source)
        self.assertIn("consume_model_download_log", source)
        self.assertIn("finish_stopping_model_download_group(&identity, &expected_exe)", source)
        self.assertNotIn("child.stdout.take()", source)
        self.assertNotIn("reader.join()", source)

    def test_completion_watchdog_is_bounded_and_independently_validated(self):
        source = RUST_RS.read_text(encoding="utf-8")
        self.assertIn("complete_seen_at.is_some_and(|at| at.elapsed() >= Duration::from_millis(1500))", source)
        self.assertNotIn("complete_seen_at.is_some_and(|at| at.elapsed() >= Duration::from_secs(8))", source)
        self.assertIn("let cache_valid = model_cache_has_weights(&root_for_worker, &model_for_worker);", source)
        self.assertIn("recovered_after_complete = cache_valid", source)

    def test_rust_cache_requires_locked_commit_digest_marker(self):
        source = RUST_RS.read_text(encoding="utf-8")
        self.assertIn('include_str!("../../../backend/rdt_model_lock.json")', source)
        self.assertIn('snapshot.file_name().and_then(|name| name.to_str()) != Some(revision.as_str())', source)
        self.assertIn('"rdt-model-verification"', source)
        self.assertIn('marker.get("sha256").and_then(Value::as_str) == Some(expected_sha.as_str())', source)
        self.assertNotIn('"model.safetensors"', source[source.index("fn model_snapshot_is_complete"):source.index("fn model_cache_has_weights")])

    def test_cancel_allows_owned_resource_tracker_helper_in_isolated_group(self):
        source = RUST_RS.read_text(encoding="utf-8")
        section = source[source.index("fn model_download_group_members_are_owned"):source.index("fn finish_stopping_model_download_group")]
        self.assertIn("resource_tracker", section)
        self.assertNotIn("members.iter().any(|item| !command_is_expected_backend", section)
        self.assertIn("leader.started != identity.started", section)

    def test_tray_restores_previous_menu_bar_icon(self):
        root = __import__("pathlib").Path(__file__).resolve().parents[1]
        source = (root / "src-tauri" / "src" / "main.rs").read_text(encoding="utf-8")
        self.assertIn('icons/rdt-menu-bar-icon.png', source)
        self.assertIn('.icon_as_template(false)', source)
        self.assertNotIn('rdt-menu-bar-template-0D45.png', source)
        self.assertTrue((root / "src-tauri" / "icons" / "rdt-menu-bar-icon.png").is_file())
        start = source.index("fn build_tray_menu(")
        tray_lines = []
        for line in source[start:].splitlines():
            if tray_lines and line.startswith("fn "):
                break
            tray_lines.append(line)
        tray = chr(10).join(tray_lines)
        self.assertNotIn('"about-rdt-connect"', tray)
        self.assertNotIn('"check-for-updates"', tray)

    def test_download_is_pinned_to_official_hugging_face_endpoint(self):
        source = RUST_RS.read_text(encoding="utf-8")
        self.assertGreaterEqual(source.count('.env("HF_ENDPOINT", "https://huggingface.co")'), 2)
        self.assertIn('.env("HF_HUB_DOWNLOAD_TIMEOUT", "60")', source)
        self.assertIn('.env("HF_HUB_ETAG_TIMEOUT", "20")', source)

    def test_apple_backend_layout_preserves_model_lock_and_frontend_lookup(self):
        source = SERVER_PY.read_text(encoding="utf-8")
        self.assertTrue(SERVER_PY.is_file())
        self.assertIn(
            'candidates.append(source_file.parents[1] / "rdt_model_lock.json")',
            source,
        )
        self.assertIn(
            'here.name == "apple" and here.parent.name == "backend"',
            source,
        )
        self.assertIn(
            'search_dirs.extend([here.parent.parent,here.parent.parent/"frontend"])',
            source,
        )
        script = (ROOT / "rdt_connect" / "scripts" / "prepare-macos-bundle.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('"$REPO_ROOT/backend/apple/server.py"', script)

    def test_python_pyincluded_lock_and_dependency_version_are_strict(self):
        script = (ROOT / "rdt_connect" / "scripts" / "prepare-macos-bundle.sh").read_text(encoding="utf-8")
        self.assertIn('--add-data "$REPO_ROOT/backend/rdt_model_lock.json:."', script)
        self.assertIn('version != "0.4.3"', script)
        req = (ROOT / "backend" / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("mlx-whisper==0.4.3", req)

    def test_custom_dmg_refuses_a_stale_build_marker(self):
        builder = (ROOT / "rdt_connect" / "scripts" / "build-custom-dmg.py").read_text(encoding="utf-8")
        package = json.loads((ROOT / "rdt_connect" / "package.json").read_text(encoding="utf-8"))
        self.assertIn('marker.encode("ascii") not in executable_bytes', builder)
        self.assertIn('build:desktop:custom-dmg:macos', package["scripts"])
        build_script = (ROOT / "rdt_connect" / "scripts" / "build-macos-with-marker.sh").read_text(encoding="utf-8")
        self.assertIn('RDT_BUILD_APP_ONLY', build_script)
        self.assertIn('build --bundles app', build_script)
        self.assertIn('RDT_SKIP_UPDATER_ARTIFACTS=1', package["scripts"]["build:desktop:custom-dmg:macos"])
        marker = (ROOT / "rdt_connect" / "scripts" / "build-id-sequence.txt").read_text(encoding="utf-8").strip()
        self.assertRegex(marker, r"^[0-9][A-Z]\d{2,}$")

    def test_failed_terminal_download_refreshes_cache_badges(self):
        source = (ROOT / "rdt_connect" / "src" / "main.ts").read_text(encoding="utf-8")
        pos = source.index('else if (incoming.phase === "failed")')
        self.assertIn("refreshModelCacheBadges()", source[pos:pos + 600])

    def test_old_transformers_tiny_id_is_absent_from_active_code(self):
        active = [ROOT / "backend" / "apple" / "server.py", ROOT / "rdt_connect" / "index.html",
                  ROOT / "rdt_connect" / "src" / "main.ts", RUST_RS]
        for path in active:
            self.assertNotIn('mlx-community/whisper-tiny"', path.read_text(encoding="utf-8"), str(path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
