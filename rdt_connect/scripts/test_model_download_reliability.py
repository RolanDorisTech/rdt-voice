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
import importlib.util
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

    def test_visual_setup_guide_is_hidden_when_https_is_ready_or_unknown(self):
        source = (ROOT / "rdt_connect" / "src" / "main.ts").read_text(encoding="utf-8")
        start = source.index("function updateVisualSetupGuide()")
        end = source.index("function scheduleVisualSetupGuideUpdate()", start)
        guide = source[start:end]
        self.assertIn("const httpsReady = Boolean(", guide)
        self.assertIn("currentLocalHttps === null ||", guide)
        self.assertIn("httpsReady", guide)
        self.assertIn('currentLocalHttps.mode === "https"', guide)
        self.assertIn("currentLocalHttps.configured", guide)
        self.assertIn("currentLocalHttps.trusted", guide)

    def test_model_metadata_preflight_has_bounded_safe_cleanup(self):
        source = RUST_RS.read_text(encoding="utf-8")
        self.assertIn(
            "checking_started_at.elapsed() >= Duration::from_secs(90)",
            source,
        )
        self.assertIn(
            "finish_stopping_model_download_group(&identity, &expected_exe)",
            source,
        )
        self.assertIn("metadata_check_timed_out", source)
        self.assertIn("The model metadata check exceeded 90 seconds.", source)
        self.assertIn("The download remains locked; select Try cancel again.", source)

    def test_downloader_stderr_uses_the_private_regular_file_log(self):
        source = RUST_RS.read_text(encoding="utf-8")
        start = source.index("fn start_model_download(")
        end = source.index("// Model downloads use their own process group", start)
        download = source[start:end]
        self.assertIn("log_writer.try_clone()", download)
        self.assertIn(".stderr(Stdio::from(stderr_writer))", download)
        self.assertIn("options.mode(0o600)", source)

    def test_python_traceback_is_reduced_to_safe_exception_class(self):
        source = RUST_RS.read_text(encoding="utf-8")
        self.assertIn('trimmed.split_once(": ")', source)
        self.assertIn('simple_name.ends_with("Error")', source)
        self.assertIn('simple_name.ends_with("Timeout")', source)
        self.assertIn("Python exception {simple_name}", source)

    def test_downloaded_model_files_are_not_claimed_ready_for_inference(self):
        source = RUST_RS.read_text(encoding="utf-8")
        start = source.index("fn start_model_download(")
        end = source.index("// Model downloads use their own process group", start)
        download = source[start:end]
        self.assertNotIn("model is ready to use", download.lower())
        self.assertIn("model loading and inference have not yet been checked", download.lower())
        self.assertIn("model loading and inference will be checked when you start the server", download.lower())

    def test_normal_backend_startup_allows_bounded_model_load_and_warmup(self):
        source = RUST_RS.read_text(encoding="utf-8")
        start = source.index("const NORMAL_BACKEND_STARTUP_TIMEOUT")
        end = source.index("fn start_backend_blocking_with_options(", start)
        normal_start = source[start:end]
        self.assertIn("Duration::from_secs(180)", normal_start)
        self.assertIn("NORMAL_BACKEND_STARTUP_TIMEOUT", normal_start)
        self.assertIn("warm-up inference happen before /health", normal_start)
        self.assertIn(
            "Duration::from_secs(startup_timeout_seconds.clamp(1, 90))",
            source,
        )

    def test_failed_normal_model_switch_restores_previous_verified_model(self):
        source = (ROOT / "rdt_connect" / "src" / "main.ts").read_text(encoding="utf-8")
        helper_start = source.index("async function restorePreviousModelAfterFailedSwitch(")
        helper_end = source.index('startButton.addEventListener("click"', helper_start)
        helper = source[helper_start:helper_end]
        self.assertIn('invoke<BackendStatus>("get_backend_status")', helper)
        self.assertIn('invoke<BackendStatus>("stop_backend")', helper)
        self.assertIn("observed.model !== failedTargetModel", helper)
        self.assertIn("stopped.running || stopped.pid", helper)
        self.assertIn("requestModelStorageStatus(previousModel)", helper)
        self.assertIn("startSelectedBackendModel(previousCache, previousPort)", helper)

        switch_start = source.index('startButton.addEventListener("click"')
        switch_end = source.index("const UPDATE_SUCCESS_STORAGE_KEY", switch_start)
        switch = source[switch_start:switch_end]
        self.assertIn("const previousModel = wasRunning && backend.model", switch)
        self.assertIn("const previousPortValue = backend.port", switch)
        self.assertIn('typeof previousPortValue === "number"', switch)
        self.assertIn("previousPortValue > 0 && previousPortValue <= 65535", switch)
        self.assertRegex(switch, r"if\s*\(\s*!targetStarted")
        self.assertIn("restorePreviousModelAfterFailedSwitch(", switch)

    def test_model_start_failure_is_not_swallowed_as_a_successful_switch(self):
        source = (ROOT / "rdt_connect" / "src" / "main.ts").read_text(encoding="utf-8")
        start = source.index("async function startSelectedBackendModel(")
        end = source.index("async function restorePreviousModelAfterFailedSwitch(", start)
        function = source[start:end]
        self.assertIn("): Promise<boolean>", function)
        self.assertIn("return startedAndVerified;", function)
        self.assertIn("if (!selectedCache.modelCached)", function)
        self.assertIn("return false;", function)

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
        expected_include = 'include_str!("../../backend/rdt_model_lock.json")'
        self.assertTrue(
            expected_include in source,
            "Rust must embed the Connect-local backend/rdt_model_lock.json",
        )
        resolved_lock = (RUST_RS.parent / "../../backend/rdt_model_lock.json").resolve()
        expected_lock = (RUST_RS.parents[2] / "backend" / "rdt_model_lock.json").resolve()
        self.assertEqual(resolved_lock, expected_lock)
        self.assertTrue(resolved_lock.is_file(), "Rust model-lock include target is missing")
        self.assertIn('snapshot.file_name().and_then(|name| name.to_str()) != Some(revision.as_str())', source)
        self.assertIn('"rdt-model-verification"', source)
        self.assertIn('marker.get("sha256").and_then(Value::as_str) == Some(expected_sha.as_str())', source)
        self.assertNotIn('"model.safetensors"', source[source.index("fn model_snapshot_is_complete"):source.index("fn model_cache_has_weights")])

    def test_cancel_allows_owned_resource_tracker_helper_in_isolated_group(self):
        source = RUST_RS.read_text(encoding="utf-8")
        section = source[source.index("fn model_download_group_members_are_owned"):source.index("fn finish_stopping_model_download_group")]
        with open("src-tauri/src/main.rs", encoding="utf-8") as rust_source_file:
            rust_source = rust_source_file.read()
        validator_start = rust_source.index("fn model_download_group_snapshot_is_owned")
        validator_end = rust_source.index("fn model_download_group_members_are_owned", validator_start)
        validator_section = rust_source[validator_start:validator_end]
        self.assertIn("resource_tracker", validator_section)
        self.assertIn("model_download_group_snapshot_is_owned(", section)
        self.assertNotIn("members.iter().any(|item| !command_is_expected_backend", section)
        self.assertIn("leader.started != identity.started", validator_section)

    def test_tray_restores_previous_menu_bar_icon(self):
        root = __import__("pathlib").Path(__file__).resolve().parents[1]
        source = (root / "src-tauri" / "src" / "main.rs").read_text(encoding="utf-8")
        self.assertIn('icons/rdt-menu-bar-icon.png', source)
        self.assertIn("let (tray_icon, tray_icon_is_template) =", source)
        mac_template_pair = re.search(
            r'#\[cfg\(target_os = "macos"\)\]\s*'
            r'let \(tray_icon, tray_icon_is_template\) = \(\s*'
            r'tauri::include_image!\("icons/rdt-menu-bar-template\.png"\),\s*'
            r'true,\s*\);',
            source,
        )
        other_platform_pair = re.search(
            r'#\[cfg\(not\(target_os = "macos"\)\)\]\s*'
            r'let \(tray_icon, tray_icon_is_template\) = \(\s*'
            r'tauri::include_image!\("icons/rdt-menu-bar-icon\.png"\),\s*'
            r'false,\s*\);',
            source,
        )
        self.assertTrue(mac_template_pair, "macOS must use its template menu-bar icon")
        self.assertTrue(other_platform_pair, "non-macOS must use the full-color tray icon")
        self.assertTrue(
            ".icon_as_template(tray_icon_is_template)" in source,
            "tray template mode must follow the platform-specific icon selection",
        )
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
        self.assertIn(
            'RDT_VOICE_BACKEND_SOURCE:-$HOME/dev/rdt-voice/backend/server.py',
            script,
        )
        self.assertIn("patch_desktop_backend.py", script)
        self.assertIn('"$CONNECT_BACKEND_SOURCE" "$STAGE/server.py"', script)
        self.assertIn('--add-data "$REPO_ROOT/backend/rdt_model_lock.json:."', script)


    def test_connect_patcher_stages_canonical_voice_with_pinned_model_lock(self):
        voice_source = Path(
            os.environ.get(
                "RDT_VOICE_BACKEND_SOURCE",
                str(Path.home() / "dev" / "rdt-voice" / "backend" / "server.py"),
            )
        ).expanduser()
        if not voice_source.is_file():
            self.skipTest("Canonical RDT Voice backend is not present on this runner.")

        patcher_path = ROOT / "rdt_connect" / "scripts" / "patch_desktop_backend.py"
        spec = importlib.util.spec_from_file_location(
            "rdt_connect_patcher_under_test", patcher_path
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        patcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(patcher)

        original_sha = hashlib.sha256(voice_source.read_bytes()).hexdigest()
        staged = patcher.patch(voice_source)
        tree = ast.parse(staged, filename="<staged-connect-backend>")
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
        }
        required = {
            "_rdt_load_model_lock",
            "_rdt_model_spec",
            "_rdt_marker_path",
            "_rdt_marker_is_current",
            "_rdt_write_verified_marker",
            "_rdt_model_snapshot_is_complete",
            "_rdt_resolve_model_snapshot",
            "_download_model_only",
        }
        self.assertTrue(required.issubset(functions))

        downloader = functions["_download_model_only"]
        hub_calls = [
            node for node in ast.walk(downloader)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "snapshot_download"
        ]
        self.assertEqual(len(hub_calls), 3)
        self.assertTrue(all(
            any(keyword.arg == "revision" for keyword in call.keywords)
            for call in hub_calls
        ))
        self.assertFalse(any(
            keyword.arg == "dry_run"
            for call in hub_calls for keyword in call.keywords
        ))
        self.assertTrue(
            patcher._rdt_function_is_revision_pinned(
                staged, "_download_model_only"
            )
        )
        # The transform pins both original call sites, then the Connect staging
        # patch removes synthetic startup warmup; only real inference remains.
        self.assertEqual(
            staged.count(
                "path_or_hf_repo=_rdt_resolve_model_snapshot(MODEL_ID)"
            ),
            1,
        )
        self.assertEqual(staged.count("mlx_whisper.transcribe("), 1)
        self.assertNotIn("_warm_path", staged)
        self.assertNotIn("path_or_hf_repo=MODEL_ID", staged)
        compile(staged, "<staged-connect-backend>", "exec")
        self.assertEqual(
            hashlib.sha256(voice_source.read_bytes()).hexdigest(),
            original_sha,
        )

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

    def test_run_action_releases_busy_when_status_refresh_fails(self):
        source = (ROOT / "rdt_connect" / "src" / "main.ts").read_text(encoding="utf-8")
        start = source.index("async function runAction(")
        end = source.index("\n\n\nlet modelAvailabilityCheckInFlight", start)
        function = source[start:end]
        self.assertEqual(function.count("catch (refreshError)"), 2)
        self.assertIn("await refreshBackendStatus();", function)
        self.assertIn("await refreshOrphanedBackendStatus();", function)
        cleanup = function.rfind("finally {")
        release = function.rfind("setBusy(false);")
        self.assertGreater(cleanup, 0)
        self.assertGreater(release, cleanup)

    def test_failed_model_switch_refuses_inconsistent_stale_pid(self):
        source = (ROOT / "rdt_connect" / "src" / "main.ts").read_text(encoding="utf-8")
        start = source.index("async function restorePreviousModelAfterFailedSwitch(")
        end = source.index('startButton.addEventListener("click"', start)
        function = source[start:end]
        guard = "if (!observed.running && observed.pid != null)"
        self.assertIn(guard, function)
        self.assertLess(
            function.index(guard),
            function.index('const stopped = await invoke<BackendStatus>("stop_backend")'),
        )
        self.assertIn("RDT Connect left it untouched.", function)

    def test_failed_model_switch_validates_saved_port_and_reports_unavailable_restore(self):
        from pathlib import Path
        source = (
            Path(__file__).resolve().parents[1] / "src" / "main.ts"
        ).read_text(encoding="utf-8")
        self.assertIn('typeof previousPortValue === "number"', source)
        self.assertIn(
            "previousPortValue > 0 && previousPortValue <= 65535",
            source,
        )
        self.assertIn(
            "the previous server configuration could not be restored automatically",
            source,
        )

    def test_model_start_finalizer_failure_returns_false_for_switch_rollback(self):
        from pathlib import Path
        source = (
            Path(__file__).resolve().parents[1] / "src" / "main.ts"
        ).read_text(encoding="utf-8")
        start = source.index("async function startSelectedBackendModel(")
        end = source.index(
            "async function restorePreviousModelAfterFailedSwitch(",
            start,
        )
        function_source = source[start:end]
        run_action = function_source.index("await runAction(async () => {")
        catch = function_source.index("} catch (error)", run_action)
        fail_closed = function_source.index(
            "startedAndVerified = false;", catch
        )
        returned = function_source.index(
            "return startedAndVerified;", fail_closed
        )
        self.assertLess(run_action, catch)
        self.assertLess(catch, fail_closed)
        self.assertLess(fail_closed, returned)

    def test_preservation_packager_uses_canonical_voice_sources_without_promoting(self):
        from pathlib import Path
        script = (
            Path(__file__).resolve().parents[1]
            / "scripts" / "prepare-macos-bundle-preserve.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("RDT_VOICE_WEB_SOURCE:-$HOME/dev/rdt-voice/alpha_testing", script)
        self.assertIn("RDT_VOICE_BACKEND_SOURCE:-$HOME/dev/rdt-voice/backend/server.py", script)
        self.assertIn('find "$VOICE_WEB_SOURCE" -maxdepth 1', script)
        self.assertIn('cp -R "$VOICE_WEB_SOURCE/locales"', script)
        self.assertIn('patch_desktop_backend.py" "$CONNECT_BACKEND_SOURCE"', script)
        self.assertNotIn('"$REPO_ROOT/backend/apple/server.py"', script)
        self.assertIn("RUN_ID=", script)
        self.assertIn("live resources are deliberately not promoted", script.lower())

    def test_preservation_packager_copies_and_verifies_voice_manifest(self):
        from pathlib import Path
        script = Path(__file__).with_name("prepare-macos-bundle-preserve.sh").read_text()
        self.assertIn('if [[ ! -s "$RESOURCES/rdt_voice_web_manifest.json" ]]; then', script)
        self.assertIn('if [[ ! -d "$RESOURCES/rdt_voice_web/assets" ]]; then', script)
        self.assertIn('cp -p "$RESOURCES/rdt_voice_web_manifest.json" "$NEXT_RESOURCES/rdt_voice_web_manifest.json"', script)
        self.assertIn("verify-alpha-r82-staging.py", script)
        self.assertIn('"$RESOURCES/rdt_voice_web/assets"', script)
        self.assertIn('"$NEXT_RESOURCES/rdt_voice_web_manifest.json"', script)
        self.assertLess(script.index("cp -p \"$RESOURCES/rdt_voice_web_manifest.json\""), script.index("prepare-offline-runtime.py"))
        self.assertLess(script.rindex("\nRDT_RUNTIME_FINAL_CHECK\n"), script.index("verify-alpha-r82-staging.py"))



    def test_cancel_completion_does_not_claim_model_ready_before_inference(self):
        source = Path("src-tauri/src/main.rs").read_text(encoding="utf-8")
        self.assertNotIn(
            "The downloader has been safely stopped and the model is ready to use.",
            source,
        )
        self.assertIn(
            "Model loading and real inference have not yet been verified.",
            source,
        )
        self.assertEqual(
            source.count("let still_this_attempt = model_download_cancel_attempt_is_current("),
            2,
        )


    def test_terminal_finalizer_requires_exact_identity_and_commits_both_stores(self):
        source = Path("src-tauri/src/main.rs").read_text(encoding="utf-8")
        self.assertIn("model_download_finalization: Arc<Mutex<()>>", source)
        helper = source[
            source.index("fn finalize_model_download_state_with"):
            source.index("fn publish_model_download_failure_for_identity_with_gate")
        ]
        self.assertIn("model_download_process_identity_matches", helper)
        self.assertIn("current_identity = None", helper)
        self.assertIn("status.active = false", helper)
        self.assertIn("cancel_requested.store(false, Ordering::SeqCst)", helper)
        self.assertIn("require_canceling && status.phase != \"canceling\"", helper)
        pid_lock = __import__("re").search(
            r"\bpid_store\b(?:\s*\.\s*as_ref\s*\(\s*\))?\s*\.\s*lock\s*\(",
            helper,
        )
        status_lock = __import__("re").search(
            r"\bstatus_store\b(?:\s*\.\s*as_ref\s*\(\s*\))?\s*\.\s*lock\s*\(",
            helper,
        )
        self.assertIsNotNone(pid_lock, "finalizer must acquire the PID mutex")
        self.assertIsNotNone(status_lock, "finalizer must acquire the status mutex")
        self.assertLess(pid_lock.start(), status_lock.start())

    def test_worker_and_cancel_use_one_finalization_gate(self):
        source = Path("src-tauri/src/main.rs").read_text(encoding="utf-8")
        worker = source[
            source.index("fn start_model_download_blocking("):
            source.index("// Model downloads use their own process group")
        ]
        cancel = source[
            source.index("fn cancel_model_download("):
            source.index("fn canonical_mlx_cache_repository")
        ]
        self.assertIn("finalize_model_download_state_with(", worker)
        self.assertIn("finalize_model_download_state_with(", cancel)
        self.assertIn("model_download_cancel_transition(&current_snapshot)", cancel)
        self.assertEqual(
            cancel.count("let still_this_attempt = model_download_cancel_attempt_is_current("),
            2,
        )
        self.assertNotIn("cancel_requested.swap(false", worker)

    def test_retry_failure_does_not_release_pid_or_active_download(self):
        source = Path("src-tauri/src/main.rs").read_text(encoding="utf-8")
        helper = source[
            source.index("fn publish_model_download_failure_for_identity_with_gate"):
            source.index("fn model_download_group_members_are_owned")
        ]
        self.assertIn("model_download_process_identity_matches", helper)
        self.assertIn("if !status.active", helper)
        self.assertIn('status.phase = "failed".into();', helper)
        self.assertNotIn("*current_identity = None", helper)




class ModelDownloadLogCleanupContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open("src-tauri/src/main.rs", encoding="utf-8") as handle:
            cls.rust_source = handle.read()

    def test_log_cleanup_attempts_unlink_after_mutex_poison(self):
        start = self.rust_source.index(
            "fn clear_model_download_log_path_with"
        )
        end = self.rust_source.index(
            "\nfn clear_model_download_log_path(",
            start,
        )
        helper = self.rust_source[start:end]

        self.assertIn("match store.lock()", helper)
        self.assertIn("Err(_) => Err(", helper)
        self.assertIn("remove_file(expected_path)", helper)

    def test_delayed_log_lock_error_is_not_silently_discarded(self):
        start = self.rust_source.index("fn cancel_model_download(")
        end = self.rust_source.index(
            "\nfn canonical_mlx_cache_repository",
            start,
        )
        cancel = self.rust_source[start:end]

        self.assertIn(
            "The model-download log-path mutex is poisoned; log cleanup may be skipped.",
            cancel,
        )
        self.assertNotIn(
            "log_path_store\n        .lock()\n        .ok()",
            cancel,
        )

    def test_log_cleanup_is_after_terminal_finalization(self):
        start = self.rust_source.index("fn cancel_model_download(")
        end = self.rust_source.index(
            "\nfn canonical_mlx_cache_repository",
            start,
        )
        cancel = self.rust_source[start:end]

        finalizer = cancel.index(
            "let finalized = finalize_model_download_state_with"
        )
        cleanup = cancel.index("match expected_log_path")
        self.assertLess(finalizer, cleanup)
        self.assertIn("confirmed downloader shutdown", cancel[cleanup:])




class ModelDownloadInspectionContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open("src-tauri/src/main.rs", encoding="utf-8") as handle:
            cls.rust_source = handle.read()

    def test_downloader_ownership_uses_checked_snapshot(self):
        start = self.rust_source.index(
            "fn model_download_group_members_are_owned("
        )
        end = self.rust_source.index(
            "// Send TERM to only the verified download process group",
            start,
        )
        body = self.rust_source[start:end]
        self.assertIn("process_snapshot_result()?", body)
        self.assertNotIn("process_snapshot()", body)

    def test_identity_capture_uses_checked_snapshot(self):
        start = self.rust_source.index(
            "fn capture_model_download_identity("
        )
        end = self.rust_source.index("\nfn ", start + 5)
        body = self.rust_source[start:end]
        self.assertIn("process_snapshot_result()?", body)
        self.assertNotIn("process_snapshot()", body)

    def test_worker_status_poison_does_not_disable_watchdog(self):
        start = self.rust_source.index("fn start_model_download_blocking(")
        end = self.rust_source.index(
            "\nfn finalize_model_download_state_with",
            start,
        )
        body = self.rust_source[start:end]
        self.assertIn("inspect_model_download_status", body)
        self.assertIn("keeping the watchdog armed", body)
        self.assertNotIn(".unwrap_or(false)", body)

    def test_process_inspection_errors_are_explicit(self):
        start = self.rust_source.index("fn checked_process_snapshot_output<")
        end = self.rust_source.index("\nfn parse_unix_process_snapshot", start)
        body = self.rust_source[start:end]
        self.assertIn("command_output.map_err", body)
        self.assertIn("if !successful", body)
        self.assertIn("let snapshot = parse(&stdout)?", body)

    def test_windows_group_liveness_fails_closed(self):
        cfg = self.rust_source.index("#[cfg(windows)]\nfn process_group_alive")
        end = self.rust_source.index(
            "\n#[cfg(windows)]\nfn windows_descendant_pids",
            cfg,
        )
        body = self.rust_source[cfg:end]
        self.assertIn("process_snapshot_result()", body)
        self.assertIn("Err(_) => true", body)




class ModelDownloadCheckedStopInspectionContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open("src-tauri/src/main.rs", encoding="utf-8") as handle:
            cls.rust_source = handle.read()

    @staticmethod
    def function_bodies(source, name):
        import re
        bodies = []
        for match in re.finditer(
            rf"(?m)^fn {re.escape(name)}\s*\(", source
        ):
            next_fn = re.search(
                r"(?m)^(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+[A-Za-z_]\w*",
                source[match.end():],
            )
            end = match.end() + next_fn.start() if next_fn else len(source)
            bodies.append(source[match.start():end])
        return bodies

    def test_windows_signal_checks_process_table_before_and_after_taskkill(self):
        bodies = self.function_bodies(self.rust_source, "send_signal_checked")
        windows = [body for body in bodies if "taskkill" in body]
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].count("process_snapshot_result()?"), 2)
        self.assertNotIn("process_snapshot()", windows[0])

    def test_downloader_group_ownership_uses_checked_process_snapshot(self):
        bodies = self.function_bodies(
            self.rust_source, "model_download_group_members_are_owned"
        )
        self.assertEqual(len(bodies), 1)
        self.assertIn("process_snapshot_result()?", bodies[0])

    def test_windows_group_liveness_uses_checked_process_snapshot(self):
        bodies = self.function_bodies(self.rust_source, "process_group_alive")
        windows = [body for body in bodies if "windows_descendant_pids" in body]
        self.assertEqual(len(windows), 1)
        self.assertIn("process_snapshot_result()", windows[0])

    def test_worker_has_no_lock_unwrap_or_false_fallback(self):
        bodies = self.function_bodies(
            self.rust_source, "start_model_download_blocking"
        )
        self.assertEqual(len(bodies), 1)
        self.assertNotRegex(
            bodies[0],
            r"\.lock\(\)\s*\.unwrap_or\(false\)",
        )




class ModelDownloadRegistrationMutexContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open("src-tauri/src/main.rs", encoding="utf-8") as handle:
            cls.rust_source = handle.read()

    def test_registration_stores_are_locked_before_spawn(self):
        start = self.rust_source.index(
            "fn start_model_download_blocking("
        )
        end = self.rust_source.index("\nfn ", start + 1)
        worker = self.rust_source[start:end]
        self.assertLess(
            worker.index("lock_model_download_registration_stores("),
            worker.index("command.spawn()"),
        )
        self.assertIn("*download_pid = Some(identity.clone())", worker)
        self.assertIn("*stored_log = Some(log_path.clone())", worker)
        self.assertNotIn("if let Ok(mut download_pid)", worker)
        self.assertNotIn("if let Ok(mut stored_log)", worker)

    def test_both_registration_mutex_errors_are_propagated(self):
        start = self.rust_source.index(
            "fn lock_model_download_registration_stores<"
        )
        end = self.rust_source.index(
            "\nfn start_model_download_blocking(", start
        )
        helper_body = self.rust_source[start:end]
        self.assertEqual(helper_body.count(".lock()"), 2)
        self.assertEqual(helper_body.count(".map_err("), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
